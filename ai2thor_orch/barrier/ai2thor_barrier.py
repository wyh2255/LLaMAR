"""AI2ThorBarrier — multi-agent round-synchronization barrier.

Reuses the threading model from ``sar_orch/barrier.py`` L113-220:
``threading.Event`` per agent + ``threading.Lock``, bridged to asyncio
via ``run_in_executor(None, event.wait)`` for event waits and
``loop.run_in_executor(self._executor._executor, ...)`` for the
blocking ``_execute_round`` call.

The event waits use asyncio's **default** executor pool, while the
Controller call (``_execute_round``) runs on the **dedicated** pool
inside ``ControllerExecutor`` (``max_workers=1``).  This separation
guarantees event waits and the controller call cannot starve each
other (deadlock prevention per implementation plan R2).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from ai2thor_orch.contracts.types import (
    ActionResult,
    PublicObservation,
    CoordinatorObservation,
    RunStatus,
)
from ai2thor_orch.visibility import AliasRegistry


class AI2ThorBarrier:
    """Multi-agent round barrier for AI2Thor environments.

    Args:
        num_agents: Number of agents participating.
        executor: A :class:`~ai2thor_orch.executor.controller_executor.ControllerExecutor`
            instance wrapping the (fake or unity) controller.
        max_steps: Maximum number of rounds before the run finishes naturally.
        step_timeout: Seconds to wait for all agents to submit before auto-filling
            NoOp for missing agents.
        alias_registry: Optional shared :class:`AliasRegistry`.  Created fresh if
            not provided.
    """

    def __init__(
        self,
        num_agents: int,
        executor: Any,
        max_steps: int,
        step_timeout: float = 60.0,
        alias_registry: AliasRegistry | None = None,
    ) -> None:
        if num_agents < 1:
            raise ValueError(f"num_agents must be >= 1, got {num_agents}")
        if max_steps < 1:
            raise ValueError(f"max_steps must be >= 1, got {max_steps}")
        if step_timeout <= 0:
            raise ValueError(f"step_timeout must be > 0, got {step_timeout}")

        self.num_agents: int = num_agents
        self._executor: Any = executor
        self.max_steps: int = max_steps
        self.step_timeout: float = step_timeout

        # Round state
        self._round_no: int = 0
        self._step_counter: int = 0
        self._action_queue: dict[int, str] = {}
        self._current_results: dict[int, ActionResult] = {}
        self._timeout_agents: list[int] = []
        self._finished: bool = False
        self._stopped: bool = False
        self._stop_reason: str = ""
        self._domain_metrics: dict[str, Any] = {}

        # Threading primitives (asyncio-safe across worker threads)
        self._obs_events: list[threading.Event] = [
            threading.Event() for _ in range(num_agents)
        ]
        self._step_lock: threading.Lock = threading.Lock()

        # Visibility
        self._alias_registry: AliasRegistry = alias_registry or AliasRegistry()

    @property
    def round_no(self) -> int:
        """Current round number (monotonic version counter)."""
        return self._round_no

    # -- Public API (aligns with SARBarrier for G3 unification) ---------------

    async def submit_action(self, agent_idx: int, action: str) -> ActionResult:
        """Submit an action for *agent_idx* and wait for the round to complete.

        If all agents have submitted (or timeout), executes the round and
        returns the ``ActionResult`` for this agent.

        Args:
            agent_idx: Agent index (0-based).
            action: Action string (e.g. ``"MoveAhead"``, ``"RotateLeft"``).

        Returns:
            The :class:`ActionResult` for this agent after the round executes.
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

        if self._stopped or self._finished:
            return ActionResult(
                agent_idx=agent_idx,
                observation="",
                success=False,
            )

        current_round = self._round_no

        with self._step_lock:
            # Check for duplicate submission (same agent, same round)
            if agent_idx in self._action_queue:
                # Return the already-stored result for this agent if available
                cached = self._current_results.get(agent_idx)
                if cached is not None and cached.raw.get("_round", -1) == current_round:
                    return cached
                # Otherwise proceed — this is a re-submit, which we allow
                # (the first action is kept, see SARBarrier behaviour)

            self._obs_events[agent_idx].clear()
            self._action_queue[agent_idx] = action
            all_submitted = len(self._action_queue) == self.num_agents
            if all_submitted:
                self._timeout_agents = []

        if all_submitted:
            # Fast path: we are the last agent, execute immediately
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self._executor._executor, self._execute_round, current_round
            )
        else:
            # Wait for other agents with timeout
            deadline = time.monotonic() + self.step_timeout
            while True:
                if self._stopped or self._finished:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Timeout: fill NoOp for missing agents and execute
                    with self._step_lock:
                        if self._round_no > current_round:
                            break  # Step already advanced
                        timeout_agents = []
                        for i in range(self.num_agents):
                            if i not in self._action_queue:
                                self._action_queue[i] = "NoOp"
                                timeout_agents.append(i)
                        if timeout_agents:
                            self._timeout_agents = timeout_agents
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        self._executor._executor, self._execute_round, current_round
                    )
                    break

                # Wait — use run_in_executor(None, ...) to avoid sharing
                # the Controller executor's thread pool (deadlock prevention).
                triggered = await asyncio.get_event_loop().run_in_executor(
                    None, self._obs_events[agent_idx].wait, remaining
                )

                if self._stopped or self._finished:
                    break

                with self._step_lock:
                    if self._round_no > current_round:
                        break  # Round already advanced while we were waiting

                if not triggered:
                    continue  # Event timed out, loop will recheck remaining

                # Triggered but round didn't advance — spurious wakeup
                self._obs_events[agent_idx].clear()

        # Return this agent's result
        result = self._current_results.get(agent_idx)
        if result is not None:
            return result
        return ActionResult(
            agent_idx=agent_idx,
            observation="",
            success=False,
        )

    def is_finished(self) -> bool:
        """Check if the run is finished (naturally or via stop)."""
        return self._finished or self._stopped

    def get_run_status(self) -> RunStatus:
        """Return current run status as a ``RunStatus`` DTO.

        Fields match the G3 ``EnvironmentRunControl`` protocol.
        """
        return RunStatus(
            step=self._step_counter,
            max_steps=self.max_steps,
            finished=self._finished,
            stopped=self._stopped,
            stop_reason=self._stop_reason,
            timeout_agents=list(self._timeout_agents),
            domain_metrics=dict(self._domain_metrics),
        )

    def request_stop(self, reason: str = "env_stop") -> None:
        """Request a graceful stop — records reason, sets flags, wakes waiters.

        Idempotent: subsequent calls are no-ops.
        """
        if self._stopped:
            return
        self._stopped = True
        self._finished = True
        self._stop_reason = reason
        # Wake all waiting agents
        for ev in self._obs_events:
            ev.set()

    def stop(self) -> None:
        """Immediate stop — calls ``request_stop()`` and shuts down executor.

        Aligns with SARBarrier.stop() behaviour.
        """
        self.request_stop("env_stop")
        try:
            self._executor.stop()
        except Exception:
            pass

    def snapshot_public(self, agent_idx: int) -> PublicObservation:
        """Return a ``PublicObservation`` for *agent_idx*.

        ``visible_objects`` are aliased via :class:`AliasRegistry` — raw
        objectIds are never exposed.
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

        result = self._current_results.get(agent_idx)
        if result is None:
            return PublicObservation(
                agent_idx=agent_idx,
                text="",
                step=self._step_counter,
            )

        # Derive rotation from raw metadata if available
        rotation: dict[str, float] | None = None
        raw_agents = result.raw.get("agents", [])
        if agent_idx < len(raw_agents):
            rot = raw_agents[agent_idx].get("rotation", {})
            if isinstance(rot, dict):
                rotation = {k: float(v) for k, v in rot.items() if isinstance(v, (int, float))}

        # Alias raw objectIds from metadata objects
        raw_objects = result.raw.get("objects", [])
        visible_aliases: list[str] = []
        for obj in raw_objects:
            raw_id = obj.get("objectId", "")
            if raw_id:
                visible_aliases.append(self._alias_registry.register(raw_id))

        return PublicObservation(
            agent_idx=agent_idx,
            text=result.observation,
            position=result.position,
            rotation=rotation,
            inventory=list(result.inventory),
            visible_objects=visible_aliases,
            step=self._step_counter,
        )

    def snapshot_coordinator(self) -> CoordinatorObservation:
        """Return a ``CoordinatorObservation`` — global view of the scene.

        This snapshot is intended for the coordinator LLM, showing all agents
        and objects.
        """
        agents_snapshot: list[dict[str, Any]] = []
        objects_set: set[str] = set()
        objects_list: list[dict[str, Any]] = []

        # Collect from all agent results
        for idx in range(self.num_agents):
            result = self._current_results.get(idx)
            if result is None:
                agents_snapshot.append({
                    "agent_idx": idx,
                    "name": f"Agent{idx}",
                    "position": None,
                    "inventory": [],
                })
                continue

            raw_agents = result.raw.get("agents", [])
            agent_info = raw_agents[idx] if idx < len(raw_agents) else {}
            agents_snapshot.append({
                "agent_idx": idx,
                "name": agent_info.get("name", f"Agent{idx}"),
                "position": agent_info.get("position"),
                "rotation": agent_info.get("rotation"),
                "inventory": result.inventory,
            })

            # Collect objects from metadata
            raw_objects = result.raw.get("objects", [])
            for obj in raw_objects:
                raw_id = obj.get("objectId", "")
                if raw_id and raw_id not in objects_set:
                    objects_set.add(raw_id)
                    objects_list.append({
                        "objectId": raw_id,
                        "alias": self._alias_registry.register(raw_id),
                        "objectType": obj.get("objectType", ""),
                        "position": obj.get("position"),
                        "visible": obj.get("visible", True),
                    })

        return CoordinatorObservation(
            round_no=self._round_no,
            agents=agents_snapshot,
            objects=objects_list,
            step=self._step_counter,
            max_steps=self.max_steps,
        )

    # -- Internal -------------------------------------------------------------

    def _execute_round(self, expected_round: int) -> None:
        """Execute one round: build action list, call executor, distribute results.

        Runs on the Controller executor's dedicated thread pool via
        ``loop.run_in_executor(self._executor._executor, ...)``.
        """
        with self._step_lock:
            # Prevent double execution for the same round
            if self._round_no != expected_round:
                return

            if self._stopped:
                return

            actions = []
            for i in range(self.num_agents):
                raw_action = self._action_queue.get(i, "NoOp")
                actions.append({"action": raw_action})

            # Clear events so waiters for the NEXT round start fresh
            for ev in self._obs_events:
                ev.clear()

        # Execute — this is the blocking controller call on the dedicated executor
        try:
            step_results = self._executor.execute_step(actions)
        except Exception as exc:
            # On failure, return error results for all agents
            with self._step_lock:
                for i in range(self.num_agents):
                    self._current_results[i] = ActionResult(
                        agent_idx=i,
                        action=actions[i]["action"],
                        observation=f"Execution error: {exc}",
                        success=False,
                        raw={"error": str(exc)},
                    )
                self._timeout_agents = list(self._timeout_agents)
                self._step_counter += 1
                self._round_no += 1
                self._action_queue.clear()
            # Wake all
            for ev in self._obs_events:
                ev.set()
            return

        # Distribute results
        with self._step_lock:
            extract = _extract_step_results(
                actions, step_results, self._timeout_agents, self._alias_registry
            )
            self._current_results = extract["results"]
            self._domain_metrics = extract["domain_metrics"]
            self._finished = (
                self._step_counter + 1 >= self.max_steps or self._stopped
            )
            self._step_counter += 1
            self._round_no += 1
            self._action_queue.clear()

        # Wake all waiting agents
        for ev in self._obs_events:
            ev.set()


def _extract_step_results(
    actions: list[dict[str, Any]],
    step_results: list[dict[str, Any]],
    timeout_agents: list[int],
    alias_registry: AliasRegistry,
) -> dict[str, Any]:
    """Build per-agent ActionResults from raw executor output.

    Args:
        actions: The action dicts sent to ``execute_step``.
        step_results: The metadata dicts returned by ``execute_step``.
        timeout_agents: Indices of agents that were NoOp-filled.
        alias_registry: For redacting observation text.

    Returns:
        Dict with keys:
            - ``results``: ``dict[int, ActionResult]``
            - ``domain_metrics``: aggregated metrics
    """
    results: dict[int, ActionResult] = {}
    all_success = True
    all_objects: list[dict[str, Any]] = []

    for i, (action, step_res) in enumerate(zip(actions, step_results)):
        metadata = step_res.get("agent_metadata", {})
        success = bool(metadata.get("lastActionSuccess", True))
        if not success:
            all_success = False

        # Position
        pos: tuple[float, float, float] | None = None
        agents_meta = metadata.get("agents", [])
        if i < len(agents_meta):
            pos_dict = agents_meta[i].get("position", {})
            if isinstance(pos_dict, dict):
                try:
                    pos = (float(pos_dict["x"]), float(pos_dict["y"]), float(pos_dict["z"]))
                except (KeyError, ValueError, TypeError):
                    pos = None

        # Inventory
        inventory: list[str] = []
        if i < len(agents_meta):
            inv_data = agents_meta[i].get("inventory", {})
            if isinstance(inv_data, dict):
                inv_objects = inv_data.get("objects", [])
                if isinstance(inv_objects, list):
                    inventory = [str(o.get("objectType", o)) for o in inv_objects if isinstance(o, dict)]
                elif isinstance(inv_objects, list):
                    inventory = [str(o) for o in inv_objects]
            elif isinstance(inv_data, list):
                inventory = [str(o.get("objectType", str(o))) if isinstance(o, dict) else str(o) for o in inv_data]

        # Observation text
        action_name = action.get("action", str(action))
        action_success = "succeeded" if success else "failed"
        obs = f"Action {action_name} {action_success}."
        if inventory:
            obs += f" Inventory: {', '.join(inventory)}."

        # Redact raw objectIds from observation
        obs = alias_registry.redact(obs)

        # Collect objects metadata for domain metrics
        objs = metadata.get("objects", [])
        if isinstance(objs, list):
            all_objects.extend(objs)

        results[i] = ActionResult(
            agent_idx=i,
            action=action_name,
            observation=obs,
            success=success,
            position=pos,
            inventory=inventory,
            raw=metadata,
        )

    # Domain metrics
    domain_metrics: dict[str, Any] = {
        "round_success": all_success,
        "num_actions": len(actions),
        "num_objects": len(all_objects),
        "timeout_agents": list(timeout_agents),
    }

    return {
        "results": results,
        "domain_metrics": domain_metrics,
    }

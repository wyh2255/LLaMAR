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

Assembly surface (env-contract P5-3): the barrier is consumed by the
generic assembly (``orchestration.assembly.run_assembly``) through the
same interface the SAR barrier provides —

- ``get_metrics() -> {steps, coverage, transport_rate, finished}`` — poll-loop
  budget check / display;
- ``drain_step_logs() -> list[dict]`` — one entry per executed round buffered
  since the last drain, so no step is lost when several complete between
  polls (each entry carries actions / successes / observations /
  coverage / transport_rate / finished / timeout_agents / noop_sources /
  step_duration_ms / error_types / completed_subtasks_delta);
- ``is_finished()`` / ``request_stop()`` / ``stop()`` / ``get_run_status()``
  — run control (G8 seam).

Terminal semantics (design doc §5.3 — 成功真值): mission *success*
(``get_metrics()["finished"]``) is set only when the task contract's
postconditions verify (``ai2thor_orch.verifier.verify_round`` over
controller truth); heuristic progress never counts.  Exhausting
``max_steps`` is a **budget gate**, not success: further submissions are
refused (``_budget_exhausted``) and the run ends with the assembly's
``max_steps_reached`` end reason.  ``is_finished()`` / ``RunStatus.finished``
report *run termination* (budget spent / stopped / verified complete),
matching the kernel contract in ``tests/test_run_control.py``
(自然收官 ≠ 停止).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from a2a.coordinator.run_control import RunStatus
from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import (
    ActionResult,
    CoordinatorObservation,
    PublicObservation,
    RoundResult,
)
from ai2thor_orch.metrics.task_metrics import TaskMetricsTracker
from ai2thor_orch.verifier.verifier import verify_round
from ai2thor_orch.visibility import AliasRegistry


class AI2ThorBarrier:
    """Multi-agent round barrier for AI2Thor environments.

    Round semantics (aligned with ``SARBarrier.submit_action``):

    1. all slots submitted and at least one holds a real action → the step
       executes immediately;
    2. all slots hold non-advancing placeholders (``advance=False`` idle
       heartbeats) → the barrier waits indefinitely and burns no step; the
       first real submission triggers execution;
    3. partial submission → after ``step_timeout`` seconds the missing
       agents are NoOp-filled (``source="timeout_injected"``) and the step
       executes.

    Once ``max_steps`` rounds have executed the budget is exhausted
    (``_budget_exhausted``): new submissions are refused immediately (no
    round executes, no step burns) and the assembly ends the run with
    ``max_steps_reached``.  ``is_finished()`` reports the run as over
    (budget spent / stopped / verifier-confirmed completion), while the
    ``get_metrics()["finished"]`` success truth stays reserved for
    verifier-confirmed postconditions.

    NoOp provenance is recorded per agent slot (``""`` for real actions; one
    of ``"llm"`` / ``"idle_heartbeat"`` / ``"timeout_injected"`` otherwise)
    and surfaces through ``get_run_status().domain_metrics["noop_sources"]``
    and :meth:`get_last_round_log`.

    Args:
        num_agents: Number of agents participating.
        executor: A :class:`~ai2thor_orch.executor.controller_executor.ControllerExecutor`
            instance wrapping the (fake or unity) controller.
        max_steps: Maximum number of rounds before the step budget is
            exhausted (budget gate — see above).
        step_timeout: Seconds to wait for all agents to submit before auto-filling
            NoOp for missing agents.
        alias_registry: Optional shared :class:`AliasRegistry`.  Created fresh if
            not provided.
        contract: Optional :class:`TaskContract` — enables per-round
            verification (``verify_round`` over controller truth, the
            ``finished`` success branch) and the task-metrics tracker behind
            :meth:`get_metrics`.  ``None`` keeps metrics at neutral values.
    """

    def __init__(
        self,
        num_agents: int,
        executor: Any,
        max_steps: int,
        step_timeout: float = 60.0,
        alias_registry: AliasRegistry | None = None,
        contract: TaskContract | None = None,
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
        self._contract: TaskContract | None = contract
        #: Task-metrics tracker behind ``get_metrics()`` / step-log rows
        #: (interaction coverage, transport rate, action reliability, balance).
        self._tracker: TaskMetricsTracker | None = (
            TaskMetricsTracker(contract, num_agents) if contract is not None else None
        )

        # Round state
        self._round_no: int = 0
        self._step_counter: int = 0
        #: ``(action, advance, noop_source)`` per agent slot.  ``noop_source``
        #: is "" for real actions and one of "llm" / "idle_heartbeat" /
        #: "timeout_injected" for NoOps (P5-1 provenance, aligned with
        #: ``SARBarrier``'s NoOpSource mechanism).
        self._action_queue: dict[int, tuple[str, bool, str]] = {}
        self._current_results: dict[int, ActionResult] = {}
        #: NoOp fills accumulated for the round currently being waited on;
        #: consumed by ``_execute_round`` into ``_timeout_agents`` so the
        #: latter always reflects the most recently executed round.
        self._current_timeout_agents: list[int] = []
        #: Timeout fills of the most recently executed round (run-status view).
        self._timeout_agents: list[int] = []
        #: Round log of the most recently executed round (see
        #: :meth:`get_last_round_log`).
        self._last_actions: list[str] = []
        self._last_noop_sources: list[str] = []
        self._finished: bool = False
        self._stopped: bool = False
        #: Step budget exhausted (``max_steps`` rounds executed) — new
        #: submissions are refused.  Run termination, NOT a success flag
        #: (mission success stays verifier-driven).
        self._budget_exhausted: bool = False
        self._stop_reason: str = ""
        self._domain_metrics: dict[str, Any] = {}
        #: Scene name captured from round metadata ``sceneName`` (sticky once
        #: seen) — fills ``CoordinatorObservation.scene`` so the rendered
        #: Environment State shows e.g. ``Scene: FloorPlan1`` instead of an
        #: empty slot (RP1b: ``Scene:  | Step: 4/8``).
        self._scene_name: str = ""

        #: Executed-round step logs buffered since the last drain
        #: (``drain_step_logs()``); each entry is a complete per-step record.
        self._pending_step_logs: list[dict[str, Any]] = []
        #: Verdict of the most recent per-round verification (None without a
        #: contract) and the most recent tracker snapshot — also mirrored
        #: into ``get_metrics()`` for poll-loop display.
        self._last_verdict: dict[str, Any] | None = None
        self._last_tracker_snapshot: dict[str, Any] = {}

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

    @property
    def alias_registry(self) -> AliasRegistry:
        """This barrier's visibility registry (single shared instance).

        Worker tools constructed by the EnvPack bind to the very same
        registry so alias→raw resolution and observation redaction agree
        (P5-2: ``Ai2ThorEnvPack.build_worker_tools`` reads this property).
        """
        return self._alias_registry

    # -- Public API (aligns with SARBarrier for G3 unification) ---------------

    async def submit_action(
        self,
        agent_idx: int,
        action: str,
        *,
        advance: bool = True,
        source: str | None = None,
    ) -> ActionResult:
        """Submit an action for *agent_idx* and wait for the round to complete.

        If all agents have submitted (or timeout), executes the round and
        returns the ``ActionResult`` for this agent.

        Args:
            agent_idx: Agent index (0-based).
            action: Action string (e.g. ``"MoveAhead"``, ``"RotateLeft"``).
            advance: Whether this submission advances the environment step.
                Idle heartbeats pass ``advance=False`` so their NoOp only
                occupies the agent's slot without pairing into a real step —
                all-idle workers must not burn the step budget.  A step is
                executed only once at least one agent submits a real action
                (``advance=True``) or the per-step timeout fires for missing
                agents.
            source: Explicit NoOp origin for the round-record marker, one of
                ``"llm"`` / ``"idle_heartbeat"`` / ``"timeout_injected"``.
                When omitted for a NoOp it is derived from ``advance``:
                ``True`` → the LLM called the no_op tool, ``False`` → the
                worker's idle-heartbeat loop.  Real (non-NoOp) actions always
                carry an empty source.

        Returns:
            The :class:`ActionResult` for this agent after the round executes.
            After ``stop()`` / mission completion / budget exhaustion the call
            returns an empty failed result immediately (no round executes).
        """
        if not (0 <= agent_idx < self.num_agents):
            raise ValueError(
                f"agent_idx {agent_idx} out of range [0, {self.num_agents})"
            )

        if self._stopped or self._finished or self._budget_exhausted:
            return ActionResult(
                agent_idx=agent_idx,
                observation="",
                success=False,
            )

        current_round = self._round_no
        loop = asyncio.get_event_loop()

        with self._step_lock:
            # Check for duplicate submission (same agent, same round)
            if agent_idx in self._action_queue:
                # Return the already-stored result for this agent if available
                cached = self._current_results.get(agent_idx)
                if cached is not None and cached.raw.get("_round", -1) == current_round:
                    return cached
                # Otherwise proceed — this is a re-submit (e.g. the worker's
                # idle placeholder is superseded by a real action), which we
                # allow: the newest action replaces the placeholder slot.

            self._obs_events[agent_idx].clear()
            if action.startswith("NoOp"):
                if source is None:
                    # Derive the origin when the caller did not pin it —
                    # advance=True means the LLM invoked the no_op tool,
                    # advance=False means the worker's idle-heartbeat loop.
                    # startswith covers both "NoOp" and "NoOp()" spellings.
                    source = "llm" if advance else "idle_heartbeat"
                elif source not in ("llm", "idle_heartbeat", "timeout_injected"):
                    raise ValueError(
                        f"invalid NoOp source {source!r}; expected one of "
                        '"llm", "idle_heartbeat", "timeout_injected"'
                    )
            else:
                source = ""
            self._action_queue[agent_idx] = (action, advance, source)
            all_submitted = len(self._action_queue) == self.num_agents
            has_real = any(adv for _, adv, _ in self._action_queue.values())

        if all_submitted and has_real:
            # Fast path: all slots are in and at least one holds a real
            # action — execute immediately.
            await loop.run_in_executor(
                self._executor._executor, self._execute_round, current_round
            )
        elif all_submitted:
            # All slots hold non-advancing placeholders (e.g. every worker
            # idle-heartbeating): wait indefinitely without burning a step.
            # The first real (advance=True) submission flips has_real and
            # triggers the step; its submitter also runs _execute_round, but
            # the expected_round guard makes a double execution a no-op.
            while True:
                if self._stopped or self._finished:
                    break

                with self._step_lock:
                    if self._round_no > current_round:
                        break  # Round executed by another agent
                    has_real = any(adv for _, adv, _ in self._action_queue.values())
                if has_real:
                    await loop.run_in_executor(
                        self._executor._executor, self._execute_round, current_round
                    )
                    break

                # Infinite wait: no deadline, so an all-placeholder state
                # never reaches the timeout fill (which would burn a step).
                await loop.run_in_executor(
                    None, self._obs_events[agent_idx].wait, None
                )

                if self._stopped or self._finished:
                    break

                with self._step_lock:
                    if self._round_no > current_round:
                        break  # Round executed

                # Triggered but round didn't advance — re-clear and keep waiting
                self._obs_events[agent_idx].clear()
        else:
            # Wait for other agents with timeout.  ``indefinite`` flips when
            # the deadline fires with every slot already present and
            # non-advancing: the all-idle placeholder state must not burn a
            # step (env_contract §2.2-②), so the wait continues indefinitely
            # instead of executing a placeholder-only round.
            deadline = time.monotonic() + self.step_timeout
            indefinite = False
            while True:
                if self._stopped or self._finished:
                    break

                wait_timeout: float | None = None
                if not indefinite:
                    wait_timeout = deadline - time.monotonic()

                if wait_timeout is not None and wait_timeout <= 0:
                    # Timeout: fill NoOp for missing agents and execute.
                    # System-injected NoOp consumes a step, so it is recorded
                    # as advance=True with source "timeout_injected".
                    with self._step_lock:
                        if self._round_no > current_round:
                            break  # Round already executed
                        timeout_agents = [
                            i
                            for i in range(self.num_agents)
                            if i not in self._action_queue
                        ]
                        for i in timeout_agents:
                            self._action_queue[i] = (
                                "NoOp",
                                True,
                                "timeout_injected",
                            )
                        # Accumulate rather than overwrite: multiple waiting
                        # agents compute their own deadline independently, so
                        # two can expire close together and both reach this
                        # block for the same round. Whichever runs second
                        # finds the slots the first already filled and
                        # recomputes an empty "missing" list — a plain
                        # assignment would let that spurious empty result
                        # erase the first agent's correct timeout record.
                        if timeout_agents:
                            self._current_timeout_agents = sorted(
                                set(self._current_timeout_agents)
                                | set(timeout_agents)
                            )
                        has_real = any(
                            adv for _, adv, _ in self._action_queue.values()
                        )
                    if not timeout_agents and not has_real:
                        # Nothing missing: every slot already holds an
                        # idle placeholder (a real action would have been
                        # executed by its submitter).  Burning a
                        # placeholder-only step here would break the
                        # "all idle placeholders never advance" invariant —
                        # switch to the indefinite wait, same as the
                        # all-submitted placeholder path.
                        indefinite = True
                        continue
                    await loop.run_in_executor(
                        self._executor._executor, self._execute_round, current_round
                    )
                    break

                # Wait — use run_in_executor(None, ...) to avoid sharing
                # the Controller executor's thread pool (deadlock prevention).
                triggered = await loop.run_in_executor(
                    None, self._obs_events[agent_idx].wait, wait_timeout
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
        """Check whether the run loop is over (run termination).

        True once the step budget is spent (自然收官 — kernel contract in
        ``tests/test_run_control.py``: ``finished=True`` while
        ``stopped=False``), once the mission verifies complete, or once
        ``stop()`` was requested.  Budget exhaustion terminates the *run*,
        not the mission: the success truth is
        ``get_metrics()["finished"]`` / ``domain_metrics["verified_completion"]``.
        """
        return self._finished or self._stopped or self._budget_exhausted

    def get_metrics(self) -> dict:
        """Return the task metrics surface consumed by the assembly poll loop.

        Keys (SAR-parity): ``coverage`` / ``transport_rate`` / ``steps`` /
        ``finished``; ``interaction_coverage`` rides along for logging.
        ``coverage`` = postcondition coverage of the most recent verification
        round (``0.0`` until a contract-verified round has executed);
        ``transport_rate`` = completed-subtask ratio of the task-metrics
        tracker; ``finished`` = **mission success truth** —
        verifier-confirmed completion only (a budget-ended run reports
        ``False`` so ``classify_end_reason`` yields ``max_steps_reached``,
        not ``success``).
        """
        return {
            "coverage": (
                float(self._last_verdict.get("coverage", 0.0))
                if self._last_verdict is not None
                else 0.0
            ),
            "transport_rate": float(
                self._last_tracker_snapshot.get("transport_rate", 0.0)
            ),
            "interaction_coverage": float(
                self._last_tracker_snapshot.get("interaction_coverage", 0.0)
            ),
            "steps": self._step_counter,
            "finished": self._finished,
        }

    def drain_step_logs(self) -> list[dict]:
        """Return and clear every executed-round log buffered since the last drain.

        Mirrors ``SARBarrier.drain_step_logs()``: a caller polling on a fixed
        interval receives one complete record per executed round — no step is
        silently skipped even when several rounds complete between polls.
        Each entry carries ``step / actions / successes / observations /
        noop_sources / timeout_agents / error_types / step_duration_ms /
        completed_subtasks_delta / coverage / transport_rate /
        interaction_coverage / verified_completion``.
        """
        with self._step_lock:
            drained = self._pending_step_logs
            self._pending_step_logs = []
        return drained

    def get_task_metrics(self) -> dict:
        """Cumulative task-metrics snapshot (tracker-backed).

        Full tracker view consumed by the AI2Thor assembly hooks for the
        terminal ``summary.json`` (interaction coverage, transport rate,
        action reliability, timeout counts, balance).  Empty dict when the
        barrier was built without a contract.
        """
        if self._tracker is None:
            return {}
        return self._tracker.snapshot()

    def get_run_status(self) -> RunStatus:
        """Return current run status as a ``RunStatus`` DTO.

        Fields match the G3 ``EnvironmentRunControl`` protocol.
        ``finished`` follows :meth:`is_finished` — run termination
        (budget spent / stopped / verified complete).
        """
        return RunStatus(
            step=self._step_counter,
            max_steps=self.max_steps,
            finished=self.is_finished(),
            stopped=self._stopped,
            stop_reason=self._stop_reason,
            timeout_agents=list(self._timeout_agents),
            domain_metrics=dict(self._domain_metrics),
        )

    def get_last_round_log(self) -> dict[str, Any]:
        """Return the round log of the most recently executed round.

        Mirrors ``SARBarrier.get_last_step_log()``: per-agent action strings
        plus per-agent NoOp provenance.  ``noop_sources[i]`` is ``""`` for a
        real action and otherwise one of ``"llm"`` / ``"idle_heartbeat"`` /
        ``"timeout_injected"``; ``timeout_agents`` lists the slots the
        barrier NoOp-filled after the step timeout.
        """
        return {
            "step": self._step_counter,
            "finished": self._finished,
            "actions": list(self._last_actions),
            "noop_sources": list(self._last_noop_sources),
            "timeout_agents": list(self._timeout_agents),
        }

    def last_round_result(self) -> RoundResult:
        """Return the most recently executed round as a ``RoundResult``.

        Verifier input surface (P5-2): feeds the existing round verifier
        (``ai2thor_orch.verifier.verify_round``) used by the coordinator
        ``finish_task`` completion truth check — per-agent ``ActionResult``
        records (raw controller metadata included) plus timeout slots,
        finished flag, and domain metrics of that round.  Before any round
        has executed this is an empty ``RoundResult`` (verification then
        reports not-complete, fail-closed).
        """
        return RoundResult(
            round_no=self._round_no,
            results=[self._current_results[i] for i in sorted(self._current_results)],
            timeout_agents=list(self._timeout_agents),
            finished=self._finished,
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
        objectIds are never exposed — and filtered by the metadata's
        per-object ``visible`` flag: only objects the agent can actually see
        enter the view.  (RP1b: the unfiltered metadata is a full-house
        inventory of every scene object; presented as "visible" it makes
        workers chase objects they cannot see.)
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

        # Alias raw objectIds from metadata objects — visible objects only.
        # Missing-key convention: treat as visible (conservative — never hide
        # an object the metadata does not explicitly mark hidden); the same
        # rule is applied in ``snapshot_coordinator`` so the worker and
        # coordinator views stay consistent.
        raw_objects = result.raw.get("objects", [])
        visible_aliases: list[str] = []
        for obj in raw_objects:
            if not bool(obj.get("visible", True)):
                continue
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
        and the objects currently visible to them (``visible`` flag filtered,
        same rule as :meth:`snapshot_public`).  ``scene`` is filled from the
        most recent round's metadata ``sceneName`` and stays sticky once
        seen; it is empty until the first round has executed.
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

            raw_metadata = result.raw if isinstance(result.raw, dict) else {}
            raw_agents = raw_metadata.get("agents", [])
            agent_info = raw_agents[idx] if idx < len(raw_agents) else {}
            agents_snapshot.append({
                "agent_idx": idx,
                "name": agent_info.get("name", f"Agent{idx}"),
                "position": agent_info.get("position"),
                "rotation": agent_info.get("rotation"),
                "inventory": result.inventory,
            })

            # Scene name: ai2thor metadata carries ``sceneName`` per event.
            # Sticky capture so later snapshots keep the last known name.
            scene_name = str(raw_metadata.get("sceneName") or "").strip()
            if scene_name:
                self._scene_name = scene_name

            # Collect objects from metadata — visible objects only (RP1b:
            # hidden objects must not leak into either view; missing-key
            # convention identical to snapshot_public: treat as visible).
            raw_objects = raw_metadata.get("objects", [])
            for obj in raw_objects:
                if not bool(obj.get("visible", True)):
                    continue
                raw_id = obj.get("objectId", "")
                if raw_id and raw_id not in objects_set:
                    objects_set.add(raw_id)
                    objects_list.append({
                        "objectId": raw_id,
                        "alias": self._alias_registry.register(raw_id),
                        "objectType": obj.get("objectType", ""),
                        "position": obj.get("position"),
                        "visible": bool(obj.get("visible", True)),
                    })

        return CoordinatorObservation(
            round_no=self._round_no,
            agents=agents_snapshot,
            objects=objects_list,
            scene=self._scene_name,
            step=self._step_counter,
            max_steps=self.max_steps,
        )

    # -- Internal -------------------------------------------------------------

    def _execute_round(self, expected_round: int) -> None:
        """Execute one round: build action list, call executor, distribute results.

        Runs on the Controller executor's dedicated thread pool via
        ``loop.run_in_executor(self._executor._executor, ...)``.

        P5-3 side effects beyond result distribution: buffers one complete
        step log for :meth:`drain_step_logs`, refreshes the task-metrics
        tracker and the per-round verification verdict, and maintains the
        terminal flags (``_budget_exhausted`` budget gate / truth-based
        ``_finished``).
        """
        with self._step_lock:
            # Prevent double execution for the same round
            if self._round_no != expected_round:
                return

            if self._stopped:
                return

            actions = []
            noop_sources = []
            for i in range(self.num_agents):
                entry = self._action_queue.get(i)
                # Tolerate legacy plain-string entries (tests / callers that
                # construct the queue directly); the source stays "".
                if isinstance(entry, tuple):
                    raw_action = entry[0]
                    src = entry[2] if len(entry) > 2 else ""
                else:
                    raw_action = entry if isinstance(entry, str) else "NoOp"
                    src = ""
                actions.append({"action": raw_action})
                noop_sources.append(src)

            # Clear events so waiters for the NEXT round start fresh
            for ev in self._obs_events:
                ev.clear()

        # Execute — this is the blocking controller call on the dedicated executor
        started = time.monotonic()
        try:
            step_results = self._executor.execute_step(actions)
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            # On failure, return error results for all agents
            with self._step_lock:
                self._timeout_agents = sorted(set(self._current_timeout_agents))
                self._current_timeout_agents = []
                self._last_actions = [a["action"] for a in actions]
                self._last_noop_sources = list(noop_sources)
                for i in range(self.num_agents):
                    self._current_results[i] = ActionResult(
                        agent_idx=i,
                        action=actions[i]["action"],
                        observation=f"Execution error: {exc}",
                        success=False,
                        raw={"error": str(exc)},
                    )
                self._append_step_log(
                    step=self._step_counter + 1,
                    actions=[a["action"] for a in actions],
                    successes=[False] * self.num_agents,
                    observations=[
                        self._current_results[i].observation
                        for i in range(self.num_agents)
                    ],
                    noop_sources=noop_sources,
                    step_duration_ms=duration_ms,
                    error_types=[f"execution_error: {exc}"],
                    completed_subtasks_delta=[],
                )
                self._step_counter += 1
                self._round_no += 1
                self._budget_exhausted = self._step_counter >= self.max_steps
                self._action_queue.clear()
            # Wake all
            for ev in self._obs_events:
                ev.set()
            return

        duration_ms = (time.monotonic() - started) * 1000.0

        # Distribute results
        with self._step_lock:
            # Round's NoOp fill record: accumulated by the timeout path while
            # waiting, consumed here so get_run_status().timeout_agents always
            # reflects the most recently executed round.
            self._timeout_agents = sorted(set(self._current_timeout_agents))
            self._current_timeout_agents = []
            extract = _extract_step_results(
                actions,
                step_results,
                self._timeout_agents,
                self._alias_registry,
                noop_sources,
            )
            self._current_results = extract["results"]
            self._domain_metrics = extract["domain_metrics"]
            self._last_actions = [a["action"] for a in actions]
            self._last_noop_sources = list(noop_sources)

            round_result = RoundResult(
                round_no=self._round_no,
                results=[
                    self._current_results[i]
                    for i in range(self.num_agents)
                    if i in self._current_results
                ],
                timeout_agents=list(self._timeout_agents),
                finished=False,
                domain_metrics=dict(self._domain_metrics),
            )

            # Task metrics: progress heuristics (interaction coverage /
            # transport rate / reliability / balance) — they never drive the
            # ``finished`` success branch (design doc §5.3).
            tracker_snapshot: dict[str, Any] = {}
            if self._tracker is not None:
                tracker_snapshot = self._tracker.update(round_result)
                self._last_tracker_snapshot = tracker_snapshot

            # 成功真值: per-round postcondition verification over controller
            # truth; only this (or ``stop()``) sets ``finished``.
            verdict: dict[str, Any] | None = None
            if self._contract is not None:
                verdict = verify_round(round_result, self._contract)
                self._last_verdict = verdict

            goal_coverage = (
                float(self._last_verdict.get("coverage", 0.0))
                if self._last_verdict is not None
                else 0.0
            )
            self._domain_metrics = {
                **self._domain_metrics,
                "coverage": goal_coverage,
                "transport_rate": float(tracker_snapshot.get("transport_rate", 0.0)),
                "interaction_coverage": float(
                    tracker_snapshot.get("interaction_coverage", 0.0)
                ),
                "verified_completion": bool(
                    verdict and verdict.get("verified_completion")
                ),
            }
            self._finished = bool(
                verdict and verdict.get("verified_completion")
            )

            self._append_step_log(
                step=self._step_counter + 1,
                actions=[a["action"] for a in actions],
                successes=[
                    bool(self._current_results[i].success)
                    for i in range(self.num_agents)
                    if i in self._current_results
                ],
                observations=[
                    self._current_results[i].observation
                    for i in range(self.num_agents)
                    if i in self._current_results
                ],
                noop_sources=noop_sources,
                step_duration_ms=duration_ms,
                error_types=self._collect_error_types(),
                completed_subtasks_delta=list(
                    tracker_snapshot.get("completed_subtasks_delta", [])
                ),
                coverage=goal_coverage,
                transport_rate=float(tracker_snapshot.get("transport_rate", 0.0)),
                interaction_coverage=float(
                    tracker_snapshot.get("interaction_coverage", 0.0)
                ),
                verified_completion=bool(
                    verdict and verdict.get("verified_completion")
                ),
            )

            self._step_counter += 1
            self._round_no += 1
            # Budget gate: this round was the last one the run may execute.
            self._budget_exhausted = self._step_counter >= self.max_steps
            self._action_queue.clear()

        # Wake all waiting agents
        for ev in self._obs_events:
            ev.set()

    # -- Step-log buffer (assembly drain surface) ---------------------------

    def _append_step_log(
        self,
        *,
        step: int,
        actions: list[str],
        successes: list[bool],
        observations: list[str],
        noop_sources: list[str],
        step_duration_ms: float,
        error_types: list[str],
        completed_subtasks_delta: list[str],
        coverage: float = 0.0,
        transport_rate: float = 0.0,
        interaction_coverage: float = 0.0,
        verified_completion: bool = False,
    ) -> None:
        """Buffer one complete executed-round record for :meth:`drain_step_logs`.

        Caller must hold ``self._step_lock``.
        """
        self._pending_step_logs.append(
            {
                "step": step,
                "actions": list(actions),
                "successes": list(successes),
                "observations": list(observations),
                "noop_sources": list(noop_sources),
                "timeout_agents": list(self._timeout_agents),
                "error_types": list(error_types),
                "step_duration_ms": step_duration_ms,
                "completed_subtasks_delta": list(completed_subtasks_delta),
                "coverage": coverage,
                "transport_rate": transport_rate,
                "interaction_coverage": interaction_coverage,
                "verified_completion": verified_completion,
                "finished": self._finished,
            }
        )

    def _collect_error_types(self) -> list[str]:
        """Per-agent error descriptors of the current round (failures only)."""
        errors: list[str] = []
        for i in range(self.num_agents):
            result = self._current_results.get(i)
            if result is None or result.success:
                continue
            raw = result.raw if isinstance(result.raw, dict) else {}
            message = (
                raw.get("errorMessage") or raw.get("errorCode") or raw.get("error")
            )
            errors.append(str(message) if message else "action_failed")
        return errors


def _extract_step_results(
    actions: list[dict[str, Any]],
    step_results: list[dict[str, Any]],
    timeout_agents: list[int],
    alias_registry: AliasRegistry,
    noop_sources: list[str] | None = None,
) -> dict[str, Any]:
    """Build per-agent ActionResults from raw executor output.

    Args:
        actions: The action dicts sent to ``execute_step``.
        step_results: The metadata dicts returned by ``execute_step``.
        timeout_agents: Indices of agents that were NoOp-filled.
        alias_registry: For redacting observation text.
        noop_sources: Per-agent NoOp provenance ("" = real action, else one
            of "llm" / "idle_heartbeat" / "timeout_injected").

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
        # NoOp provenance per agent slot (P5-1): "" = real action, else one of
        # "llm" / "idle_heartbeat" / "timeout_injected".
        "noop_sources": list(noop_sources) if noop_sources is not None else [],
    }

    return {
        "results": results,
        "domain_metrics": domain_metrics,
    }

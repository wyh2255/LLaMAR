"""Deterministic metrics derived from executed AI2Thor actions.

This module deliberately separates legacy action-reference coverage from
state-based completion verification. It never consumes coordinator/worker LLM
claims: all values come from ActionResult records emitted by the barrier.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import ActionResult, RoundResult

_IGNORED_ACTIONS = {"noop", "done", "idle", "pass"}
_ACTION_RE = re.compile(r"^(?P<verb>[A-Za-z]+)\((?P<arguments>.*)\)$")
_NUMBERED_ALIAS_RE = re.compile(r"_(?:\d+)$")


class TaskMetricsTracker:
    """Accumulate task-progress, action-reliability, and balance metrics.

    ``interaction_coverage`` intentionally mirrors the legacy checker: a task
    object or receptacle counts when an action references it, even if that
    action fails. ``transport_rate`` is stricter: it credits only successful
    low-level interactions and their mechanically implied navigation evidence.
    """

    def __init__(self, contract: TaskContract, num_agents: int) -> None:
        if num_agents < 1:
            raise ValueError("num_agents must be >= 1")

        self._contract = contract
        self._num_agents = num_agents
        self._coverage_objects = {
            _normalise_object_name(name) for name in contract.coverage_objects
        }
        self._parsed_subtasks = [
            _parse_action(subtask) for subtask in contract.subtasks
        ]
        self._completed_subtask_indices: set[int] = set()
        self._touched_coverage_objects: set[str] = set()
        self._inventory_by_agent = {
            agent_idx: list(contract.initial_inventory.get(agent_idx, []))
            for agent_idx in range(num_agents)
        }
        self._action_attempts = 0
        self._successful_actions = 0
        self._failed_actions = 0
        self._timeout_count = 0
        self._timeout_rounds = 0
        self._per_agent_successes = {agent_idx: 0 for agent_idx in range(num_agents)}
        self._last_completed_delta: list[str] = []
        self._last_round_counts = {
            "action_attempts": 0,
            "successful_actions": 0,
            "failed_actions": 0,
        }

    def update(self, round_result: RoundResult) -> dict[str, Any]:
        """Record a completed round and return the cumulative metric snapshot."""
        timeout_agents = set(round_result.timeout_agents)
        if timeout_agents:
            self._timeout_count += len(timeout_agents)
            self._timeout_rounds += 1

        self._last_completed_delta = []
        round_attempts = 0
        round_successes = 0
        round_failures = 0

        for result in round_result.results:
            action = _action_from_result(result)
            verb, arguments = _parse_action(action)
            previous_inventory = self._inventory_by_agent.get(result.agent_idx, [])

            self._record_interaction_coverage(arguments)
            if verb not in _IGNORED_ACTIONS:
                round_attempts += 1
                self._action_attempts += 1
                if result.success:
                    round_successes += 1
                    self._successful_actions += 1
                    self._per_agent_successes[result.agent_idx] = (
                        self._per_agent_successes.get(result.agent_idx, 0) + 1
                    )
                    self._record_successful_subtasks(
                        verb, arguments, previous_inventory
                    )
                else:
                    round_failures += 1
                    self._failed_actions += 1

            self._inventory_by_agent[result.agent_idx] = list(result.inventory)

        self._last_round_counts = {
            "action_attempts": round_attempts,
            "successful_actions": round_successes,
            "failed_actions": round_failures,
        }
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of all cumulative measurements."""
        interaction_coverage = _ratio(
            len(self._touched_coverage_objects), len(self._coverage_objects)
        )
        transport_rate = _ratio(
            len(self._completed_subtask_indices), len(self._contract.subtasks)
        )
        return {
            "interaction_coverage": interaction_coverage,
            "transport_rate": transport_rate,
            "completed_subtasks": [
                self._contract.subtasks[index]
                for index in sorted(self._completed_subtask_indices)
            ],
            "completed_subtasks_delta": list(self._last_completed_delta),
            "completed_subtask_count": len(self._completed_subtask_indices),
            "total_subtasks": len(self._contract.subtasks),
            "action_attempts": self._action_attempts,
            "successful_actions": self._successful_actions,
            "failed_actions": self._failed_actions,
            "action_success_rate": _ratio(
                self._successful_actions, self._action_attempts
            ),
            "round_action_attempts": self._last_round_counts["action_attempts"],
            "round_successful_actions": self._last_round_counts["successful_actions"],
            "round_failed_actions": self._last_round_counts["failed_actions"],
            "round_action_success_rate": _ratio(
                self._last_round_counts["successful_actions"],
                self._last_round_counts["action_attempts"],
            ),
            "timeout_count": self._timeout_count,
            "timeout_rounds": self._timeout_rounds,
            "per_agent_successful_actions": {
                str(agent_idx): count
                for agent_idx, count in self._per_agent_successes.items()
            },
            "balance": _balance(self._per_agent_successes.values()),
        }

    def _record_interaction_coverage(self, arguments: Iterable[str]) -> None:
        for argument in arguments:
            object_name = _normalise_object_name(argument)
            if object_name in self._coverage_objects:
                self._touched_coverage_objects.add(object_name)

    def _record_successful_subtasks(
        self,
        verb: str,
        arguments: tuple[str, ...],
        previous_inventory: list[str],
    ) -> None:
        if verb == "pickupobject" and arguments:
            object_name = _normalise_object_name(arguments[0])
            self._mark_subtask("navigateto", (object_name,))
            self._mark_subtask("pickupobject", (object_name,))
            return

        if verb == "putobject":
            self._mark_subtask(verb, arguments)
            if arguments and previous_inventory:
                receptacle = _normalise_object_name(arguments[0])
                held_object = _normalise_object_name(previous_inventory[0])
                self._mark_subtask("navigateto", (receptacle, held_object))
                self._mark_subtask("putobject", (receptacle, held_object))
            return

        if verb in {"openobject", "closeobject"}:
            self._mark_subtask(verb, arguments)
            if arguments and previous_inventory:
                receptacle = _normalise_object_name(arguments[0])
                held_object = _normalise_object_name(previous_inventory[0])
                self._mark_subtask(verb, (receptacle, held_object))
            return

        self._mark_subtask(verb, arguments)

    def _mark_subtask(self, verb: str, arguments: tuple[str, ...]) -> None:
        target = (verb, tuple(_normalise_object_name(arg) for arg in arguments))
        for index, expected in enumerate(self._parsed_subtasks):
            if index in self._completed_subtask_indices or expected != target:
                continue
            self._completed_subtask_indices.add(index)
            self._last_completed_delta.append(self._contract.subtasks[index])
            return


def _action_from_result(result: ActionResult) -> str:
    if result.action:
        return result.action
    raw_action = result.raw.get("lastAction", "") if result.raw else ""
    return raw_action if isinstance(raw_action, str) else ""


def _parse_action(action: str) -> tuple[str, tuple[str, ...]]:
    match = _ACTION_RE.fullmatch(action.strip())
    if match is None:
        return action.strip().lower(), ()
    arguments = tuple(
        _normalise_object_name(argument)
        for argument in match.group("arguments").split(",")
        if argument.strip()
    )
    return match.group("verb").lower(), arguments


def _normalise_object_name(value: str) -> str:
    object_name = value.strip().split("|", 1)[0]
    return _NUMBERED_ALIAS_RE.sub("", object_name)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _balance(successes: Iterable[int]) -> float:
    values = list(successes)
    if not values:
        return 1.0
    largest = max(values)
    return min(values) / largest if largest else 1.0

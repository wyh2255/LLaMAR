"""Task verifier — checks postconditions and round-level completion.

Supports ``3_transport_groceries`` by verifying coverage objects are in the
Fridge receptacle.  Extensible to other tasks via the ``TaskContract``;
for tasks without a postcondition implementation the verifier reports
"unsupported" (audit semantics) and the caller falls back to the paper-gauge
tracker completion truth (D7 — see ``supports_task``).
"""

from __future__ import annotations

from typing import Any

from ai2thor_orch.contracts.task import TaskContract
from ai2thor_orch.contracts.types import RoundResult

#: 有 postcondition 级实现的任务（D7 回退判据的单一真源）。
_SUPPORTED_TASKS = frozenset({"3_transport_groceries"})


def supports_task(task_id: str) -> bool:
    """Whether the verifier implements postcondition checks for *task_id*.

    ``False`` means the verifier cannot judge this task's completion.  Callers
    (the ``finish_task`` completion factory in ``env_pack``) must fall back to
    the paper-gauge tracker truth (action-evidence tally filled) instead of
    treating "unsupported" as "not finished" (D7).
    """
    return task_id in _SUPPORTED_TASKS


def verify_postconditions(final_metadata: dict[str, Any], contract: TaskContract) -> bool:
    """Check whether the final environment state satisfies task postconditions.

    For ``3_transport_groceries``:
        All coverage objects (excluding "Fridge") must be inside the Fridge.
        An object is "inside Fridge" if one of its metadata
        ``parentReceptacles`` entries refers to the Fridge by objectId
        (matched on the type-name prefix, e.g. ``Fridge|-02.10|+00.00|+01.07``).

    Args:
        final_metadata: The final ``Event.metadata`` dict from the controller.
        contract: The task contract defining coverage_objects.

    Returns:
        ``True`` if all applicable coverage objects are in their target locations.
    """
    if supports_task(contract.task_id):
        return _verify_transport_groceries(final_metadata, contract)
    # Future tasks: add elif branches here
    return False


def verify_round(round_result: RoundResult, contract: TaskContract) -> dict[str, Any]:
    """Evaluate a single round's result against the task contract.

    Args:
        round_result: The :class:`RoundResult` from one completed round.
        contract: The task contract defining coverage_objects.

    Returns:
        Dict with keys:
            - ``verified_completion``: ``bool`` — whether all postconditions are met.
            - ``coverage``: ``float`` (0.0–1.0) — fraction of coverage objects satisfied.
            - ``details``: ``dict`` — breakdown per object.
    """
    if supports_task(contract.task_id):
        return _verify_round_transport_groceries(round_result, contract)

    return {
        "verified_completion": False,
        "coverage": 0.0,
        "goal_coverage": 0.0,
        "details": {"error": f"Unsupported task: {contract.task_id}"},
    }


# ── Internal: 3_transport_groceries ────────────────────────────────────────


def _receptacle_is_fridge(entry: Any) -> bool:
    """Check whether one ``parentReceptacles`` entry refers to the Fridge.

    真机语义（Bug C / RP3 实证）：``parentReceptacles`` 存的是**完整 objectId**
    （形如 ``Fridge|-02.10|+00.00|+01.07``），修复前用 ``"Fridge" in [...]``
    精确成员匹配 → 真机永假（transport 0.59 时 coverage 仍恒 0.0）。这里按
    ``|`` 前的类型名前缀匹配；裸类型名条目 split 后仍是自身，天然兼容。
    """
    return isinstance(entry, str) and entry.split("|", 1)[0] == "Fridge"


def _objects_in_fridge(metadata: dict[str, Any]) -> dict[str, bool]:
    """Check which grocery objects are inside the Fridge.

    Args:
        metadata: Controller event metadata dict with an ``objects`` list.

    Returns:
        Mapping of objectType -> bool (True if inside Fridge).
    """
    objects: list[dict[str, Any]] = metadata.get("objects", [])
    result: dict[str, bool] = {}

    for obj in objects:
        obj_type = obj.get("objectType", "")
        parent_recep = obj.get("parentReceptacles", [])

        # An object is "in the fridge" if any parentReceptacles entry refers
        # to the Fridge (full objectId on real builds; bare name in old fakes)
        in_fridge = isinstance(parent_recep, list) and any(
            _receptacle_is_fridge(entry) for entry in parent_recep
        )
        # Only mark positive; default stays out
        if in_fridge:
            result[obj_type] = True
        elif obj_type not in result:
            result[obj_type] = False

    return result


def _verify_transport_groceries(
    metadata: dict[str, Any], contract: TaskContract
) -> bool:
    """Verify all non-Fridge coverage objects are inside the Fridge."""
    coverage = contract.coverage_objects
    # Fridge itself is not an object to put IN the fridge
    grocery_objects = [o for o in coverage if o != "Fridge"]
    if not grocery_objects:
        return True

    in_fridge = _objects_in_fridge(metadata)
    return all(in_fridge.get(obj, False) for obj in grocery_objects)


def _verify_round_transport_groceries(
    round_result: RoundResult, contract: TaskContract
) -> dict[str, Any]:
    """Round-level verification for transport_groceries.

    Aggregates metadata from all agent results in the round and checks
    which coverage objects are currently in the Fridge.
    """
    coverage = contract.coverage_objects
    grocery_objects = [o for o in coverage if o != "Fridge"]

    # Collect all objects from all agent results
    all_objects: list[dict[str, Any]] = []
    for action_result in round_result.results:
        raw = action_result.raw
        objs = raw.get("objects", [])
        if isinstance(objs, list):
            all_objects.extend(objs)

    # Check each grocery object
    in_fridge = _objects_in_fridge({"objects": all_objects})
    satisfied = [obj for obj in grocery_objects if in_fridge.get(obj, False)]
    coverage_ratio = len(satisfied) / len(grocery_objects) if grocery_objects else 1.0
    all_satisfied = len(satisfied) == len(grocery_objects)

    details: dict[str, Any] = {
        "grocery_objects": grocery_objects,
        "in_fridge": {obj: in_fridge.get(obj, False) for obj in grocery_objects},
        "satisfied": satisfied,
        "round_no": round_result.round_no,
        "num_agents": len(round_result.results),
    }

    return {
        "verified_completion": all_satisfied,
        "coverage": round(coverage_ratio, 4),
        "goal_coverage": round(coverage_ratio, 4),
        "details": details,
    }

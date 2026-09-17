"""Phase 5 error taxonomy for failed ToolResult outcomes.

Pure classifier: maps the *structured* ``error`` field of a failed
``ToolResult`` to a public error code, or to one of two sentinels:

- an allowlisted framework code (e.g. ``worker_busy``) when the error exactly
  names it (or carries an ``<code>: <detail>`` prefix),
- a domain failure category (e.g. ``object_not_visible``) when the error
  matches a *measured* environment business-failure message form — exact
  textual signatures only, so unrecognized text can never be misattributed,
- ``unclassified_tool_error`` when the error is non-empty but not recognized,
- ``missing_error_code`` when the error field is empty/None.

The module also maps *exceptions* (which are never JSON-serializable and must
never be stored inside dispatch state, task events, or status payloads) to a
safe structured string: an allowlisted error code plus the message.  This keeps
the persistence boundary free of raw exception objects.

The module never parses ``content="Error: ..."`` text and has no ``a2a`` or
``sar_orch`` dependency, so both router/worker agents can import it without
introducing a framework cycle.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "DOMAIN_ERROR_CODES",
    "FRAMEWORK_ERROR_CODES",
    "MISSING_ERROR_CODE",
    "NETWORK_ERROR",
    "REPORTED_FRAMEWORK_ERROR_CODES",
    "UNCLASSIFIED_TOOL_ERROR",
    "WORKER_UNREACHABLE",
    "classify_error",
    "error_code_for_result",
    "exception_error_code",
    "exception_to_safe_string",
]

# Sentinel for a failed ToolResult whose structured error is empty.
MISSING_ERROR_CODE = "missing_error_code"

# Sentinel for a failed ToolResult whose structured error is not an
# allowlisted framework code.
UNCLASSIFIED_TOOL_ERROR = "unclassified_tool_error"

# Allowlisted framework code for a generic A2A/transport layer failure
# (e.g. ``A2AClientError`` raised while pushing a task to a worker).
NETWORK_ERROR = "network_error"

# Allowlisted framework code for a worker that cannot be reached/resolved.
WORKER_UNREACHABLE = "worker_unreachable"

# Allowlisted framework error codes produced by coordinator/worker tools.
# Derived from the `error="..."` values in src/a2a/builtin_tools,
# src/a2a/coordinator and sar_orch/tools.
FRAMEWORK_ERROR_CODES: frozenset[str] = frozenset(
    {
        "action_failed",
        "activation_error",
        "body_too_long",
        "cancel_failed",
        "configure_failed",
        "connection_failed",
        "control_mutation_forbidden",
        "dependency_incomplete",
        "dispatch_acceptance_failed",
        "dispatch_allocation_failed",
        "duplicate_members",
        "empty_members",
        "graph_activation_required",
        "invalid_action",
        "invalid_direction",
        "invalid_message_type",
        "invalid_plan",
        "invalid_supply_type",
        "invalid_target",
        "max_tasks_reached",
        "mission_not_finished",
        "mission_runtime_aborted",
        "missing_body",
        "missing_content",
        "missing_recipient",
        "missing_related_task_id",
        "missing_subject",
        "missing_who",
        "network_error",
        "no_active_team",
        "no_output_available",
        "no_registry",
        "no_worker",
        "node_not_found",
        "node_not_ready",
        "not_team_members",
        "not_yet_dispatched",
        "planned_worker_mismatch",
        "verification_failed",
        "undeclared_task",
        "partial_delivery_failure",
        "partial_revoke_failure",
        "partial_sync_failure",
        "participant_busy",
        "plan_update_failed",
        "recipient_not_found",
        "runtime_unavailable",
        "send_failed",
        "skill_not_found",
        "subject_too_long",
        "task_not_routable_yet",
        "team_ack_failed",
        "team_preparation_failed",
        "team_setup_failed",
        "unknown_task_id",
        "unsupported_delete",
        "unsupported_update",
        "verifier_not_configured",
        "worker_busy",
        "worker_not_found",
        "worker_offline",
        "worker_unreachable",
    }
)

# The subset of allowlisted codes that the Phase 5 acceptance aggregator
# reports explicitly as `framework_error_counts`.
REPORTED_FRAMEWORK_ERROR_CODES: tuple[str, ...] = (
    "worker_busy",
    "task_not_routable_yet",
    "unknown_task_id",
)

# Domain (environment) business-failure categories — a second, deliberately
# separate vocabulary from ``FRAMEWORK_ERROR_CODES``.  These name failures
# where the action was well-formed and *did* reach the simulator, but the
# simulator refused it for domain reasons (object not visible, locomotion
# blocked, action precondition unmet).  Categories are only added together
# with a *measured* message form (see ``_DOMAIN_ERROR_PATTERNS``), so the
# vocabulary stays grounded — an unrecognized message can never be
# misattributed to a domain category, it falls back to
# ``unclassified_tool_error``.
#
# Naming:
#   object_not_visible     the acted-on object cannot be resolved within the
#                          agent's visibility (ai2thor: "Target object not
#                          found within the specified visibility"); covers the
#                          tool alias guard ("Unknown object alias: ...").
#   navigation_blocked     a movement action is refused because a scene object
#                          occupies the target grid cell (ai2thor: "<X> is
#                          blocking Agent N from moving by (dx, dy, dz).").
#   object_state_mismatch  the action's precondition on world/agent state does
#                          not hold (ai2thor_orch executor soft failure:
#                          empty-hand PutObject, errorCode "EmptyHand").
#   camera_horizon_out_of_range
#                          the camera pitch (horizon) implied by the action is
#                          outside the build's allowed range [-30, 60] degrees:
#                          LookUp/LookDown asked to go past a limit, or a
#                          Teleport carried an out-of-range horizon (measured
#                          build refusal messages, see patterns below).
#
# NOT landed here (no measured/verifiable message form yet, would be guessing):
#   object_not_in_reach / no_valid_action — add them together with the exact
#   message form they must match, per the "以实测信息为准" rule.
DOMAIN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "camera_horizon_out_of_range",
        "navigation_blocked",
        "object_not_visible",
        "object_state_mismatch",
    }
)

# Message-form rules for measured domain failure texts.  Each entry is
# ``(category, compiled pattern)``; the pattern is only ever applied to the
# structured error string (never ToolResult.content).  Patterns are written
# as distinctive multi-word signatures so unrelated text cannot match.
_DOMAIN_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Measured in the A100 first run (44 descriptors across l3_full/l3_short
    # trajectory.csv ErrorTypes): pickup/open on an object that is outside the
    # agent's visibility — the ai2thor build reports it as a
    # NullReferenceException from getInteractableSimObjectFromId.
    (
        "object_not_visible",
        re.compile(r"(?i)target object not found within the specified visibility"),
    ),
    # Measured in the same runs (11 descriptors): locomotion refused because a
    # scene object blocks the movement delta.
    (
        "navigation_blocked",
        re.compile(r"(?i)\bis blocking agent \d+ from moving by\b"),
    ),
    # Tool-side alias guard (ai2thor_orch/tools/worker/*): the referenced
    # alias was never registered for this run — the object is not visible to
    # the team ("Ensure the object is visible.").
    (
        "object_not_visible",
        re.compile(r"(?i)\bunknown (?:object|receptacle) alias\b"),
    ),
    # executor soft failure (unity_controller.py :func:`_map_put_object`):
    # "PutObject 要求该 agent 手上持有物体，…" composed with the "[EmptyHand]"
    # errorCode tail (see ai2thor_orch/tools/worker/_barrier_helpers.py).
    (
        "object_state_mismatch",
        re.compile(r"(?i)\[emptyhand\]|手上持有物体"),
    ),
    # Camera-horizon range refusals（RP4 真机归因，2026-09-17）。两条文案都是
    # build 自身的拒绝对话（ai2thor 5.0.0 Unity）：
    # (1) ``teleportFull`` 对越界 horizon 抛的异常原文——实测 LookUp/LookDown
    #     到 +60 界后相机 euler 回读带 60.00002 浮点残差，Teleport 缺省
    #     horizon 即取该残差值 → 该 agent 之后每一步 Teleport 连锁被拒；
    # (2) ``LookUp``/``LookDown`` 越过 ±界时 ``checkForUpDownAngleLimit`` 的
    #     拒绝文案（down 形态为实测原文；up 形态是同一守卫函数的对称分支）。
    (
        "camera_horizon_out_of_range",
        re.compile(r"(?i)each horizon must be in \[-?\d+(?:\.\d+)?:\d+(?:\.\d+)?\]"),
    ),
    (
        "camera_horizon_out_of_range",
        re.compile(
            r"(?i)can't look (?:down|up) beyond \d+(?:\.\d+)? degrees "
            r"(?:below|above) the forward horizon"
        ),
    ),
)


def classify_error(error: str | None) -> str:
    """Classify a failed ToolResult's structured ``error`` into a public code.

    Resolution order:

    1. exact allowlisted framework code (or domain category name);
    2. ``<code>: <detail>`` prefix carrying such a code;
    3. measured domain failure message forms (``_DOMAIN_ERROR_PATTERNS``).

    Anything else non-empty falls back to ``unclassified_tool_error`` — the
    domain rules are textual signatures of *recorded* failure messages, not
    fuzzy heuristics.

    Args:
        error: The ``ToolResult.error`` field value (never ``content``).

    Returns:
        An allowlisted framework error code, a domain failure category,
        ``unclassified_tool_error`` for non-empty unrecognized text, or
        ``missing_error_code`` when empty.
    """
    if error is None or not str(error).strip():
        return MISSING_ERROR_CODE
    candidate = str(error).strip()
    if candidate in FRAMEWORK_ERROR_CODES or candidate in DOMAIN_ERROR_CODES:
        return candidate
    # Structured errors may carry an ``<allowlisted_code>: <detail>`` prefix
    # (e.g. ``undeclared_task: node-1 not in MissionGraph.``); the leading
    # token is still an allowlisted code.  Only the structured error field is
    # inspected, never ``content``.
    prefix = candidate.split(":", 1)[0].strip()
    if prefix in FRAMEWORK_ERROR_CODES or prefix in DOMAIN_ERROR_CODES:
        return prefix
    for code, pattern in _DOMAIN_ERROR_PATTERNS:
        if pattern.search(candidate):
            return code
    return UNCLASSIFIED_TOOL_ERROR


def error_code_for_result(result: Any) -> str:
    """Return the public error code for a ToolResult-like object.

    Successful results produce an empty code; failed results are classified
    from their structured ``error`` field only.
    """
    if getattr(result, "success", True):
        return ""
    return classify_error(getattr(result, "error", None))


# Exception type names whose ``__module__``/name marks an A2A/transport layer
# failure rather than a worker-side or domain failure.
_NETWORK_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "A2AError",
        "A2AClientError",
        "A2AClientTimeoutError",
        "AgentCardResolutionError",
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "WriteTimeout",
    }
)

_CONNECTION_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
    }
)

_WORKER_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "AgentNotFoundError",
        "NoWorkerError",
        "WorkerNotFoundError",
    }
)


def exception_error_code(exc: BaseException) -> str:
    """Map an exception to an allowlisted framework error code.

    Exception objects are never JSON-serializable, so before any exception is
    stored into dispatch state / task events / status payloads it must be
    reduced to a code + message.  This returns the code.

    - A2A/transport layer exceptions (``A2AClientError``, httpx errors) map to
      ``network_error``.
    - Low-level connection failures map to ``connection_failed``.
    - Unresolvable/unreachable workers map to ``worker_unreachable``.
    - Anything else maps to ``unclassified_tool_error``.
    """
    module = type(exc).__module__ or ""
    name = type(exc).__name__
    if module.startswith("a2a") or name in _NETWORK_EXCEPTION_NAMES:
        return NETWORK_ERROR
    if module.startswith("httpx") or name in _CONNECTION_EXCEPTION_NAMES:
        return "connection_failed"
    if name in _WORKER_EXCEPTION_NAMES or "worker" in name.lower():
        return WORKER_UNREACHABLE
    return UNCLASSIFIED_TOOL_ERROR


def exception_to_safe_string(exc: BaseException) -> str:
    """Serialize an exception into a JSON-safe, allowlisted string.

    Format: ``<code>: <ExceptionType>: <message>``.  The leading token is an
    allowlisted framework code, so ``classify_error`` recovers it if the string
    ever flows through a failed ``ToolResult.error`` field.  The raw exception
    object never reaches the persistence boundary.
    """
    code = exception_error_code(exc)
    message = str(exc).strip() or type(exc).__name__
    return f"{code}: {type(exc).__name__}: {message}"

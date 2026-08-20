"""Phase 5 error taxonomy for failed ToolResult outcomes.

Pure classifier: maps the *structured* ``error`` field of a failed
``ToolResult`` to a public, allowlisted framework error code, or to one of two
sentinels:

- an allowlisted framework code (e.g. ``worker_busy``) when the error exactly
  names it (or carries an ``<allowlisted_code>: <detail>`` prefix),
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

from typing import Any

__all__ = [
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


def classify_error(error: str | None) -> str:
    """Classify a failed ToolResult's structured ``error`` into a public code.

    Args:
        error: The ``ToolResult.error`` field value (never ``content``).

    Returns:
        An allowlisted framework error code, ``unclassified_tool_error`` for
        non-empty unrecognized text, or ``missing_error_code`` when empty.
    """
    if error is None or not str(error).strip():
        return MISSING_ERROR_CODE
    candidate = str(error).strip()
    if candidate in FRAMEWORK_ERROR_CODES:
        return candidate
    # Structured errors may carry an ``<allowlisted_code>: <detail>`` prefix
    # (e.g. ``undeclared_task: node-1 not in MissionGraph.``); the leading
    # token is still an allowlisted code.  Only the structured error field is
    # inspected, never ``content``.
    prefix = candidate.split(":", 1)[0].strip()
    if prefix in FRAMEWORK_ERROR_CODES:
        return prefix
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

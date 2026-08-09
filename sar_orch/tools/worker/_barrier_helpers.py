"""Shared helpers for SAR barrier-based action tools."""

from __future__ import annotations

from typing import Any

from Agent.router_agent.tools.base import ToolResult

# Module-level publisher for observation dedup (set once per worker process).
# The publisher is created in sar_orch/worker.py and set here so all action
# tools can access it without threading through the agent construction chain.
_publisher: Any = None


def set_publisher(pub: Any) -> None:
    """Install a WorkerReportPublisher instance for the current worker process."""
    global _publisher
    _publisher = pub


def tool_result_from_barrier(result: dict, overrides: dict | None = None) -> ToolResult:
    """Build a ToolResult with both text content and structured observation data.

    Args:
        result: The dict returned by SARBarrier.submit_action().
               Must contain "observation" (str), and may contain
               "structured_observations", "structured_position",
               "structured_inventory".
        overrides: Optional dict with extra keys to merge into the
                   ToolResult (e.g., custom content text).

    Returns:
        ToolResult with content set to result["observation"] (or overrides)
        and data containing observations, position, and inventory.
    """
    content = result.get("observation", "")
    if overrides and "content" in overrides:
        content = overrides["content"]

    obs_list = result.get("structured_observations", [])
    error_detail = result.get("error_detail")
    data = None
    if obs_list or overrides or error_detail:
        data = {
            "observations": obs_list,
            "position": result.get("structured_position"),
            "inventory": result.get("structured_inventory"),
        }
        if error_detail:
            data["error_detail"] = error_detail
        if overrides:
            data.update(overrides)

    # Apply worker-side dedup via module-level publisher
    if _publisher is not None and data is not None:
        data = _publisher.apply_to_data(data)

    extra = {}
    if overrides:
        extra = {k: v for k, v in overrides.items() if k not in ("content", "error")}

    success = result.get("success", True)
    error = None
    if not success:
        error = result.get("error")
        if not error and overrides:
            error = overrides.get("error")
        if not error:
            error = "action_failed"

    return ToolResult(
        success=success,
        content=content,
        data=data,
        error=error,
        **extra,
    )

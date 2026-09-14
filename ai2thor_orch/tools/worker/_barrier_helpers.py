"""Shared helpers for AI2Thor barrier-based action tools.

形态对齐 SAR 版（``sar_orch/tools/worker/_barrier_helpers.py``）：目录型工具
注册表之外，barrier 消费面共用的小工具函数集中在这里。
"""

from __future__ import annotations

from typing import Any


def action_failure_error(fallback: str, result: Any) -> str:
    """Compose a failed action's ``ToolResult.error`` from the barrier result.

    ``AI2ThorBarrier.submit_action`` returns an ``ActionResult`` whose ``raw``
    carries the ai2thor event metadata; ``errorMessage`` / ``errorCode`` there
    are the *only* structured source of the real business failure
    (``"Target object not found within the specified visibility"``,
    ``"X is blocking Agent N from moving by ..."``, ``"EmptyHand"``, …).
    ``ToolResult.error`` is the field the framework error taxonomy
    (``Agent.error_taxonomy.classify_error``) parses, so surfacing the detail
    here is what lets those failures classify to their domain category
    (``object_not_visible`` / ``navigation_blocked`` / ``object_state_mismatch``)
    instead of falling back to ``unclassified_tool_error``.

    该文本只在 agent loop 内部流转（分类用）：failed ToolResult 在进入任何
    sink 之前就被归约为公开 ``error_code`` —— 原文既不进 LLM 上下文，也不
    进日志 / step callback / A2A 消息。

    Args:
        fallback: Tool-specific generic failure text
            (e.g. ``"Failed to pick up Apple_1"``).
        result: The ``ActionResult`` returned by
            ``AI2ThorBarrier.submit_action``.

    Returns:
        ``fallback`` when the result carries no detail; otherwise
        ``"<fallback>: <errorMessage>"`` with a ``" [<errorCode>]"`` tail when
        an error code is present and not already named in the message.
    """
    raw = getattr(result, "raw", None)
    metadata = raw if isinstance(raw, dict) else {}
    message = str(metadata.get("errorMessage") or "").strip()
    code = str(metadata.get("errorCode") or "").strip()
    if not message:
        return f"{fallback}: [{code}]" if code else fallback
    if code and code not in message:
        return f"{fallback}: {message} [{code}]"
    return f"{fallback}: {message}"


__all__ = ["action_failure_error"]

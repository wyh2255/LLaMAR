"""Shared helpers for AI2Thor barrier-based action tools.

形态对齐 SAR 版（``sar_orch/tools/worker/_barrier_helpers.py``）：目录型工具
注册表之外，barrier 消费面共用的小工具函数集中在这里。
"""

from __future__ import annotations

from typing import Any

from Agent.error_taxonomy import classify_error

#: 失败类目名（与 ``Agent.error_taxonomy`` 的域词表一致）。
_OBJECT_NOT_VISIBLE = "object_not_visible"

#: ``object_not_visible`` 类失败追加的行动指引（措辞与 worker prompt 的搜索
#: 协议同向：先 rotate/move 搜索再重试，禁止同一别名盲目重试）。
_OBJECT_NOT_VISIBLE_HINT = (
    "Not in view right now — rotate/move to search before retrying; "
    "do not retry the same alias blindly."
)


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
    (``object_not_visible`` / ``navigation_blocked`` / ``object_state_mismatch`` /
    ``camera_horizon_out_of_range``)
    instead of falling back to ``unclassified_tool_error``.

    该文本只在 agent loop 内部流转（分类用）：failed ToolResult 在进入任何
    sink 之前就被归约为公开 ``error_code`` —— 原文既不进 LLM 上下文，也不
    进日志 / step callback / A2A 消息。

    P0 失败文案可行动化（RP1b）：归类为 ``object_not_visible`` 的失败，文本
    尾部追加行动指引（先搜索再重试、禁止同名盲重试）。判定用
    :func:`Agent.error_taxonomy.classify_error` 查询既有分类结论 —— 归类
    规则零改动、不复制，指引后缀也不改变分类结论（test_tools 有回归钉子）。
    上一条边界依旧成立：worker LLM 侧可见的失败信号仍只有公开
    ``error_code``（``Error: object_not_visible``），配合 Environment State
    的 ``Visible now:`` 空提示；本指引随 error 文本保留，供分类器输入与直接
    消费 ToolResult 的调用面使用。

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
        text = f"{fallback}: [{code}]" if code else fallback
    elif code and code not in message:
        text = f"{fallback}: {message} [{code}]"
    else:
        text = f"{fallback}: {message}"
    # 失败文案可行动化（P0，RP1b）：归类为 object_not_visible 的失败在文本
    # 尾部追加行动指引。判定复用既有分类器（单一真源：只查询、不复制、不改
    # 归类规则），指引后缀不改变分类结论（test_tools 有回归钉子）。
    if classify_error(text) == _OBJECT_NOT_VISIBLE:
        text = f"{text} {_OBJECT_NOT_VISIBLE_HINT}"
    return text


__all__ = ["action_failure_error"]

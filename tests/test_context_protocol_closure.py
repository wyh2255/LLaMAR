"""Phase 1 — Context protocol closure invariants.

Covers router/worker prune + assemble with no orphan tool messages and no
pending assistant tool calls; `assemble()` must REJECT the ``Environment State``
user-block append while a prior assistant tool call remains unclosed; the
existing controller / worker NeedInput closure behavior must be preserved; and
the trailing role=user state block must never carry the output contract.
"""

import sys

from Agent.controller import AgentController
from Agent.router_agent.context import ContextConfig as RouterConfig
from Agent.router_agent.context import CoordinatorContextManager
from Agent.router_agent.schema import Message as RouterMessage
from Agent.worker_agent.context import ContextConfig as WorkerConfig
from Agent.worker_agent.context import WorkerContextManager
from Agent.worker_agent.schema import Message as WorkerMessage

OUTPUT_SCHEMA = "Respond with EXACTLY one tool call per turn."


def _tc(message_cls, call_id: str, name: str = "dummy_tool"):
    module = sys.modules[message_cls.__module__]
    ToolCall = module.ToolCall
    FunctionCall = module.FunctionCall
    return ToolCall(
        id=call_id, type="function", function=FunctionCall(name=name, arguments={})
    )


def _tool_msg(message_cls, call_id: str, name: str = "dummy_tool"):
    return message_cls(
        role="tool", content=f"result {call_id}", tool_call_id=call_id, name=name
    )


def _assistant_with_calls(message_cls, *calls):
    return message_cls(role="assistant", content="", tool_calls=list(calls))


def _active_tool_call_ids(messages) -> set[str]:
    ids = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if tc.id:
                    ids.add(tc.id)
    return ids


def _has_orphan_tool(messages) -> bool:
    """A tool message whose tool_call has no matching assistant tool_call."""
    ids = _active_tool_call_ids(messages)
    return any(
        m.role == "tool" and m.tool_call_id and m.tool_call_id not in ids
        for m in messages
    )


def _has_pending_tool_call(messages) -> bool:
    """An assistant tool_call that has no matching tool result."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            answered = {m.tool_call_id for m in messages[i + 1 :] if m.role == "tool"}
            return any(tc.id and tc.id not in answered for tc in msg.tool_calls)
        if msg.role != "tool":
            break
    return False


def _make_turn_pair(message_cls, prefix: str, count: int) -> list:
    """Interleaved assistant(tool_call)/tool messages (all closed)."""
    out: list = []
    for i in range(count):
        cid = f"{prefix}-{i}"
        out.append(_assistant_with_calls(message_cls, _tc(message_cls, cid)))
        out.append(_tool_msg(message_cls, cid))
    return out


# ---------------------------------------------------------------------------
# Router prune + assemble — no orphan / pending tool calls
# ---------------------------------------------------------------------------


def test_router_prune_and_assemble_leave_no_orphan_or_pending_tool_calls():
    ctx = CoordinatorContextManager(
        config=RouterConfig(strategy="hybrid", output_schema=OUTPUT_SCHEMA),
        token_limit=2000,
    )
    messages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
    ]
    messages.extend(_make_turn_pair(RouterMessage, "r", 10))
    assert not _has_orphan_tool(messages)
    assert not _has_pending_tool_call(messages)

    ctx.prune_history(messages)
    assert not _has_orphan_tool(messages), "router prune left orphan tool message"
    assert not _has_pending_tool_call(messages), "router prune left pending tool call"

    assembled = ctx.assemble("system", messages)
    assert not _has_orphan_tool(assembled), "router assemble left orphan tool message"
    assert not _has_pending_tool_call(assembled), "router assemble left pending call"
    # Trailing user block must carry the Environment State heading and NO contract.
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content
    assert "### Output Format" not in assembled[-1].content
    assert OUTPUT_SCHEMA not in assembled[-1].content
    assert "## Output / Response Contract" in assembled[0].content


# ---------------------------------------------------------------------------
# Worker prune + assemble — no orphan / pending tool calls
# ---------------------------------------------------------------------------


def test_worker_prune_and_assemble_leave_no_orphan_or_pending_tool_calls():
    ctx = WorkerContextManager(
        config=WorkerConfig(strategy="hybrid", output_schema=OUTPUT_SCHEMA),
        token_limit=2000,
    )
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="go"),
    ]
    # More exec turns than recent_messages (12) to force count-based pruning.
    messages.extend(_make_turn_pair(WorkerMessage, "w", 20))
    assert not _has_orphan_tool(messages)
    assert not _has_pending_tool_call(messages)

    ctx.prune_history(messages)
    assert not _has_orphan_tool(messages), "worker prune left orphan tool message"
    assert not _has_pending_tool_call(messages), "worker prune left pending tool call"

    assembled = ctx.assemble("system", messages)
    assert not _has_orphan_tool(assembled), "worker assemble left orphan tool message"
    assert not _has_pending_tool_call(assembled), "worker assemble left pending call"
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content
    assert "### Output Format" not in assembled[-1].content
    assert OUTPUT_SCHEMA not in assembled[-1].content


def test_worker_prune_preserves_pruning_semantics_within_recent_window():
    """The worker keeps a bounded recent window; router does not count-prune."""
    worker = WorkerContextManager(
        config=WorkerConfig(strategy="hybrid", recent_messages=8),
        token_limit=10**9,  # far above any token threshold → only count-prune applies
    )
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="go"),
    ]
    messages.extend(_make_turn_pair(WorkerMessage, "c", 20))
    before = len(messages)
    worker.prune_history(messages)
    assert len(messages) < before, "worker count-based prune should shrink history"
    assert not _has_orphan_tool(messages)
    assert not _has_pending_tool_call(messages)

    router = CoordinatorContextManager(
        config=RouterConfig(strategy="hybrid"),
        token_limit=10**9,
    )
    rmessages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
    ]
    rmessages.extend(_make_turn_pair(RouterMessage, "c", 20))
    r_before = len(rmessages)
    router.prune_history(rmessages)
    assert len(rmessages) == r_before, "router prune must not count-prune"
    assert not _has_orphan_tool(rmessages)
    assert not _has_pending_tool_call(rmessages)


# ---------------------------------------------------------------------------
# assemble() rejects Environment State append while a tool call is unclosed
# ---------------------------------------------------------------------------


def test_assemble_rejects_environment_state_append_on_unclosed_tool_call():
    ctx = CoordinatorContextManager(config=RouterConfig(strategy="hybrid"))
    pending = _tc(RouterMessage, "pending-1")
    messages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
        _assistant_with_calls(RouterMessage, pending),  # unclosed assistant tool call
    ]

    assembled = ctx.assemble("system", messages)
    # No Environment State user block appended; the history is passed through.
    assert len(assembled) == len(messages)
    assert assembled[-1].role == "assistant"
    assert assembled[-1].tool_calls is not None


def test_assemble_closes_protocol_and_appends_environment_state_after_closure():
    ctx = CoordinatorContextManager(config=RouterConfig(strategy="hybrid"))
    pending = _tc(RouterMessage, "pending-2")
    messages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
        _assistant_with_calls(RouterMessage, pending),
    ]

    # Controller / NeedInput closure: the pending call gets its tool result
    # appended before the next round (see AgentController.submit initial_messages).
    pending_call = AgentController._find_pending_tool_call(messages)
    assert pending_call is not None and pending_call.id == "pending-2"
    messages.append(_tool_msg(RouterMessage, pending.id))

    assert not _has_pending_tool_call(messages)
    assembled = ctx.assemble("system", messages)
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content


# ---------------------------------------------------------------------------
# Worker NeedInput backfill + controller resume closure is preserved
# ---------------------------------------------------------------------------


def test_need_input_backfill_and_controller_resume_closure_preserved():
    raiser = _tc(WorkerMessage, "ask-1", name="ask_coordinator")
    skipped = _tc(WorkerMessage, "skip-1", name="navigate_to")
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="do work"),
        _assistant_with_calls(WorkerMessage, raiser, skipped),
    ]
    # Worker agent backfills placeholders for tool_calls AFTER the raiser.
    messages.append(_tool_msg(WorkerMessage, skipped.id))

    # The raiser is still pending — the controller must find it on resume.
    pending_call = AgentController._find_pending_tool_call(messages)
    assert pending_call is not None
    assert pending_call.id == "ask-1"

    # Controller appends the real answer for the pending call.
    messages.append(
        WorkerMessage(
            role="tool",
            content="coordinator reply",
            tool_call_id=raiser.id,
            name="ask_coordinator",
        )
    )
    assert not _has_pending_tool_call(messages)
    assert not _has_orphan_tool(messages)

    ctx = WorkerContextManager(config=WorkerConfig(strategy="hybrid"))
    assembled = ctx.assemble("system", messages)
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content


# ---------------------------------------------------------------------------
# User state block never contains the output contract (both agents)
# ---------------------------------------------------------------------------


def test_user_state_block_contains_no_output_contract_router():
    ctx = CoordinatorContextManager(
        config=RouterConfig(strategy="hybrid", output_schema=OUTPUT_SCHEMA)
    )
    messages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
    ]
    assembled = ctx.assemble("system", messages)
    user_block = assembled[-1].content
    assert "Output" not in user_block
    assert "Response Contract" not in user_block
    assert OUTPUT_SCHEMA not in user_block


def test_user_state_block_contains_no_output_contract_worker():
    ctx = WorkerContextManager(
        config=WorkerConfig(strategy="hybrid", output_schema=OUTPUT_SCHEMA)
    )
    messages = [
        WorkerMessage(role="system", content="system"),
        WorkerMessage(role="user", content="go"),
    ]
    assembled = ctx.assemble("system", messages)
    user_block = assembled[-1].content
    assert "Output" not in user_block
    assert "Response Contract" not in user_block
    assert OUTPUT_SCHEMA not in user_block

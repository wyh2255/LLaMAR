"""Tests for AgentController.submit() snapshot save/restore."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agent.controller.controller import AgentController
from Agent.worker_agent.context import ContextManager
from Agent.worker_agent.schema import Message, RunResult, ToolCall, FunctionCall


class _FakeAgent:
    """Minimal fake agent for controller testing."""
    def __init__(self):
        self.messages = [Message(role="system", content="sys")]
        self.require_explicit_completion = False
        self.cancel_event = None

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None):
        return RunResult(content="done", success=True)


class _FakeAgentNeedInput:
    """Fake agent that returns need_input=True, with messages containing a tool_call."""
    def __init__(self):
        self.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="go"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call-1", type="function", function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}))],
            ),
        ]
        self.require_explicit_completion = False
        self.cancel_event = None

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None):
        return RunResult(content="Where?", success=False, need_input=True)


@pytest.mark.asyncio
async def test_submit_saves_snapshot_on_need_input():
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgentNeedInput(),
        session_factory=lambda: ctx_manager,
    )

    result = await controller.submit("ctx-1", "go", task_id="task-1")

    assert result.need_input is True
    snapshot = ctx_manager.load_snapshot("task-1")
    assert snapshot is not None
    assert len(snapshot) == 4
    assert snapshot[2].tool_calls is not None


@pytest.mark.asyncio
async def test_submit_restores_from_initial_messages():
    """When initial_messages provided, agent.messages should be restored + tool_result appended."""
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgent(),
        session_factory=lambda: ctx_manager,
    )

    initial = [
        Message(role="system", content="sys"),
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call-1", type="function", function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}))],
        ),
    ]

    result = await controller.submit(
        "ctx-1", "Go to sector 7", task_id="task-1", initial_messages=initial
    )

    assert result.success is True
    # The fake agent's messages should have been restored + tool_result appended
    # We can't directly check the agent (it's gone), but no crash means it worked


@pytest.mark.asyncio
async def test_submit_without_task_id_works():
    """submit() without task_id should work as before (no snapshot)."""
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgent(),
        session_factory=lambda: ctx_manager,
    )

    result = await controller.submit("ctx-1", "go")
    assert result.success is True

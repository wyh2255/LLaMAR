"""Tests for AgentController.submit() snapshot save/restore."""

import pytest

from Agent.controller.controller import AgentController
from Agent.worker_agent.context import ContextManager
from Agent.worker_agent.schema import Message, RunResult, ToolCall, FunctionCall


class _FakeAgent:
    """Minimal fake agent for controller testing."""

    last_instance = None

    def __init__(self):
        self.messages = [Message(role="system", content="sys")]
        self.require_explicit_completion = False
        self.cancel_event = None
        _FakeAgent.last_instance = self

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None, **kwargs):
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
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        type="function",
                        function=FunctionCall(
                            name="ask_coordinator", arguments={"question": "Where?"}
                        ),
                    )
                ],
            ),
        ]
        self.require_explicit_completion = False
        self.cancel_event = None

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None, **kwargs):
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
            tool_calls=[
                ToolCall(
                    id="call-1",
                    type="function",
                    function=FunctionCall(
                        name="ask_coordinator", arguments={"question": "Where?"}
                    ),
                )
            ],
        ),
    ]

    result = await controller.submit(
        "ctx-1", "Go to sector 7", task_id="task-1", initial_messages=initial
    )

    assert result.success is True
    # Verify agent's messages were restored + tool_result injected
    agent = _FakeAgent.last_instance
    assert agent is not None
    assert len(agent.messages) == 4
    assert agent.messages[0].role == "system"
    assert agent.messages[1].role == "user"
    assert agent.messages[2].role == "assistant"
    assert agent.messages[2].tool_calls is not None
    assert agent.messages[3].role == "tool"
    assert "Go to sector 7" in agent.messages[3].content


@pytest.mark.asyncio
async def test_submit_restores_from_initial_messages_without_tool_calls():
    """When initial_messages last message is NOT a tool_call, add_user_message is used."""
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgent(),
        session_factory=lambda: ctx_manager,
    )
    initial = [
        Message(role="system", content="sys"),
        Message(role="user", content="Previous query"),
    ]
    result = await controller.submit(
        "ctx-2", "Follow up", task_id="task-2", initial_messages=initial
    )
    assert result.success is True
    agent = _FakeAgent.last_instance
    assert agent is not None
    # Should have 3 messages: system, "Previous query", "Follow up"
    assert len(agent.messages) == 3
    assert agent.messages[0].role == "system"
    assert agent.messages[1].role == "user"
    assert agent.messages[1].content == "Previous query"
    assert agent.messages[2].role == "user"
    assert agent.messages[2].content == "Follow up"


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

"""Architecture A Phase 0 integration contracts."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import pytest

from a2a.client import ClientConfig, create_client
from a2a.coordinator.agent_executor import CoordinatorAgentExecutor
from a2a.coordinator.agent_registry import AgentRegistry
from a2a.coordinator.server import create_server
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import TaskState, TaskStatusUpdateEvent
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
from Agent.router_agent.schema import RunResult
from sar_orch.experiment import classify_end_reason


class RecordingEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise AssertionError(f"server did not become ready: {url}")


@pytest.mark.asyncio
async def test_controller_failure_is_a2a_failed_status_and_framework_error():
    registry = AgentRegistry()
    executor = CoordinatorAgentExecutor(registry=registry)

    async def fail_submit(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected relay failure")

    executor._controller.submit = fail_submit  # noqa: SLF001
    request = SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            parts=[Part(text="trigger failure")],
            task_id="task-failure",
            context_id="ctx-failure",
        )
    )
    context = RequestContext(
        call_context=ServerCallContext(),
        request=request,
        task_id="task-failure",
        context_id="ctx-failure",
    )
    queue = RecordingEventQueue()

    await executor.execute(context, queue)

    states = [
        event.status.state
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
    ]
    a2a_error = TaskState.TASK_STATE_FAILED in states
    assert a2a_error is True
    assert TaskState.TASK_STATE_COMPLETED not in states
    assert classify_end_reason(
        finished=False,
        steps=0,
        max_steps=20,
        elapsed_seconds=1.0,
        wall_clock_limit=60.0,
        a2a_done=False,
        a2a_error=a2a_error,
        coordinator_error=False,
    ) == "framework_error"


@pytest.mark.asyncio
async def test_returned_agent_failure_is_a2a_failed_status_without_completion():
    registry = AgentRegistry()
    executor = CoordinatorAgentExecutor(registry=registry)

    async def return_failed_result(*args: Any, **kwargs: Any) -> RunResult:
        return RunResult(
            content="LLM call failed after retry exhaustion",
            success=False,
        )

    executor._controller.submit = return_failed_result  # noqa: SLF001
    request = SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            parts=[Part(text="trigger returned failure")],
            task_id="task-returned-failure",
            context_id="ctx-returned-failure",
        )
    )
    context = RequestContext(
        call_context=ServerCallContext(),
        request=request,
        task_id="task-returned-failure",
        context_id="ctx-returned-failure",
    )
    queue = RecordingEventQueue()

    await executor.execute(context, queue)

    states = [
        event.status.state
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
    ]
    assert TaskState.TASK_STATE_FAILED in states
    assert TaskState.TASK_STATE_COMPLETED not in states


def _status_states(queue: RecordingEventQueue) -> list[Any]:
    return [
        event.status.state
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
    ]


async def _execute_with_submit_result(
    result: RunResult,
    *,
    task_id: str,
    context_id: str,
    text: str,
) -> list[Any]:
    registry = AgentRegistry()
    executor = CoordinatorAgentExecutor(registry=registry)

    async def return_result(*args: Any, **kwargs: Any) -> RunResult:
        return result

    executor._controller.submit = return_result  # noqa: SLF001
    request = SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            task_id=task_id,
            context_id=context_id,
        )
    )
    context = RequestContext(
        call_context=ServerCallContext(),
        request=request,
        task_id=task_id,
        context_id=context_id,
    )
    queue = RecordingEventQueue()
    await executor.execute(context, queue)
    return _status_states(queue)


@pytest.mark.asyncio
async def test_explicit_terminal_mission_failure_is_completed_not_framework_error():
    """finish_task(success=false) is honest mission terminal — not engine failure."""
    states = await _execute_with_submit_result(
        RunResult(
            content="[MISSION COMPLETE] could not rescue all persons",
            success=False,
            task_complete=True,
        ),
        task_id="task-mission-fail",
        context_id="ctx-mission-fail",
        text="finish with mission failure",
    )
    assert TaskState.TASK_STATE_COMPLETED in states
    assert TaskState.TASK_STATE_FAILED not in states
    assert classify_end_reason(
        finished=True,
        steps=5,
        max_steps=20,
        elapsed_seconds=10.0,
        wall_clock_limit=60.0,
        a2a_done=True,
        a2a_error=False,
        coordinator_error=False,
    ) != "framework_error"


@pytest.mark.asyncio
async def test_explicit_terminal_mission_success_remains_completed():
    states = await _execute_with_submit_result(
        RunResult(
            content="[MISSION COMPLETE] all fires extinguished",
            success=True,
            task_complete=True,
        ),
        task_id="task-mission-ok",
        context_id="ctx-mission-ok",
        text="finish with mission success",
    )
    assert TaskState.TASK_STATE_COMPLETED in states
    assert TaskState.TASK_STATE_FAILED not in states


@pytest.mark.asyncio
async def test_need_input_result_is_not_framework_error():
    """need_input remains exempt from framework_error classification."""
    states = await _execute_with_submit_result(
        RunResult(
            content="Need human clarification",
            success=False,
            need_input=True,
        ),
        task_id="task-need-input",
        context_id="ctx-need-input",
        text="need input",
    )
    assert TaskState.TASK_STATE_FAILED not in states


@pytest.mark.asyncio
async def test_router_finish_task_false_propagates_task_complete_on_run_result(tmp_path):
    """Typed seam: finish_task(success=false) → RunResult(task_complete=True, success=False)."""
    from Agent.router_agent.agent import Agent
    from Agent.router_agent.schema import FunctionCall, LLMResponse, ToolCall
    from Agent.router_agent.tools.base import ToolResult
    from sar_orch.tools.coordinator.finish_task import FinishTaskTool

    tool_call = ToolCall(
        id="call-finish",
        type="function",
        function=FunctionCall(
            name="finish_task",
            arguments={"success": False, "summary": "mission failed"},
        ),
    )

    class _FinishThenStopLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="Declaring mission failure",
                    tool_calls=[tool_call],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done", finish_reason="stop")

    class _FinishHooks:
        async def on_run_start(self, agent, user_message):
            return None

        async def on_run_end(self, agent, result):
            return None

        async def should_continue(self, agent, step):
            return not getattr(agent, "_task_complete", False)

        async def pre_llm(self, agent, messages):
            return messages

        async def post_llm(self, agent, response):
            return None

        async def pre_tool(self, agent, tool_name, args):
            return args

        async def post_tool(self, agent, tool_name, result: ToolResult):
            if result.task_complete:
                agent._task_complete = True
                agent._mission_success = result.mission_success
            return result

    agent = Agent(
        llm_client=_FinishThenStopLLM(),
        system_prompt="test",
        tools=[FinishTaskTool()],
        workspace_dir=str(tmp_path),
        max_steps=5,
        hooks=_FinishHooks(),
        require_explicit_completion=True,
    )

    result = await agent.run()

    assert result.task_complete is True
    assert result.success is False
    assert result.need_input is False


@pytest.mark.asyncio
async def test_router_llm_retry_failure_is_not_task_complete(tmp_path):
    from Agent.router_agent.agent import Agent
    from Agent.router_agent.retry import RetryExhaustedError

    class _FailingLLM:
        async def generate(self, messages, tools=None):
            raise RetryExhaustedError(RuntimeError("provider unavailable"), attempts=4)

    agent = Agent(
        llm_client=_FailingLLM(),
        system_prompt="test",
        tools=[],
        workspace_dir=str(tmp_path),
        max_steps=5,
    )

    result = await agent.run()

    assert result.success is False
    assert result.task_complete is False
    assert result.need_input is False


@pytest.mark.asyncio
async def test_controller_normalize_preserves_task_complete():
    from Agent.controller.controller import AgentController
    from Agent.router_agent.schema import RunResult as RouterRunResult

    raw = RouterRunResult(
        content="mission failed honestly",
        success=False,
        task_complete=True,
        steps_used=3,
    )
    normalized = AgentController._normalize(raw)
    assert normalized.success is False
    assert normalized.task_complete is True
    assert normalized.content == "mission failed honestly"


@pytest.mark.asyncio
async def test_real_streaming_shutdown_cross_loop_has_single_queue_finalizer(tmp_path):
    port = _free_port()
    a2a_port = _free_port()
    server = create_server(
        host="127.0.0.1",
        port=port,
        a2a_port=a2a_port,
        verifier_enabled=False,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    owner_exceptions: list[dict[str, Any]] = []

    def owner_exception_handler(loop, context):
        owner_exceptions.append(context)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        await _wait_for_http(
            f"http://127.0.0.1:{a2a_port}/.well-known/agent-card.json"
        )
        owner_loop = server._owner_loop  # noqa: SLF001
        assert owner_loop is not None
        owner_loop.call_soon_threadsafe(
            owner_loop.set_exception_handler, owner_exception_handler
        )

        async def block_controller(*args: Any, **kwargs: Any) -> Any:
            await asyncio.Event().wait()

        server._a2a_server.executor._controller.submit = block_controller  # noqa: SLF001
        client = await create_client(
            f"http://127.0.0.1:{a2a_port}/",
            ClientConfig(
                streaming=True,
                httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(10.0)),
            ),
        )
        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text="block until shutdown")],
            )
        )
        stream = client.send_message(request)
        first_event = await asyncio.wait_for(anext(stream), timeout=5.0)
        assert first_event is not None
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.1)

        await server.shutdown()
        await asyncio.wait_for(asyncio.to_thread(thread.join, 10.0), timeout=12.0)
        assert not thread.is_alive()
        assert server._server_task is None  # noqa: SLF001
        assert server._a2a_server is None  # noqa: SLF001
        assert not owner_exceptions

        if not pending_event.done():
            pending_event.cancel()
        try:
            await pending_event
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await stream.aclose()
        await client.close()
    finally:
        if thread.is_alive():
            await server.shutdown()
            await asyncio.to_thread(thread.join, 10.0)
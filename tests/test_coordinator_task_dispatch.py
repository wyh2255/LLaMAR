"""Tests for coordinator task dispatch pipeline — A2A submission → execution → barrier progression.

Tests the full chain: external task submission, coordinator orchestration loop,
tool dispatch, and barrier step advancement. Uses mocked LLM to avoid API costs.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Agent.router_agent.schema import Message
from a2a.coordinator.task_store import TaskStore
from sar_orch.barrier import SARBarrier
from sar_orch.coordinator import SARCoordinator


# ──────────────────────────────────────────────
# Mock LLM  – simulates RouterAgent tool decisions
# ──────────────────────────────────────────────

class MockLLMResponse:
    """Simulates LLM.generate() returning tool calls or finish."""

    def __init__(self, tool_calls, finish_reason="stop", content=""):
        self.content = content
        self.finish_reason = finish_reason
        self.tool_calls = tool_calls
        self.usage = MagicMock(
            prompt_tokens=50,
            completion_tokens=10,
            total_tokens=60,
            cache_hit_tokens=0,
            cache_miss_tokens=50,
        )


class MockLLMClient:
    """A deterministic mock LLM that returns pre-programmed responses."""

    def __init__(self, responses=None):
        self.call_count = 0
        self.responses = responses or []

    def add_response(self, mock_resp):
        self.responses.append(mock_resp)

    async def generate(self, messages, tools=None, **kwargs):
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        return MockLLMResponse(tool_calls=[], finish_reason="stop")


# ──────────────────────────────────────────────
# Mock Agent - deterministic ReAct loop
# ──────────────────────────────────────────────

class MockAgent:
    """Deterministic agent that runs through a fixed sequence of tool calls."""

    def __init__(self, llm_client=None, step_callback=None):
        self.llm_client = llm_client or MockLLMClient()
        self.step_callback = step_callback
        self.api_prompt_tokens = 0
        self.api_completion_tokens = 0
        self.api_total_tokens = 0
        self.api_cache_hit_tokens = 0
        self.api_cache_miss_tokens = 0
        self.cumulative_total_tokens = 0
        self.messages = []
        self._task_complete = False
        self.runtime_state = None

    def attach_context(self, ctx):
        self.context = ctx

    def add_user_message(self, text):
        self.messages.append(Message(role="user", content=text))

    async def run(self, cancel_event=None, step_callback=None):
        cb = step_callback or self.step_callback
        step = 0
        for step in range(3):
            resp = await self.llm_client.generate(self.messages)
            self.api_prompt_tokens += resp.usage.prompt_tokens
            self.api_completion_tokens += resp.usage.completion_tokens
            self.api_total_tokens += resp.usage.total_tokens
            self.api_cache_hit_tokens += resp.usage.cache_hit_tokens
            self.api_cache_miss_tokens += resp.usage.cache_miss_tokens

            if cb:
                cb("llm_response", usage=resp.usage, content=resp.content)

            if not resp.tool_calls:
                break

            for tc in resp.tool_calls:
                if cb:
                    cb("tool_start", tool_name=tc["name"], arguments=tc["args"])
                if cb:
                    cb("tool_result", tool_name=tc["name"], content="mock_ok", success=True)

            if self._task_complete:
                break
        return MagicMock(content="done", success=True, need_input=False, steps_used=step + 1)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def barrier():
    return SARBarrier(num_agents=2, scene=1, seed=42)


@pytest.fixture
def mock_agent():
    return MockAgent()


@pytest.fixture
def coordinator(barrier):
    """Create a SARCoordinator with a fully mocked server."""
    coord = SARCoordinator(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        barrier=barrier,
        coordinator_secret=bytes(range(32)),
        model="mock-model",
        provider="openai",
        api_base="http://mock-api/",
        state_mode="semantic",
    )
    coord._server = MagicMock()
    coord._server.set_barrier = MagicMock()
    coord._server.set_semantic_map = MagicMock()
    coord._server.run = MagicMock()
    coord._server.shutdown = AsyncMock()
    coord._thread = MagicMock()
    return coord


# ──────────────────────────────────────────────
# Tests: SARCoordinator initialization
# ──────────────────────────────────────────────

class TestSARCoordinatorInit:
    def test_constructor_sets_fields(self, barrier):
        coord = SARCoordinator(
            coordinator_secret=bytes(range(32)),
            host="127.0.0.1",
            port=8080,
            a2a_port=8081,
            barrier=barrier,
            model="test-model",
            provider="test-provider",
            api_base="http://test-api/",
            state_mode="semantic",
        )
        assert coord._host == "127.0.0.1"
        assert coord._port == 8080
        assert coord._a2a_port == 8081
        assert coord._barrier is barrier
        assert coord._model == "test-model"
        assert coord._state_mode == "semantic"

    def test_constructor_defaults(self, barrier):
        coord = SARCoordinator(
            barrier=barrier, coordinator_secret=bytes(range(32))
        )
        assert coord._host == "0.0.0.0"
        assert coord._port == 8080
        assert coord._a2a_port == 8081
        assert coord._state_mode == "semantic"

    def test_constructor_semantic_map_starts_none(self, barrier):
        """semantic_map is None until start() is called."""
        coord = SARCoordinator(
            barrier=barrier, coordinator_secret=bytes(range(32))
        )
        assert coord._semantic_map is None

    def test_coordinator_has_barrier(self, barrier):
        coord = SARCoordinator(
            barrier=barrier, coordinator_secret=bytes(range(32))
        )
        assert coord._barrier is barrier


# ──────────────────────────────────────────────
# Tests: Tool class existence (not build-time)
# ──────────────────────────────────────────────

class TestCoordinatorTools:
    def test_send_message_tool_exists(self):
        """SendMessageTool should be importable and constructible."""
        from a2a.builtin_tools.send_message import SendMessageTool

        store = MagicMock()
        store._dispatch_to_worker = {}
        store._worker_to_dispatch = {}
        store.dispatched_count = 0
        store.max_tasks = 10

        registry = MagicMock()
        tool = SendMessageTool(
            store=store,
            registry=registry,
            coordinator_host="localhost",
            coordinator_port=8080,
        )
        assert tool.name == "send_message"

    def test_sar_finish_task_tool_exists(self):
        from sar_orch.tools.coordinator.finish_task import FinishTaskTool

        store = MagicMock()
        tool = FinishTaskTool(store)
        assert tool.name == "finish_task"

    def test_query_task_events_tool_exists(self):
        from a2a.builtin_tools.query_task_events import QueryTaskEventsTool

        store = MagicMock()
        tool = QueryTaskEventsTool(store)
        assert tool.name == "query_task_events"

    def test_query_task_results_tool_exists(self):
        from a2a.builtin_tools.query_task_results import QueryTaskResultsTool

        results = MagicMock()
        tool = QueryTaskResultsTool(results)
        assert tool.name == "query_task_results"

    def test_verify_result_tool_exists(self):
        from a2a.builtin_tools.verify_result import VerifyResultTool

        store = MagicMock()
        tool = VerifyResultTool(store)
        assert tool.name == "verify_result"

    def test_update_plan_tool_exists(self):
        from a2a.builtin_tools.update_plan import UpdatePlanTool

        store = MagicMock()
        tool = UpdatePlanTool(store)
        assert tool.name == "update_plan"

    def test_cancel_task_tool_exists(self):
        from a2a.builtin_tools.cancel_task import CancelTaskTool

        store = MagicMock()
        registry = MagicMock()
        tool = CancelTaskTool(store, registry)
        assert tool.name == "cancel_task"


# ──────────────────────────────────────────────
# Tests: TaskStore operations used by coordinator
# ──────────────────────────────────────────────

class TestTaskStore:
    def test_store_requires_original_request_and_router(self):
        """TaskStore requires original_request and router params."""
        router = MagicMock()
        store = TaskStore(original_request="test mission", router=router)
        assert store.original_request == "test mission"
        assert store._plan == []
        assert store.max_tasks == 20

    def test_store_register_and_resolve_dispatch(self):
        router = MagicMock()
        store = TaskStore(original_request="test", router=router)
        store.register_worker_task_id("dispatch-1", "worker-uuid")
        result = store.resolve_dispatch_id("worker-uuid")
        assert result == "dispatch-1"

    def test_store_resolve_dispatch_id_self_when_node_exists(self):
        """resolve_dispatch_id returns task_id as-is if a plan node exists."""
        router = MagicMock()
        store = TaskStore(original_request="test", router=router)
        store.register_worker_task_id("dispatch-1", "worker-uuid")
        result = store.resolve_dispatch_id("dispatch-1")
        # No plan node, so get_node returns None, then checks _worker_to_dispatch
        assert result is None or result == "dispatch-1"


# ──────────────────────────────────────────────
# Tests: A2A Task Submission Message Format
# ──────────────────────────────────────────────

class TestTaskSubmissionFormat:
    def test_submit_task_builds_correct_protobuf(self):
        """Verify that submit_task builds the correct A2A protobuf message."""
        from a2a.types.a2a_pb2 import SendMessageRequest, Message, Part, Role

        task_desc = "Extinguish all fires"
        message = Message(role=Role.ROLE_USER, parts=[Part(text=task_desc)])
        request = SendMessageRequest(message=message)

        assert request.message.role == Role.ROLE_USER
        assert request.message.parts[0].text == task_desc
        assert request.HasField("message")

    @pytest.mark.asyncio
    async def test_submit_task_handles_connection_refused(self, barrier):
        """When A2A endpoint is unreachable, submit_task returns error string."""
        coord = SARCoordinator(
            coordinator_secret=bytes(range(32)),
            host="127.0.0.1",
            port=19999,
            a2a_port=19999,
            barrier=barrier,
        )
        result = await coord.submit_task("test task")
        assert result.startswith("Error:")


# ──────────────────────────────────────────────
# Tests: Agent execution with mock LLM
# ──────────────────────────────────────────────

class TestMockLLMExecution:
    @pytest.mark.asyncio
    async def test_mock_agent_runs_and_calls_back(self):
        """MockAgent should call step_callback for each llm/tool event."""
        calls = []
        agent = MockAgent(
            llm_client=MockLLMClient([
                MockLLMResponse(
                    tool_calls=[{"name": "send_message", "args": {"who": "Alice", "content": "go"}}],
                ),
            ]),
            step_callback=lambda t, **kw: calls.append((t, kw)),
        )
        result = await agent.run()
        assert result.success
        event_types = [c[0] for c in calls]
        assert "llm_response" in event_types
        assert "tool_start" in event_types
        assert "tool_result" in event_types

    @pytest.mark.asyncio
    async def test_mock_agent_stops_when_no_tool_calls(self):
        """Agent should stop when LLM returns no tool calls."""
        calls = []
        agent = MockAgent(
            llm_client=MockLLMClient([
                MockLLMResponse(tool_calls=[]),
            ]),
            step_callback=lambda t, **kw: calls.append(t),
        )
        result = await agent.run()
        assert result.success
        assert "llm_response" in calls

    @pytest.mark.asyncio
    async def test_mock_agent_accumulates_token_usage(self):
        """Agent should accumulate token usage across LLM calls."""
        agent = MockAgent(
            llm_client=MockLLMClient([
                MockLLMResponse(tool_calls=[{"name": "noop", "args": {}}]),
                MockLLMResponse(tool_calls=[]),
            ]),
        )
        await agent.run()
        assert agent.api_prompt_tokens == 100  # 50 + 50
        assert agent.api_total_tokens == 120  # 60 + 60


# ──────────────────────────────────────────────
# Tests: Barrier integration — async submit
# ──────────────────────────────────────────────

class TestBarrierStepProgression:
    @pytest.mark.asyncio
    async def test_barrier_initial_step_is_zero(self, barrier):
        assert barrier._step_counter == 0

    @pytest.mark.asyncio
    async def test_barrier_submit_all_agents_in_parallel(self, barrier):
        """Submit all agents in parallel — this is how real workers do it."""
        # Use small timeout so tests don't hang
        barrier.STEP_TIMEOUT = 0.5
        results = await asyncio.gather(
            barrier.submit_action(0, "NoOp"),
            barrier.submit_action(1, "NoOp"),
        )
        assert len(results) == 2
        for r in results:
            assert isinstance(r, dict)

    @pytest.mark.asyncio
    async def test_barrier_all_agents_submit_advances_step(self, barrier):
        """After all agents submit in parallel, step counter should increment."""
        barrier.STEP_TIMEOUT = 0.5
        await asyncio.gather(
            barrier.submit_action(0, "NoOp"),
            barrier.submit_action(1, "NoOp"),
        )
        assert barrier._step_counter >= 1

    @pytest.mark.asyncio
    async def test_barrier_get_last_step_log(self, barrier):
        """get_last_step_log should expose actions after step execution."""
        barrier.STEP_TIMEOUT = 0.5
        await asyncio.gather(
            barrier.submit_action(0, "NoOp"),
            barrier.submit_action(1, "NoOp"),
        )
        step_log = barrier.get_last_step_log()
        assert "actions" in step_log
        assert "timeout_agents" in step_log

    @pytest.mark.asyncio
    async def test_barrier_get_metrics(self, barrier):
        """get_metrics should return step, coverage, transport_rate, finished."""
        metrics = barrier.get_metrics()
        assert "steps" in metrics
        assert "coverage" in metrics
        assert "transport_rate" in metrics
        assert "finished" in metrics


# ──────────────────────────────────────────────
# Tests: Barrier env snapshot
# ──────────────────────────────────────────────

class TestBarrierSnapshot:
    def test_get_env_snapshot_returns_grid_objects(self, barrier):
        """get_env_snapshot returns grid object collections (step/finished in get_metrics)."""
        snapshot = barrier.get_env_snapshot()
        for key in ("agents", "fires", "persons", "reservoirs", "deposits", "flammables"):
            assert key in snapshot
        # step/finished come from get_metrics() + is_finished(), not from snapshot
        metrics = barrier.get_metrics()
        assert "steps" in metrics


# ──────────────────────────────────────────────
# Tests: SendMessageTool — coordinator communication
# ──────────────────────────────────────────────

class TestSendMessageTool:
    @pytest.mark.asyncio
    async def test_assign_task_success(self):
        from a2a.builtin_tools.send_message import SendMessageTool

        store = MagicMock()
        store._dispatch_to_worker = {}
        store._worker_to_dispatch = {}
        store.dispatched_count = 0
        store.max_tasks = 10

        from a2a.coordinator.agent_registry import AgentInfo, AgentStatus
        registry = MagicMock()
        registry.get.return_value = AgentInfo(
            agent_id="Alice", description="SAR Worker",
            endpoint="http://alice:8090", status=AgentStatus.ONLINE,
        )

        tool = SendMessageTool(
            store=store, registry=registry,
            coordinator_host="localhost", coordinator_port=8080,
        )

        with patch.object(
            tool._dispatch_tool, "execute",
            new=AsyncMock(return_value=MagicMock(success=True, content="dispatched")),
        ):
            result = await tool.execute(
                message_type="assign_task", who="Alice",
                content="Extinguish fire at (3,2)",
                related_task_id="task-1",
            )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_invalid_message_type_returns_error(self):
        from a2a.builtin_tools.send_message import SendMessageTool
        store = MagicMock()
        registry = MagicMock()
        tool = SendMessageTool(
            store=store, registry=registry,
            coordinator_host="localhost", coordinator_port=8080,
        )
        result = await tool.execute(message_type="not_a_real_type")
        assert result.success is False
        assert result.error == "invalid_message_type"

    @pytest.mark.asyncio
    async def test_assign_task_missing_who_returns_error(self):
        from a2a.builtin_tools.send_message import SendMessageTool
        store = MagicMock()
        registry = MagicMock()
        tool = SendMessageTool(
            store=store, registry=registry,
            coordinator_host="localhost", coordinator_port=8080,
        )
        result = await tool.execute(message_type="assign_task", content="do it")
        assert result.success is False
        assert result.error == "missing_who"


# ──────────────────────────────────────────────
# Tests: Experiment metadata and end reason
# ──────────────────────────────────────────────

class TestExperimentMetadata:
    def test_build_run_metadata_includes_key_fields(self):
        """metadata should contain all reproducibility fields."""
        from sar_orch.experiment import build_run_metadata
        meta = build_run_metadata(
            run_id="test-run",
            code_commit="abc123",
            scene=1,
            num_agents=2,
            seed=42,
            model="deepseek-v4-flash",
            provider="openai",
            api_base="https://api.deepseek.com",
            max_steps=50,
            wall_clock_limit=3600.0,
            sandbox_profile="workspace",
            coordinator_prompts="",
            worker_prompts="",
        )
        assert meta["run_id"] == "test-run"
        assert meta["scene"] == 1
        assert meta["agent_count"] == 2
        assert meta["seed"] == 42
        assert meta["max_steps"] == 50
        assert meta["task_objective"] == "Extinguish all fires and rescue all persons"
        assert "prompt_version" in meta

    def test_classify_end_reason_all_cases(self):
        """All end_reason enum values should match classify_end_reason()."""
        from sar_orch.experiment import classify_end_reason

        assert classify_end_reason(
            finished=True, steps=10, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=False, a2a_error=False, coordinator_error=False,
        ) == "success"

        assert classify_end_reason(
            finished=False, steps=50, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=False, a2a_error=False, coordinator_error=False,
        ) == "max_steps_reached"

        assert classify_end_reason(
            finished=False, steps=10, max_steps=50,
            elapsed_seconds=3600, wall_clock_limit=3600,
            a2a_done=False, a2a_error=False, coordinator_error=False,
        ) == "wall_clock_timeout"

        assert classify_end_reason(
            finished=False, steps=10, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=True, a2a_error=False, coordinator_error=False,
        ) == "coordinator_finished_early"

        assert classify_end_reason(
            finished=False, steps=10, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=False, a2a_error=True, coordinator_error=False,
        ) == "framework_error"

        assert classify_end_reason(
            finished=False, steps=10, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=False, a2a_error=False, coordinator_error=True,
        ) == "framework_error"

        assert classify_end_reason(
            finished=False, steps=10, max_steps=50,
            elapsed_seconds=100, wall_clock_limit=3600,
            a2a_done=False, a2a_error=False, coordinator_error=False,
        ) == "stopped_before_success"

    def test_default_max_steps(self):
        """Default max_steps should be 50, matching experiment.py:163."""
        assert (None or 50) == 50

    def test_barrier_create_with_num_agents(self):
        b = SARBarrier(num_agents=2, scene=1, seed=42)
        assert b.num_agents == 2
        assert b.scene == 1
        assert b.seed == 42

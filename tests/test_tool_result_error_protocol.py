"""Phase 5 tool-result error protocol tests.

Covers:
- ``Agent.error_taxonomy``: pure classifier mapping a failed ToolResult's
  structured error to an allowlisted framework code, ``unclassified_tool_error``
  or ``missing_error_code`` (never parsing ``content="Error: ..."``). The
  allowlist includes MissionRuntime graph codes, ``invalid_plan`` and
  ``action_failed``.
- Router/worker Agents: public ``error_code`` produced before redaction and
  passed with the ``tool_result`` event after redaction; the raw error text
  never enters Message / logger / [DATA].
- ``sar_orch`` producers: worker ``log_agent_interaction`` and coordinator
  ``log_router_interaction`` record ``{Success, ErrorType}`` outcome rows;
  barrier-backed worker tools surface a structured code on failure.
- ``sar_orch/eval/memory_acceptance`` aggregator: positive count for injected
  ``worker_busy`` failures; nonzero exit with ``instrumentation_missing`` when
  any failed outcome lacks an ErrorType.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from Agent.error_taxonomy import (
    FRAMEWORK_ERROR_CODES,
    MISSING_ERROR_CODE,
    REPORTED_FRAMEWORK_ERROR_CODES,
    UNCLASSIFIED_TOOL_ERROR,
    classify_error,
    error_code_for_result,
)

WORKER_BUSY = "worker_busy"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class _FakeBarrier:
    _step_counter = 3


# ---------------------------------------------------------------------------
# Pure taxonomy
# ---------------------------------------------------------------------------


def test_classify_allowlisted_framework_code():
    assert classify_error("worker_busy") == "worker_busy"
    assert classify_error("task_not_routable_yet") == "task_not_routable_yet"
    assert classify_error("unknown_task_id") == "unknown_task_id"
    assert classify_error("  worker_busy  ") == "worker_busy"
    # Structured errors with an allowlisted prefix still classify as the code.
    assert classify_error("unknown_task_id: dispatch-7 not found.") == "unknown_task_id"


def test_classify_unclassified_nonempty_error():
    assert classify_error("Worker already has an active task") == UNCLASSIFIED_TOOL_ERROR
    assert classify_error("boom: something unexpected") == UNCLASSIFIED_TOOL_ERROR


def test_classify_missing_error_code_when_empty():
    assert classify_error("") == MISSING_ERROR_CODE
    assert classify_error("   ") == MISSING_ERROR_CODE
    assert classify_error(None) == MISSING_ERROR_CODE


def test_allowlist_contains_reported_codes():
    for code in REPORTED_FRAMEWORK_ERROR_CODES:
        assert code in FRAMEWORK_ERROR_CODES


def test_allowlist_contains_mission_runtime_graph_codes():
    """MissionRuntime codes passed through activate_plan_node must be known."""
    for code in (
        "node_not_found",
        "node_not_ready",
        "dependency_incomplete",
        "participant_busy",
        "dispatch_acceptance_failed",
        "dispatch_allocation_failed",
        "team_preparation_failed",
        "team_ack_failed",
        "team_setup_failed",
        "mission_runtime_aborted",
    ):
        assert code in FRAMEWORK_ERROR_CODES
        assert classify_error(code) == code
        assert classify_error(f"{code}: some detail") == code


def test_allowlist_contains_invalid_plan_and_action_failed():
    assert "invalid_plan" in FRAMEWORK_ERROR_CODES
    assert "plan_update_failed" in FRAMEWORK_ERROR_CODES
    assert "action_failed" in FRAMEWORK_ERROR_CODES
    assert classify_error("invalid_plan") == "invalid_plan"
    assert classify_error("action_failed") == "action_failed"


def test_allowlist_contains_dispatch_assign_verify_codes():
    """Static codes added for the remaining dynamic error strings in
    dispatch_task / assign_task / verify_result must be allowlisted and
    classify exactly (including the ``<code>: <detail>`` prefix form)."""
    for code in (
        "max_tasks_reached",
        "undeclared_task",
        "planned_worker_mismatch",
        "worker_not_found",
        "connection_failed",
        "node_not_found",
        "no_output_available",
        "verification_failed",
        "verifier_not_configured",
    ):
        assert code in FRAMEWORK_ERROR_CODES
        assert classify_error(code) == code
        assert classify_error(f"{code}: some detail") == code


def _make_store():
    from a2a.coordinator.task_store import TaskStore

    return TaskStore("request", router=None)


def test_dispatch_task_returns_static_allowlisted_codes():
    import asyncio

    from a2a.builtin_tools.dispatch_task import DispatchTaskTool

    class RouterMock:
        async def send_task_async(
            self, agent_id, prompt, callback_url, task_id, context_id=None
        ):
            return f"wt-{task_id}"

    # max_tasks_reached
    store = _make_store()
    store.max_tasks = 0
    store._router = RouterMock()
    tool = DispatchTaskTool(store, coordinator_host="localhost", coordinator_port=8080)
    result = asyncio.run(tool.execute(agent_id="Alice", prompt="go"))
    assert result.success is False
    assert result.error == "max_tasks_reached"
    assert result.error in FRAMEWORK_ERROR_CODES
    assert error_code_for_result(result) == "max_tasks_reached"

    # undeclared_task
    store = _make_store()
    store._router = RouterMock()
    store.replace_mission_graph(
        [{"task_id": "declared", "participant_ids": ["Alice"]}]
    )
    tool = DispatchTaskTool(store, coordinator_host="localhost", coordinator_port=8080)
    result = asyncio.run(
        tool.execute(agent_id="Alice", prompt="go", task_id="undeclared")
    )
    assert result.success is False
    assert result.error == "undeclared_task"
    assert error_code_for_result(result) == "undeclared_task"
    assert "MissionGraph" in (result.content or "")

    # planned_worker_mismatch
    store = _make_store()
    store._router = RouterMock()
    store.replace_mission_graph(
        [{"task_id": "declared", "participant_ids": ["Alice"]}]
    )
    tool = DispatchTaskTool(store, coordinator_host="localhost", coordinator_port=8080)
    result = asyncio.run(
        tool.execute(agent_id="Bob", prompt="go", task_id="declared")
    )
    assert result.success is False
    assert result.error == "planned_worker_mismatch"
    assert error_code_for_result(result) == "planned_worker_mismatch"
    assert "participant" in (result.content or "").lower()


def test_assign_task_returns_static_allowlisted_codes():
    import asyncio

    from a2a.builtin_tools.assign_task import AssignTaskTool
    from a2a.coordinator.agent_registry import (
        AgentInfo,
        AgentNotFoundError,
        AgentRegistry,
    )

    class _FailingRegistry:
        def get(self, agent_id):
            raise AgentNotFoundError(agent_id)

    tool = AssignTaskTool(registry=_FailingRegistry(), sdk_clients={})
    result = asyncio.run(tool.execute(agent_id="Ghost", prompt="go"))
    assert result.success is False
    assert result.error == "worker_not_found"
    assert error_code_for_result(result) == "worker_not_found"

    # connection_failed: registry resolves but the worker transport raises.
    class _BoomClient:
        def send_message(self, request):
            async def _agen():
                raise ConnectionError("refused")
                yield None  # pragma: no cover

            return _agen()

    registry = AgentRegistry()
    registry.register(
        AgentInfo(
            agent_id="Alice",
            description="t",
            endpoint="http://localhost:9999/",
            capabilities=["sar"],
        )
    )
    tool2 = AssignTaskTool(registry=registry, sdk_clients={"Alice": _BoomClient()})
    result = asyncio.run(tool2.execute(agent_id="Alice", prompt="go"))
    assert result.success is False
    assert result.error == "connection_failed"
    assert error_code_for_result(result) == "connection_failed"


def test_verify_result_returns_static_allowlisted_codes():
    import asyncio

    from a2a.builtin_tools.verify_result import VerifyResultTool

    # verifier_not_configured (no verifier attached)
    store = _make_store()
    store._verifier = None
    tool = VerifyResultTool(store)
    result = asyncio.run(tool.execute(task_id="task-1"))
    assert result.success is False
    assert result.error == "verifier_not_configured"
    assert error_code_for_result(result) == "verifier_not_configured"

    # node_not_found (task not in plan)
    store = _make_store()
    store._verifier = object()
    tool = VerifyResultTool(store)
    result = asyncio.run(tool.execute(task_id="missing"))
    assert result.success is False
    assert result.error == "node_not_found"
    assert error_code_for_result(result) == "node_not_found"

    # no_output_available (node exists but has no collected output)
    store = _make_store()
    store.update_plan([{"task_id": "task-1", "description": "desc"}])
    store._verifier = object()
    tool = VerifyResultTool(store)
    result = asyncio.run(tool.execute(task_id="task-1"))
    assert result.success is False
    assert result.error == "no_output_available"
    assert error_code_for_result(result) == "no_output_available"

    # verification_failed (verifier raises)
    class _BoomVerifier:
        async def verify(self, **kwargs):
            raise RuntimeError("verifier exploded")

    store = _make_store()
    store.update_plan([{"task_id": "task-1", "description": "desc"}])
    store._results["task-1"] = "some output"
    store._verifier = _BoomVerifier()
    tool = VerifyResultTool(store)
    result = asyncio.run(tool.execute(task_id="task-1"))
    assert result.success is False
    assert result.error == "verification_failed"
    assert error_code_for_result(result) == "verification_failed"
    assert "verifier exploded" in (result.content or "")


def test_error_code_for_result_success_is_empty():
    from Agent.worker_agent.tools.base import ToolResult

    result = ToolResult(success=True, content="ok", error="ignored")
    assert error_code_for_result(result) == ""


def test_error_code_for_result_failed():
    from Agent.worker_agent.tools.base import ToolResult

    assert error_code_for_result(ToolResult(success=False, error="worker_busy")) == WORKER_BUSY
    assert error_code_for_result(ToolResult(success=False, error="")) == MISSING_ERROR_CODE
    assert error_code_for_result(ToolResult(success=False, error=None)) == MISSING_ERROR_CODE
    assert error_code_for_result(ToolResult(success=False, error="weird thing")) == (
        UNCLASSIFIED_TOOL_ERROR
    )


def test_error_code_never_parses_content():
    """The taxonomy inspects the structured error field, never content."""
    from Agent.worker_agent.tools.base import ToolResult

    result = ToolResult(
        success=False,
        content="Error: worker_busy",
        error="",
    )
    assert error_code_for_result(result) == MISSING_ERROR_CODE


# ---------------------------------------------------------------------------
# Router / worker Agent protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_agent_passes_error_code_and_hides_raw_error(tmp_path):
    from Agent.router_agent.agent import Agent
    from Agent.router_agent.schema import FunctionCall, LLMResponse, ToolCall
    from Agent.router_agent.tools.base import Tool, ToolResult

    RAW_ERROR = "boom: Authorization: Bearer hunter2-raw-secret"

    class _BadTool(Tool):
        name = "bad_tool"
        description = "bad"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=False, content="", error=RAW_ERROR)

    class _LLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        type="function",
                        function=FunctionCall(name="bad_tool", arguments={}),
                    )
                ],
                finish_reason="tool_calls",
            )

    captured: dict = {}

    async def step_cb(type_, **data):
        if type_ == "tool_result":
            captured.update(data)

    agent = Agent(
        llm_client=_LLM(),
        system_prompt="test",
        tools=[_BadTool()],
        workspace_dir=str(tmp_path),
        max_steps=1,
        log_dir=str(tmp_path / "logs"),
    )
    await agent.run(step_callback=step_cb)

    assert captured.get("success") is False
    assert captured.get("error_code") == UNCLASSIFIED_TOOL_ERROR
    # Raw error text must not enter the step_callback event / Message / logger.
    assert "hunter2-raw-secret" not in (captured.get("content") or "")
    history_text = json.dumps([m.content for m in agent.messages])
    assert "hunter2-raw-secret" not in history_text
    log_file = agent.logger.get_log_file_path()
    if log_file is not None:
        assert "hunter2-raw-secret" not in log_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_agent_passes_error_code_and_hides_raw_error(tmp_path):
    from Agent.worker_agent.agent import Agent
    from Agent.worker_agent.schema import FunctionCall, LLMResponse, ToolCall
    from Agent.worker_agent.tools.base import Tool, ToolResult

    RAW_ERROR = "worker-boom Authorization: Bearer hunter2-worker-secret"

    class _BadTool(Tool):
        name = "bad_tool"
        description = "bad"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=False, content="", error=RAW_ERROR)

    class _LLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        type="function",
                        function=FunctionCall(name="bad_tool", arguments={}),
                    )
                ],
                finish_reason="tool_calls",
            )

    captured: dict = {}

    async def step_cb(type_, **data):
        if type_ == "tool_result":
            captured.update(data)

    agent = Agent(
        llm_client=_LLM(),
        system_prompt="test",
        tools=[_BadTool()],
        workspace_dir=str(tmp_path),
        max_steps=1,
        log_dir=str(tmp_path / "logs"),
    )
    await agent.run(step_callback=step_cb)

    assert captured.get("success") is False
    assert captured.get("error_code") == UNCLASSIFIED_TOOL_ERROR
    assert "hunter2-worker-secret" not in (captured.get("content") or "")
    history_text = json.dumps([m.content for m in agent.messages])
    assert "hunter2-worker-secret" not in history_text
    log_file = agent.logger.get_log_file_path()
    if log_file is not None:
        assert "hunter2-worker-secret" not in log_file.read_text(encoding="utf-8")


def test_worker_a2a_sink_data_never_contains_raw_error():
    """The [DATA] block emitted for a failed tool_result carries only the
    public error_code, never the raw error text."""
    import asyncio

    from a2a.worker.sink import A2AWorkerSink

    raw_error = "boom: hmac=0123456789abcdef0123456789abcdef"
    captured: list = []

    class _Queue:
        async def enqueue_event(self, event):
            captured.append(event)

    sink = A2AWorkerSink(event_queue=_Queue(), task_id="t", context_id="ctx")
    asyncio.run(
        sink.emit(
            "tool_result",
            tool_name="bad_tool",
            success=False,
            content="Error: unclassified_tool_error",
            data=None,
            error_code=UNCLASSIFIED_TOOL_ERROR,
        )
    )
    assert captured, "enqueue_event must be called"
    text = captured[0].status.message.parts[0].text
    assert raw_error not in text
    assert "unclassified_tool_error" in text


# ---------------------------------------------------------------------------
# Producers: worker agent_interactions.csv
# ---------------------------------------------------------------------------


def test_worker_producer_records_success_and_error_code(tmp_path):
    from sar_orch.logger import ExperimentLogger
    from sar_orch.worker import SARWorker

    logger = ExperimentLogger(log_dir=str(tmp_path))
    worker = SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    worker._on_step_event("tool_start", tool_name="send_message", arguments={"who": "Bob"})
    worker._on_step_event(
        "tool_result",
        tool_name="send_message",
        success=False,
        content="Error: worker_busy",
        error_code=WORKER_BUSY,
    )
    logger.close()

    rows = read_csv(tmp_path / "agent_interactions.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["EventType"] == "tool_result"
    assert row["Success"] == "False"
    assert row["ErrorType"] == WORKER_BUSY


# ---------------------------------------------------------------------------
# Producers: worker barrier tools -> structured error codes
# ---------------------------------------------------------------------------


class _NavEnv:
    class _Ctrl:
        def get(self, kind, idx):
            return _NavEnv._Agent()

        def get_inventory(self, idx):
            return {}

    class _Agent:
        def get_position(self):
            return (0, 0, 0)

    controller = _Ctrl()


class _NavBarrier:
    def __init__(self, result: dict):
        self._result = result
        self.env = _NavEnv()

    async def submit_action(self, agent_idx: int, action: str) -> dict:
        return self._result


def test_tool_result_from_barrier_failure_yields_structured_code():
    """A barrier result with success=False and NO error key must surface the
    allowlisted ``action_failed`` code (not an empty error)."""
    from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier

    result = tool_result_from_barrier(
        {"observation": "Cannot navigate", "success": False, "finished": False}
    )
    assert result.success is False
    assert result.error == "action_failed"
    assert result.error in FRAMEWORK_ERROR_CODES
    assert error_code_for_result(result) == "action_failed"


def test_tool_result_from_barrier_passes_through_barrier_error():
    """When the barrier result carries its own error key it must pass through."""
    from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier

    result = tool_result_from_barrier(
        {
            "observation": "No path",
            "success": False,
            "finished": False,
            "error": "participant_busy",
        }
    )
    assert result.success is False
    assert result.error == "participant_busy"
    assert error_code_for_result(result) == "participant_busy"


def test_tool_result_from_barrier_success_has_no_error():
    from sar_orch.tools.worker._barrier_helpers import tool_result_from_barrier

    result = tool_result_from_barrier(
        {"observation": "ok", "success": True, "finished": False}
    )
    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
async def test_navigate_to_barrier_failure_produces_structured_code():
    """navigate_to-style barrier failure (success=False, no error key) must now
    yield a structured allowlisted code instead of an empty error."""
    from sar_orch.tools.worker.navigate_to import NavigateToTool

    tool = NavigateToTool(
        _NavBarrier(
            {"observation": "Target unreachable", "success": False, "finished": False}
        ),
        0,
    )
    result = await tool.execute(target_id="WaterSource_1")
    assert result.success is False
    assert result.error == "action_failed"
    assert error_code_for_result(result) == "action_failed"
    assert "action_failed" not in result.content  # content stays descriptive
    assert "unreachable" in result.content.lower()


def test_update_plan_failure_classifies_to_allowlisted_invalid_plan():
    """MissionGraph validation failures from update_plan must produce the
    allowlisted ``invalid_plan`` code, never a raw exception string."""
    import asyncio

    from a2a.builtin_tools.update_plan import UpdatePlanTool
    from a2a.coordinator.task_store import TaskStore

    store = TaskStore("req", router=None)
    tool = UpdatePlanTool(store)
    result = asyncio.run(
        tool.execute(
            [
                {"task_id": "a", "participant_ids": ["Alice"], "depends_on": ["b"]},
                {"task_id": "b", "participant_ids": ["Bob"], "depends_on": ["a"]},
            ]
        )
    )
    assert result.success is False
    assert result.error == "invalid_plan"
    assert result.error in FRAMEWORK_ERROR_CODES
    assert error_code_for_result(result) == "invalid_plan"
    assert "cycle" in result.content.lower()


def test_send_message_activate_plan_node_absent_error_defaults_to_activation_error():
    """activate_plan_node returning success=False WITHOUT an error key must be
    classified as the allowlisted ``activation_error`` code."""
    import asyncio

    from a2a.builtin_tools.send_message import SendMessageTool

    class _FakeRuntime:
        _manager = None

        async def activate_plan_node(
            self, logical_id, mission_graph, team_service=None
        ):
            return {"success": False, "reason": "transient team failure"}

    store = MagicMock()
    store._runtime = _FakeRuntime()
    store._mission_graph = object()
    tool = SendMessageTool(
        store=store,
        registry=MagicMock(),
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    result = asyncio.run(
        tool.execute(message_type="activate_plan_node", related_task_id="node-1")
    )
    assert result.success is False
    assert result.error == "activation_error"
    assert result.error in FRAMEWORK_ERROR_CODES
    assert error_code_for_result(result) == "activation_error"


def test_send_message_activate_plan_node_passes_through_allowlisted_error():
    """activate_plan_node failures with an allowlisted error key pass it through."""
    import asyncio

    from a2a.builtin_tools.send_message import SendMessageTool

    class _FakeRuntime:
        _manager = None

        async def activate_plan_node(
            self, logical_id, mission_graph, team_service=None
        ):
            return {
                "success": False,
                "error": "dependency_incomplete",
                "reason": "node-2 not done",
            }

    store = MagicMock()
    store._runtime = _FakeRuntime()
    store._mission_graph = object()
    tool = SendMessageTool(
        store=store,
        registry=MagicMock(),
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    result = asyncio.run(
        tool.execute(message_type="activate_plan_node", related_task_id="node-3")
    )
    assert result.success is False
    assert result.error == "dependency_incomplete"
    assert error_code_for_result(result) == "dependency_incomplete"


# ---------------------------------------------------------------------------
# Producer: coordinator router_interactions.csv
# ---------------------------------------------------------------------------


def test_coordinator_producer_records_success_and_error_code(tmp_path):
    from sar_orch.coordinator import SARCoordinator
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    coord = SARCoordinator(
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    coord._on_router_event(
        "tool_start",
        tool_name="send_message",
        arguments={
            "message_type": "assign_task",
            "content": "Go to the west reservoir",
            "who": "Alice",
        },
    )
    coord._on_router_event(
        "tool_result",
        tool_name="send_message",
        success=False,
        content="Error: worker_busy",
        error_code=WORKER_BUSY,
    )
    logger.close()

    rows = read_csv(tmp_path / "router_interactions.csv")
    outcome = [r for r in rows if r["Success"] == "False"]
    assert len(outcome) == 1
    assert outcome[0]["ErrorType"] == WORKER_BUSY
    assert outcome[0]["EventType"] == "send_message_result"


# ---------------------------------------------------------------------------
# Aggregator: positive / negative
# ---------------------------------------------------------------------------


def _write_acceptance_env(run_dir: Path, *, coverage=0.8, transport_rate=0.6):
    """Provide run_metrics.json + a fake export manifest so the Phase 5
    memory_acceptance evaluator can run over the producer CSVs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_metrics.json").write_text(
        json.dumps(
            {"coverage": coverage, "transport_rate": transport_rate},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    memory = run_dir / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "export_manifest.json").write_text(
        json.dumps({"scope_id": "run-1", "canonical_revision": 0}),
        encoding="utf-8",
    )


def test_positive_aggregation_counts_injected_worker_busy(tmp_path):
    """Injected worker + coordinator failed ToolResult(error='worker_busy')
    must yield ErrorType=worker_busy producer rows and a correct aggregator
    count (failed_tool_rows=2, worker_busy=2)."""
    from sar_orch.coordinator import SARCoordinator
    from sar_orch.eval.memory_acceptance import evaluate
    from sar_orch.logger import ExperimentLogger
    from sar_orch.worker import SARWorker

    _write_acceptance_env(tmp_path)

    logger = ExperimentLogger(log_dir=str(tmp_path))
    barrier = _FakeBarrier()

    worker = SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=barrier,
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    worker._on_step_event("tool_start", tool_name="send_message", arguments={"who": "Bob"})
    worker._on_step_event(
        "tool_result",
        tool_name="send_message",
        success=False,
        content="Error: worker_busy",
        error_code=WORKER_BUSY,
    )

    coord = SARCoordinator(
        barrier=barrier, exp_logger=logger, coordinator_secret=bytes(range(32))
    )
    coord._on_router_event(
        "tool_start",
        tool_name="send_message",
        arguments={"message_type": "assign_task", "content": "go", "who": "Alice"},
    )
    coord._on_router_event(
        "tool_result",
        tool_name="send_message",
        success=False,
        content="Error: worker_busy",
        error_code=WORKER_BUSY,
    )
    logger.close()

    report, unknown = evaluate(tmp_path)
    assert unknown == {}
    assert report["failed_tool_rows"] == 2
    assert report["missing_error_code_rows"] == 0
    assert report["framework_error_counts"] == {
        WORKER_BUSY: 2,
        "task_not_routable_yet": 0,
        "unknown_task_id": 0,
    }


def test_negative_missing_error_code_exits_nonzero_instrumentation_missing(
    tmp_path, capsys
):
    """A failed outcome (Success=false) with an empty ErrorType must be counted
    as missing_error_code_rows>0 and the aggregator script must exit nonzero
    with an `instrumentation_missing` diagnostic."""
    from sar_orch.coordinator import SARCoordinator
    from sar_orch.eval.memory_acceptance import (
        EXIT_INSTRUMENTATION_MISSING,
        main,
    )
    from sar_orch.logger import ExperimentLogger
    from sar_orch.worker import SARWorker

    _write_acceptance_env(tmp_path)

    logger = ExperimentLogger(log_dir=str(tmp_path))
    worker = SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    worker._on_step_event("tool_start", tool_name="no_op", arguments={})
    worker._on_step_event(
        "tool_result",
        tool_name="no_op",
        success=False,
        content="Error",
        error_code="",
    )

    # The aggregator requires both producer CSVs; produce a clean coordinator
    # (successful) router outcome so the only failed outcome is the worker one
    # above.
    coord = SARCoordinator(
        barrier=_FakeBarrier(),
        exp_logger=logger,
        coordinator_secret=bytes(range(32)),
    )
    coord._on_router_event(
        "tool_start",
        tool_name="send_message",
        arguments={"message_type": "assign_task", "content": "go", "who": "Alice"},
    )
    coord._on_router_event(
        "tool_result",
        tool_name="send_message",
        success=True,
        content="Assigned",
        error_code="",
    )
    logger.close()

    exit_code = main(["--results-dir", str(tmp_path)])
    assert exit_code == EXIT_INSTRUMENTATION_MISSING
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "instrumentation_missing" in err

    report_json = json.loads((tmp_path / "memory_acceptance.json").read_text())
    assert report_json["missing_error_code_rows"] > 0
    assert report_json["failed_tool_rows"] > 0

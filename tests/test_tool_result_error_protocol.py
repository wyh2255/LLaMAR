"""Phase 5 tool-result error protocol tests.

Covers:
- ``Agent.error_taxonomy``: pure classifier mapping a failed ToolResult's
  structured error to an allowlisted framework code, ``unclassified_tool_error``
  or ``missing_error_code`` (never parsing ``content="Error: ..."``).
- Router/worker Agents: public ``error_code`` produced before redaction and
  passed with the ``tool_result`` event after redaction; the raw error text
  never enters Message / logger / [DATA].
- ``sar_orch`` producers: worker ``log_agent_interaction`` and coordinator
  ``log_router_interaction`` record ``{Success, ErrorType}`` outcome rows.
- ``sar_orch/eval/memory_acceptance`` aggregator: positive count for injected
  ``worker_busy`` failures; nonzero exit with ``instrumentation_missing`` when
  any failed outcome lacks an ErrorType.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

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
# Producer: coordinator router_interactions.csv
# ---------------------------------------------------------------------------


def test_coordinator_producer_records_success_and_error_code(tmp_path):
    from sar_orch.coordinator import SARCoordinator
    from sar_orch.logger import ExperimentLogger

    logger = ExperimentLogger(log_dir=str(tmp_path))
    coord = SARCoordinator(barrier=_FakeBarrier(), exp_logger=logger)
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
    )
    worker._on_step_event("tool_start", tool_name="send_message", arguments={"who": "Bob"})
    worker._on_step_event(
        "tool_result",
        tool_name="send_message",
        success=False,
        content="Error: worker_busy",
        error_code=WORKER_BUSY,
    )

    coord = SARCoordinator(barrier=barrier, exp_logger=logger)
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
    coord = SARCoordinator(barrier=_FakeBarrier(), exp_logger=logger)
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

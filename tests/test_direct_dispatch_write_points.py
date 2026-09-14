"""C4 regression — direct-dispatch products exist only for accepted calls.

Defect (A1 card §7 adjacent finding, A100 first-run report §4-5): the direct
``send_message`` path wrote its products on ``tool_start`` — *before* the tool
executed.  ``subtasks.csv`` (assigned/canceled rows) and the top-level
``events.ndjson`` semantic events (assign_task / cancel_task / reply_to_help)
therefore existed even for dispatches that were rejected — the A100 short run
carried two ``subtasks.csv`` rows for ``assign_task`` calls that returned
``error=undeclared_task`` (graph mode) and never reached a worker.

Semantics fix: ``assigned`` means *actually dispatched*.  Dispatch state is
written on ``tool_result`` iff the call was accepted; a rejected/failed
attempt leaves no state row — its evidence lives in ``router_interactions.csv``
(the tool_start call row plus the ``send_message_result`` outcome row carrying
the public ``error_code``).  Recorded ``Step`` stays the call's step.

These tests drive the real seam: a real ``SendMessageTool`` against a real
``TaskStore`` (graph-mode rejection / accepted legacy dispatch) feeds the real
``ToolResult`` into the coordinator router callback (``_on_router_event``)
with the AI2Thor experiment logger attached, then asserts the products.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.mission_runtime import MissionRuntime, MissionRuntimeManager
from a2a.coordinator.task_store import TaskStore
from ai2thor_orch.env_pack import Ai2ThorEnvPack
from ai2thor_orch.logger import AI2ThorExperimentLogger
from orchestration.coordinator import OrchestratorCoordinator

_STEP_COUNTER = 3


class _FakeBarrier:
    """Mutable step counter: the surface ``_on_router_event`` reads."""

    def __init__(self, step: int = _STEP_COUNTER) -> None:
        self._step_counter = step


class _FakeRouter:
    """Minimal router used by the legacy (runtime-less) dispatch path."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, str]] = []

    async def send_task_async(
        self,
        agent_id: str,
        prompt: str,
        callback_url: str,
        dispatch_id: str,
        context_id: str | None = None,
    ) -> str:
        self.dispatched.append((agent_id, dispatch_id))
        return "wtid-1"


def _make_coordinator(tmp_path: Path, barrier: _FakeBarrier | None = None):
    exp_logger = AI2ThorExperimentLogger(
        experiment_name="3_transport_groceries", log_dir=str(tmp_path)
    )
    coord = OrchestratorCoordinator(
        env_pack=Ai2ThorEnvPack(mode="fake"),
        barrier=barrier or _FakeBarrier(),
        exp_logger=exp_logger,
        coordinator_secret=bytes(range(32)),
    )
    return coord, exp_logger


def _make_graph_mode_store() -> TaskStore:
    """Store with a declared MissionGraph (direct assign is rejected)."""
    manager = MissionRuntimeManager()
    runtime: MissionRuntime = manager.admit("ctx-write-points")
    store = TaskStore(
        original_request="transport groceries",
        router=None,
        context_id="ctx-write-points",
    )
    store.attach_runtime(runtime)
    store._mission_graph.replace(
        [
            {
                "logical_id": "recon",
                "participant_ids": ["Alice", "Bob"],
                "objective": "Explore the scene and report the groceries",
            },
        ]
    )
    return store


def _make_accepted_store() -> tuple[TaskStore, _FakeRouter]:
    """Runtime-less store: direct assign dispatches through the fake router."""
    router = _FakeRouter()
    store = TaskStore(
        original_request="transport groceries",
        router=router,
        context_id="ctx-write-points",
    )
    return store, router


def _make_tool(store: TaskStore) -> SendMessageTool:
    registry = MagicMock()
    return SendMessageTool(
        store=store,
        registry=registry,
        coordinator_host="localhost",
        coordinator_port=8080,
    )


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _feed_tool_result(coord: OrchestratorCoordinator, result, arguments: dict) -> None:
    """Replay the router step_callback sequence for one tool call."""
    coord._on_router_event("tool_start", tool_name="send_message", arguments=arguments)
    coord._on_router_event(
        "tool_result",
        tool_name="send_message",
        success=result.success,
        content=result.content,
        data=result.data,
        error_code=result.error or "",
    )


class TestRejectedDispatchesWriteNoState:
    @pytest.mark.asyncio
    async def test_graph_mode_rejected_assign_writes_no_dispatch_products(
        self, tmp_path
    ):
        """``undeclared_task`` rejection (A100 short-run case) → zero dispatch
        state; the attempt stays visible in router_interactions.csv only."""
        store = _make_graph_mode_store()
        tool = _make_tool(store)
        coord, exp_logger = _make_coordinator(tmp_path)

        arguments = {
            "message_type": "assign_task",
            "who": "Alice",
            "content": "Pick up Bread_1 and put it into Fridge_1",
        }
        result = await tool.execute(**arguments)
        assert result.success is False
        assert result.error == "undeclared_task", result.content

        _feed_tool_result(coord, result, arguments)
        exp_logger.close()

        # ── no dispatch state: no subtasks.csv row, no assign_task event ──
        assert _read_csv_rows(tmp_path / "subtasks.csv") == []
        events = _read_events(tmp_path / "events.ndjson")
        assert [e for e in events if e["event_type"] == "assign_task"] == []

        # ── attempt evidence: call row + outcome row with the error code ──
        rows = _read_csv_rows(tmp_path / "router_interactions.csv")
        call_rows = [r for r in rows if r["EventType"] == "assign_task"]
        outcome_rows = [r for r in rows if r["EventType"] == "send_message_result"]
        assert len(call_rows) == 1
        assert call_rows[0]["Subtask"] == arguments["content"]
        assert call_rows[0]["Success"] == ""
        assert call_rows[0]["WorkerTaskID"] == "dispatch-1"
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["Success"] == "False"
        assert outcome_rows[0]["ErrorType"] == "undeclared_task"

    @pytest.mark.asyncio
    async def test_rejected_cancel_writes_no_canceled_row(self, tmp_path):
        """An unknown-task cancel must not fabricate a ``canceled`` row."""
        store = _make_graph_mode_store()
        tool = _make_tool(store)
        coord, exp_logger = _make_coordinator(tmp_path)

        arguments = {"message_type": "cancel_task", "related_task_id": "nope"}
        result = await tool.execute(**arguments)
        assert result.success is False
        assert result.error == "unknown_task_id", result.content

        _feed_tool_result(coord, result, arguments)
        exp_logger.close()

        assert _read_csv_rows(tmp_path / "subtasks.csv") == []
        events = _read_events(tmp_path / "events.ndjson")
        assert [e for e in events if e["event_type"] == "cancel_task"] == []
        rows = _read_csv_rows(tmp_path / "router_interactions.csv")
        assert [r["ErrorType"] for r in rows if r["EventType"] == "cancel_task"] == [""]
        assert [
            r["ErrorType"] for r in rows if r["EventType"] == "send_message_result"
        ] == ["unknown_task_id"]

    @pytest.mark.asyncio
    async def test_rejected_reply_writes_no_reply_event(self, tmp_path):
        """An unroutable reply must not fabricate a ``reply_to_help`` event."""
        store = _make_graph_mode_store()
        tool = _make_tool(store)
        coord, exp_logger = _make_coordinator(tmp_path)

        arguments = {
            "message_type": "reply_to_help",
            "related_task_id": "nope",
            "content": "Proceed with the kitchen first",
        }
        result = await tool.execute(**arguments)
        assert result.success is False
        assert result.error == "unknown_task_id", result.content

        _feed_tool_result(coord, result, arguments)
        exp_logger.close()

        events = _read_events(tmp_path / "events.ndjson")
        assert [e for e in events if e["event_type"] == "reply_to_help"] == []


class TestAcceptedDispatchesWriteOneToOne:
    @pytest.mark.asyncio
    async def test_accepted_assign_writes_one_row_and_event_with_call_step(
        self, tmp_path
    ):
        """Accepted assign → exactly one assigned row + one assign_task event
        (1:1), both attributed to the tool_start step."""
        store, router = _make_accepted_store()
        tool = _make_tool(store)
        barrier = _FakeBarrier(step=3)
        coord, exp_logger = _make_coordinator(tmp_path, barrier=barrier)

        arguments = {
            "message_type": "assign_task",
            "who": "Alice",
            "content": "Pick up Bread_1 and put it into Fridge_1",
        }
        result = await tool.execute(**arguments)
        assert result.success is True, result.content
        await asyncio.sleep(0.05)  # let the non-blocking dispatch binding settle
        assert router.dispatched == [("Alice", "dispatch-1")]

        coord._on_router_event(
            "tool_start", tool_name="send_message", arguments=arguments
        )
        barrier._step_counter = 7  # a later step must not re-attribute the call
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=result.success,
            content=result.content,
            data=result.data,
            error_code=result.error or "",
        )
        exp_logger.close()

        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == 1
        assert rows[0]["SubtaskID"] == "dispatch-1"
        assert rows[0]["Status"] == "assigned"
        assert rows[0]["AssignedTo"] == "Alice"
        assert rows[0]["Subtask"] == arguments["content"]
        assert rows[0]["Step"] == "3"

        events = _read_events(tmp_path / "events.ndjson")
        assign_events = [e for e in events if e["event_type"] == "assign_task"]
        assert len(assign_events) == 1
        assert assign_events[0]["step"] == 3
        assert assign_events[0]["correlation_id"] == "coordinator-dispatch-1"
        assert assign_events[0]["payload"]["content"] == arguments["content"]
        assert assign_events[0]["payload"]["who"] == "Alice"

    @pytest.mark.asyncio
    async def test_accepted_cancel_writes_canceled_row_and_event(self, tmp_path):
        """Accepted cancel → one ``canceled`` terminal row + one event."""
        coord, exp_logger = _make_coordinator(tmp_path)

        arguments = {"message_type": "cancel_task", "related_task_id": "dispatch-1"}
        coord._on_router_event(
            "tool_start", tool_name="send_message", arguments=arguments
        )
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=True,
            content="cancelled",
            data=None,
            error_code="",
        )
        exp_logger.close()

        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == 1
        assert rows[0]["SubtaskID"] == "dispatch-1"
        assert rows[0]["Status"] == "canceled"
        assert rows[0]["Step"] == str(_STEP_COUNTER)

        events = _read_events(tmp_path / "events.ndjson")
        cancel_events = [e for e in events if e["event_type"] == "cancel_task"]
        assert len(cancel_events) == 1
        assert cancel_events[0]["payload"]["related_task_id"] == "dispatch-1"

    def test_accepted_reply_writes_reply_event(self, tmp_path):
        """Accepted reply → one ``reply_to_help`` event with the preview."""
        coord, exp_logger = _make_coordinator(tmp_path)
        arguments = {
            "message_type": "reply_to_help",
            "related_task_id": "dispatch-1",
            "content": "Proceed with the kitchen first",
        }
        coord._on_router_event(
            "tool_start", tool_name="send_message", arguments=arguments
        )
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=True,
            content="replied",
            data=None,
            error_code="",
        )
        exp_logger.close()

        events = _read_events(tmp_path / "events.ndjson")
        reply_events = [e for e in events if e["event_type"] == "reply_to_help"]
        assert len(reply_events) == 1
        assert reply_events[0]["step"] == _STEP_COUNTER
        assert reply_events[0]["payload"] == {
            "related_task_id": "dispatch-1",
            "response_preview": arguments["content"],
        }


class TestRejectedThenAcceptedSequence:
    def test_rejected_then_accepted_assign_writes_only_the_accepted_call(
        self, tmp_path
    ):
        """Rejected attempt followed by a successful retry: no stale stash
        leaks — the products belong to the accepted call only (its own ids and
        step), and the rejected attempt keeps only its router rows."""
        barrier = _FakeBarrier(step=4)
        coord, exp_logger = _make_coordinator(tmp_path, barrier=barrier)

        rejected_args = {
            "message_type": "assign_task",
            "who": "Alice",
            "content": "Pick up Bread_1 and put it into Fridge_1",
        }
        coord._on_router_event(
            "tool_start", tool_name="send_message", arguments=rejected_args
        )
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=False,
            content="Error: undeclared_task",
            data=None,
            error_code="undeclared_task",
        )

        barrier._step_counter = 5
        accepted_args = {
            "message_type": "assign_task",
            "who": "Bob",
            "content": "Pick up Apple_1 and put it into Fridge_1",
        }
        coord._on_router_event(
            "tool_start", tool_name="send_message", arguments=accepted_args
        )
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=True,
            content="dispatched",
            data={"dispatch_id": "dispatch-2", "worker_id": "Bob"},
            error_code="",
        )
        exp_logger.close()

        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == 1
        assert rows[0]["SubtaskID"] == "dispatch-2"
        assert rows[0]["AssignedTo"] == "Bob"
        assert rows[0]["Subtask"] == accepted_args["content"]
        assert rows[0]["Step"] == "5"

        events = _read_events(tmp_path / "events.ndjson")
        assign_events = [e for e in events if e["event_type"] == "assign_task"]
        assert len(assign_events) == 1
        assert assign_events[0]["payload"]["who"] == "Bob"
        assert assign_events[0]["step"] == 5

        # Both attempts stay auditable: 2 call rows + 2 outcome rows.
        router_rows = _read_csv_rows(tmp_path / "router_interactions.csv")
        assert len([r for r in router_rows if r["EventType"] == "assign_task"]) == 2
        outcomes = [r for r in router_rows if r["EventType"] == "send_message_result"]
        assert [r["Success"] for r in outcomes] == ["False", "True"]
        assert [r["ErrorType"] for r in outcomes] == ["undeclared_task", ""]

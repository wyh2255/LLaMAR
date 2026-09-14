"""A1 regression — plan-node (DAG) coordination path emits the same dispatch
products as the direct ``assign_task`` path.

Defect (A100 first-run report §4-5, 2026-09-14): once the coordinator declares
a MissionGraph (``update_plan``), direct ``assign_task`` is rejected (graph
mode) and every worker assignment is dispatched by
``send_message(message_type='activate_plan_node')`` through the MissionRuntime.
Those dispatches never passed the ``assign_task`` branch of
``OrchestratorCoordinator._log_send_message``, so ``subtasks.csv`` stayed
empty (file not even created) and ``events.ndjson`` carried no ``assign_task``
entries — identical runs produced different products depending on which
coordination path the LLM picked (path-dependent artifacts → false negatives
in product-based acceptance).

These tests drive the real seam instead of synthetic payloads: a real
MissionRuntime fan-out feeds the real ``SendMessageTool`` result into the real
coordinator router callback (``_on_router_event``) with the AI2Thor experiment
logger attached, then assert both products.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a.builtin_tools.send_message import SendMessageTool
from a2a.coordinator.mission_graph import MissionGraph
from a2a.coordinator.mission_runtime import MissionRuntime, MissionRuntimeManager
from a2a.coordinator.task_store import TaskStore
from ai2thor_orch.env_pack import Ai2ThorEnvPack
from ai2thor_orch.logger import AI2ThorExperimentLogger
from orchestration.coordinator import OrchestratorCoordinator

_STEP_COUNTER = 3


class _FakeBarrier:
    """Only the surface ``_on_router_event`` reads."""

    _step_counter = _STEP_COUNTER


_RECON_OBJECTIVE = (
    "Explore the scene, locate the Fridge and all grocery items, report aliases"
)
_RECON_ASSIGNMENTS = {
    "Alice": "Explore kitchen area; report Fridge alias and groceries seen",
    "Bob": "Explore remaining rooms; report groceries and Fridge alias",
}
_DELIVER_OBJECTIVE = "Deliver the located groceries into the Fridge"
_DELIVER_ASSIGNMENTS = {
    "Alice": "Pick up the nearest grocery and put it inside the Fridge",
    "Bob": "Pick up the nearest grocery and put it inside the Fridge",
}


def _make_coordinator(tmp_path: Path):
    exp_logger = AI2ThorExperimentLogger(
        experiment_name="3_transport_groceries", log_dir=str(tmp_path)
    )
    coord = OrchestratorCoordinator(
        env_pack=Ai2ThorEnvPack(mode="fake"),
        barrier=_FakeBarrier(),
        exp_logger=exp_logger,
        coordinator_secret=bytes(range(32)),
    )
    return coord, exp_logger


def _make_activation_scene(prompts: dict[str, str], nodes: list[dict] | None = None):
    """Real runtime/store/graph/tool wired for one multi-participant node.

    ``prompts`` is an out-param capturing the exact per-worker prompt handed
    to the dispatch adapter (i.e. what the worker actually receives).
    ``nodes`` overrides the default single-node MissionGraph declaration.
    """
    manager = MissionRuntimeManager()
    runtime: MissionRuntime = manager.admit("ctx-regression")
    store = TaskStore(
        original_request="transport groceries",
        router=None,
        context_id="ctx-regression",
    )
    store.attach_runtime(runtime)
    graph: MissionGraph = store._mission_graph
    graph.replace(
        nodes
        if nodes is not None
        else [
            {
                "logical_id": "recon",
                "participant_ids": ["Alice", "Bob"],
                "objective": "Explore the scene and report the groceries",
                "assignments": {
                    "Alice": "Check the kitchen",
                    "Bob": "Check the pantry",
                },
            },
        ]
    )

    async def adapter(worker_id, prompt, callback_url, dispatch_id, context_id):
        prompts[worker_id] = prompt
        return f"wtid-{worker_id}"

    runtime.set_dispatch_adapter(adapter)
    tool = SendMessageTool(
        store=store,
        registry=MagicMock(),
        coordinator_host="localhost",
        coordinator_port=8080,
    )
    return tool


def _read_csv_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_events(path: Path) -> list[dict]:
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


class TestPlanNodeDispatchProducts:
    @pytest.mark.asyncio
    async def test_plan_node_activation_emits_subtasks_and_assign_task(self, tmp_path):
        """Successful ``activate_plan_node`` fan-out → one ``assigned`` row and
        one ``assign_task`` event per dispatch, carrying the dispatched prompt."""
        prompts: dict[str, str] = {}
        tool = _make_activation_scene(prompts)
        coord, exp_logger = _make_coordinator(tmp_path)

        result = await tool.execute(
            message_type="activate_plan_node", related_task_id="recon"
        )
        assert result.success is True, result.content
        dispatches = result.data["dispatches"]
        assert {d["worker_id"] for d in dispatches} == {"Alice", "Bob"}

        _feed_tool_result(
            coord,
            result,
            {"message_type": "activate_plan_node", "related_task_id": "recon"},
        )
        exp_logger.close()

        # ── subtasks.csv: one assigned row per physical dispatch ──────────
        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == len(dispatches) == 2
        by_worker = {row["AssignedTo"]: row for row in rows}
        assert set(by_worker) == {"Alice", "Bob"}
        for dispatch in dispatches:
            row = by_worker[dispatch["worker_id"]]
            assert row["SubtaskID"] == dispatch["dispatch_id"]
            assert row["Status"] == "assigned"
            assert row["Step"] == str(_STEP_COUNTER)
            # content is the exact prompt the dispatch adapter delivered
            assert row["Subtask"] == prompts[dispatch["worker_id"]]
            assert "Assignment for" in row["Subtask"]

        # ── events.ndjson: one assign_task event per dispatch ─────────────
        events = _read_events(tmp_path / "events.ndjson")
        assign_events = [e for e in events if e["event_type"] == "assign_task"]
        assert len(assign_events) == 2
        events_by_worker = {e["payload"]["who"]: e for e in assign_events}
        for dispatch in dispatches:
            event = events_by_worker[dispatch["worker_id"]]
            payload = event["payload"]
            assert payload["message_type"] == "assign_task"
            assert payload["related_task_id"] == dispatch["dispatch_id"]
            assert payload["node_id"] == "recon"
            assert payload["content"] == prompts[dispatch["worker_id"]]
            assert event["step"] == _STEP_COUNTER
            assert event["agent"] == "Coordinator"
            assert event["correlation_id"].startswith("coordinator-dispatch-")

    @pytest.mark.asyncio
    async def test_failed_activation_emits_no_dispatch_products(self, tmp_path):
        """An activation that never dispatched (dependency_incomplete) must not
        fabricate rows/events."""
        prompts: dict[str, str] = {}
        tool = _make_activation_scene(prompts)
        coord, exp_logger = _make_coordinator(tmp_path)

        graph = tool._store._mission_graph
        graph.replace(
            [
                {
                    "logical_id": "recon",
                    "participant_ids": ["Alice", "Bob"],
                    "objective": "Explore the scene",
                },
                {
                    "logical_id": "deliver",
                    "participant_ids": ["Alice"],
                    "objective": "Deliver the groceries",
                    "depends_on": ["recon"],
                },
            ]
        )
        result = await tool.execute(
            message_type="activate_plan_node", related_task_id="deliver"
        )
        assert result.success is False
        assert result.error == "dependency_incomplete"

        _feed_tool_result(
            coord,
            result,
            {"message_type": "activate_plan_node", "related_task_id": "deliver"},
        )
        exp_logger.close()

        subtasks_path = tmp_path / "subtasks.csv"
        if subtasks_path.exists():
            assert _read_csv_rows(subtasks_path) == []
        events = _read_events(tmp_path / "events.ndjson")
        assert [e for e in events if e["event_type"] == "assign_task"] == []


class TestDirectDispatchProductsUnchanged:
    def test_direct_assign_task_still_writes_legacy_rows_and_events(self, tmp_path):
        """Guard: the pre-existing direct-dispatch write points stay intact."""
        coord, exp_logger = _make_coordinator(tmp_path)
        content = "Pick up Bread_1 and put it into Fridge_1"
        coord._on_router_event(
            "tool_start",
            tool_name="send_message",
            arguments={
                "message_type": "assign_task",
                "who": "Alice",
                "content": content,
            },
        )
        coord._on_router_event(
            "tool_result",
            tool_name="send_message",
            success=True,
            content="Task dispatched",
            data={"queued": False, "deferred": False, "worker_id": "Alice"},
            error_code="",
        )
        exp_logger.close()

        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == 1
        assert rows[0]["SubtaskID"] == "dispatch-1"
        assert rows[0]["Status"] == "assigned"
        assert rows[0]["AssignedTo"] == "Alice"
        assert rows[0]["Subtask"] == content

        events = _read_events(tmp_path / "events.ndjson")
        assign_events = [e for e in events if e["event_type"] == "assign_task"]
        assert len(assign_events) == 1
        assert assign_events[0]["payload"]["content"] == content
        assert assign_events[0]["payload"]["who"] == "Alice"


class TestFullRunReplayRecordedScenario:
    """等价回放 A100 完整跑（报告 §4-5）记录到的调度序列。

    录制事实（`reports/a100_firstrun_20260914/l3_full/coordinator/`，
    coordinator trace 7ec0ed6b…）：coordinator 用 ``update_plan`` 声明
    ``recon(Alice+Bob) → deliver-alice / deliver-bob（depends_on=[recon]）``；
    step 0 recon 一次激活成功（Alice/Bob 各 1 个物理 dispatch）；recon 未完成
    时 deliver 两次激活被拒（``dependency_incomplete``，trace step_index=7）；
    recon 两个 dispatch 完成后 deliver 重激活成功（step 23，各 1 个 dispatch）。
    全程共 4 个物理 dispatch —— 修复前这 4 条 assigned 行 / assign_task 事件
    一条都不会产生（``subtasks.csv`` 文件根本不生成）。
    """

    @staticmethod
    def _nodes() -> list[dict]:
        return [
            {
                "logical_id": "recon",
                "participant_ids": ["Alice", "Bob"],
                "objective": _RECON_OBJECTIVE,
                "assignments": dict(_RECON_ASSIGNMENTS),
            },
            {
                "logical_id": "deliver-alice",
                "participant_ids": ["Alice"],
                "objective": _DELIVER_OBJECTIVE,
                "assignments": {"Alice": _DELIVER_ASSIGNMENTS["Alice"]},
                "depends_on": ["recon"],
            },
            {
                "logical_id": "deliver-bob",
                "participant_ids": ["Bob"],
                "objective": _DELIVER_OBJECTIVE,
                "assignments": {"Bob": _DELIVER_ASSIGNMENTS["Bob"]},
                "depends_on": ["recon"],
            },
        ]

    @pytest.mark.asyncio
    async def test_recorded_full_run_sequence_emits_four_dispatch_products(
        self, tmp_path
    ):
        prompts: dict[str, str] = {}
        tool = _make_activation_scene(prompts, nodes=self._nodes())
        coord, exp_logger = _make_coordinator(tmp_path)
        store = tool._store

        async def activate(node_id: str):
            result = await tool.execute(
                message_type="activate_plan_node", related_task_id=node_id
            )
            _feed_tool_result(
                coord,
                result,
                {"message_type": "activate_plan_node", "related_task_id": node_id},
            )
            return result

        # step 0：recon 激活成功（多参与者 fan-out）
        recon = await activate("recon")
        assert recon.success is True, recon.content
        assert len(recon.data["dispatches"]) == 2

        # recon 未完成：deliver 激活被拒（dependency_incomplete）→ 零产物
        for node_id in ("deliver-alice", "deliver-bob"):
            denied = await activate(node_id)
            assert denied.success is False
            assert denied.error in ("dependency_incomplete", "node_not_ready")

        # recon 两个 dispatch 完成 → deliver 节点转 ready（A100 step 23 重激活）
        for dispatch in recon.data["dispatches"]:
            store.apply_physical_status(
                dispatch["dispatch_id"],
                "COMPLETED",
                source="replay",
                result="recon done",
            )

        deliver_dispatches: dict[str, list[dict]] = {}
        for node_id in ("deliver-alice", "deliver-bob"):
            ok = await activate(node_id)
            assert ok.success is True, ok.content
            deliver_dispatches[node_id] = ok.data["dispatches"]

        exp_logger.close()

        # ── subtasks.csv：4 条 assigned，只覆盖成功激活的物理 dispatch ──
        rows = _read_csv_rows(tmp_path / "subtasks.csv")
        assert len(rows) == 4
        assert [row["Status"] for row in rows] == ["assigned"] * 4

        node_of: dict[str, tuple[str, str, dict[str, str]]] = {}
        for dispatch in recon.data["dispatches"]:
            node_of[dispatch["dispatch_id"]] = (
                "recon",
                dispatch["worker_id"],
                _RECON_ASSIGNMENTS,
            )
        for node_id, dispatches in deliver_dispatches.items():
            for dispatch in dispatches:
                node_of[dispatch["dispatch_id"]] = (
                    node_id,
                    dispatch["worker_id"],
                    _DELIVER_ASSIGNMENTS,
                )

        by_id = {row["SubtaskID"]: row for row in rows}
        assert set(by_id) == set(node_of)
        for dispatch_id, (node_id, worker, assignments) in node_of.items():
            objective = _RECON_OBJECTIVE if node_id == "recon" else _DELIVER_OBJECTIVE
            # subtask = 与 dispatch adapter 实际交付逐字一致的融合 prompt
            assert by_id[dispatch_id]["AssignedTo"] == worker
            assert by_id[dispatch_id]["Subtask"] == (
                f"{objective}\n\nAssignment for {worker}: {assignments[worker]}"
            )

        # ── events.ndjson：恰好 4 条 assign_task（被拒激活零事件）────────
        events = _read_events(tmp_path / "events.ndjson")
        assign_events = [e for e in events if e["event_type"] == "assign_task"]
        assert len(assign_events) == 4
        assert {e["payload"]["related_task_id"] for e in assign_events} == set(node_of)
        for event in assign_events:
            node_id = node_of[event["payload"]["related_task_id"]][0]
            assert event["payload"]["node_id"] == node_id

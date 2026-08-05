"""P1 Phase 0 — Trajectory fire-flow 的 step-level 合同（P0.4 / P1.3，全部 RED）。

当前 `_check_fire_flow` 用 `get_interaction()`（每组首行）给 GetSupply/UseSupply
借参数：query-first 时首行是 query，GetSupply 的 type 因此丢失，后续 UseSupply
被误判 illegal；多个 GetSupply 候选时又只取首行、过早“确认”了唯一 type。

新合同（P1.3）：

1. `UseSupply(Fire, Type)` 的 type 从**同一条 trajectory Action** 的第二参数取得，
   不从 query 代表行借参。
2. `GetSupply(Reservoir)` 在当前日志里没有 type 参数：从该 agent、该 step 的
   **完整** interaction 流中过滤 `action_name == "GetSupply"` 且 `ai.succeeded
   is True` 的候选，仅当 canonical supply type 集合（`constraint.SUPPLY_TYPE_MAP`
   的 SAND/WATER/A/B 规则）恰为一个时使用它。
3. 候选缺失或有多个 type → 追加 `unknown_get_supply_type` evidence，后续
   UseSupply 因此前置取物类型不可证而仅能 `unverified`（`passed=None`），
   不得伪造 `illegal`。
4. 不得再调用 `get_interaction()`；不得按 CSV 首/末行或 trajectory
   action+success 做一对一推断。

三组 fixture：query-first 唯一候选 / 无候选 / 多个 canonical type 候选。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.trajectory import grade_trajectory

AGENT_COLUMNS = [
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "LLMInput",
    "LLMOutput",
    "Thinking",
    "RunID",
    "CorrelationID",
    "EventType",
    "ToolLatencyMs",
    "ErrorType",
]

TRAJ_COLUMNS = [
    "Step",
    "Actions",
    "Successes",
    "TimeoutAgents",
    "Coverage",
    "TransportRate",
    "Finished",
    "EndReason",
    "CompletedSubtasksDelta",
]

_QUERY_ACTION = "map_agent__query_natural(Where is Reservoir_1?)"


def _obs(inventory: dict, *, action_desc: str, success: bool = True) -> str:
    outcome = "successful" if success else "not successful"
    return (
        f"I am at co-ordinates: (0, 0, 0)\n"
        f"I am holding {json.dumps(inventory)}\n"
        f"Names: ['GreatFire_Region_1', 'Reservoir_1']\n"
        f"I tried to {action_desc} and was {outcome}."
    )


def _traj_row(step: int, actions: list[str], successes: list[bool]) -> dict:
    return {
        "Step": step,
        "Actions": json.dumps(actions),
        "Successes": json.dumps(list(successes)),
        "TimeoutAgents": "[]",
        "Coverage": 0.0,
        "TransportRate": 0.0,
        "Finished": "False",
        "EndReason": "",
        "CompletedSubtasksDelta": "[]",
    }


def _write_run(tmp_path: Path, rows: list[dict], traj_rows: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"agent_names": ["Alice"], "agent_count": 1}),
        encoding="utf-8",
    )
    with (run_dir / "agent_interactions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AGENT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in AGENT_COLUMNS})
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJ_COLUMNS)
        w.writeheader()
        for r in traj_rows:
            w.writerow({c: r.get(c, "") for c in TRAJ_COLUMNS})
    return run_dir


def _fire_flow(run_dir: Path):
    for r in grade_trajectory(load_episode(run_dir)):
        if r.detail.get("check") == "fire_flow":
            return r
    raise AssertionError("fire_flow check missing")


def _query_row(step: int) -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "map_agent__query_natural",
        "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
        "Action": _QUERY_ACTION,
        "Observation": '{"answer": "Reservoir_1 is to the north"}',
    }


def _get_supply_row(step: int, source: str, supply_type: str) -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "get_supply",
        "ToolArgs": json.dumps({"source_id": source, "supply_type": supply_type}),
        # 与实测一致：trajectory 的 GetSupply(Reservoir) 不带 type 参数，
        # type 只能来自 interaction 流（ToolArgs）。
        "Action": f"GetSupply({source})",
        "Observation": _obs(
            {"Sand": 0, "Water": 1, "Person": 0},
            action_desc=f"get supply from {source}",
        ),
    }


def _use_supply_row(step: int, fire: str = "GreatFire_Region_1", supply_type: str = "Water") -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "use_supply",
        "ToolArgs": json.dumps({"fire_id": fire, "supply_type": supply_type}),
        "Action": f"UseSupply({fire}, {supply_type})",
        "Observation": _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc=f"use supply on {fire}",
        ),
    }


# --------------------------------------------------------------------------
# 1. query-first 唯一 GetSupply 候选
# --------------------------------------------------------------------------


def _unique_candidate_run(tmp_path: Path) -> Path:
    return _write_run(
        tmp_path,
        [
            _query_row(1),  # L2 —— query 首行，当前代表行
            _get_supply_row(1, "Reservoir_1", "Water"),  # L3 —— 唯一 GetSupply 候选
            _use_supply_row(2),
        ],
        [
            _traj_row(1, ["GetSupply(Reservoir_1)"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [True]),
        ],
    )


def test_query_first_unique_get_supply_candidate_is_used(tmp_path):
    """唯一候选的 type 必须从 interaction 流解析出来 —— 当前 get_interaction()
    取 query 首行导致 type 丢失，UseSupply 被误判 illegal。"""
    r = _fire_flow(_unique_candidate_run(tmp_path))

    assert r.passed is True  # RED：当前 False（illegal_use_supply_count=1）
    assert r.detail["illegal_use_supply_count"] == 0  # RED：当前 1
    assert "unknown_get_supply_type" not in r.detail
    assert "Reservoir_1:Water" in r.detail["get_supply_records"]


# --------------------------------------------------------------------------
# 2. 无 GetSupply 候选
# --------------------------------------------------------------------------


def _no_candidate_run(tmp_path: Path) -> Path:
    return _write_run(
        tmp_path,
        [
            _query_row(1),  # L2 —— 只有 query，无 GetSupply 候选
            _use_supply_row(2),
        ],
        [
            _traj_row(1, ["NoOp"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [True]),
        ],
    )


def test_no_get_supply_candidate_is_unverified_not_illegal(tmp_path):
    """候选缺失 → unknown_get_supply_type evidence + unverified（passed=None），
    不得伪造 illegal。当前把这种情况判成 illegal（passed=False）。"""
    r = _fire_flow(_no_candidate_run(tmp_path))

    assert "unknown_get_supply_type" in r.detail  # RED：当前无此键
    assert len(r.detail["unknown_get_supply_type"]) == 1
    assert r.passed is None  # RED：当前 False
    assert r.detail["illegal_use_supply_count"] == 0  # RED：当前 1


# --------------------------------------------------------------------------
# 3. 多个 canonical type 候选
# --------------------------------------------------------------------------


def _multi_candidate_run(tmp_path: Path) -> Path:
    return _write_run(
        tmp_path,
        [
            _get_supply_row(1, "Reservoir_1", "Water"),  # L2
            _get_supply_row(1, "Reservoir_2", "Sand"),  # L3 —— 第二个 type 候选
            _use_supply_row(2),
        ],
        [
            _traj_row(1, ["GetSupply(Reservoir_1)"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [True]),
        ],
    )


def test_multiple_canonical_type_candidates_are_unverified(tmp_path):
    """两个不同 canonical type 候选 → 前置取物类型不可证 → unverified。
    当前取首行就“确认”Water 并判 passed=True，是过度自信。"""
    r = _fire_flow(_multi_candidate_run(tmp_path))

    assert "unknown_get_supply_type" in r.detail  # RED：当前无此键
    assert len(r.detail["unknown_get_supply_type"]) == 1
    assert r.passed is None  # RED：当前 True
    assert r.detail["illegal_use_supply_count"] == 0


# --------------------------------------------------------------------------
# 4. 现有有效证据路径不得回归（GREEN guard）
# --------------------------------------------------------------------------


def _plain_get_use_run(tmp_path: Path) -> Path:
    return _write_run(
        tmp_path,
        [
            _get_supply_row(1, "Reservoir_1", "Water"),
            _use_supply_row(2),
        ],
        [
            _traj_row(1, ["GetSupply(Reservoir_1)"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [True]),
        ],
    )


def test_plain_get_then_use_flow_still_passes(tmp_path):
    """有效证据路径（成功 GetSupply → 成功 UseSupply 同 type）必须保持通过。"""
    r = _fire_flow(_plain_get_use_run(tmp_path))
    assert r.passed is True
    assert r.detail["illegal_use_supply_count"] == 0
    assert "unknown_get_supply_type" not in r.detail

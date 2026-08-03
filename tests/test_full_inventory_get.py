"""full_inventory_get 时序修复的回归测试（同 E-2 / empty_supply_use 一类缺陷）。

`_check_full_inventory_get` 曾用 ai.inventory（动作**后**的快照）判定"动作发起时
库存是否已满"。GetSupply 成功取到第 3 个单位后，动作后库存正好满仓——旧实现会把
这类**成功**的 GetSupply 误判成"满仓还硬取"。20 个基线 run 里该规则报的 40 起
违规经核验全部属于此类误报。

修复：改用 inventory_before（动作前快照），并在其为 None（该 agent 首条交互，
无前序快照）时不指控、记 parse_miss（无证据不构成指控，漏报优于误报）。

规则本身的意图（满仓还去 GetSupply = 浪费动作）保留：若 inventory_before 显示
动作前就已满，仍必须判违规。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.constraint import grade_constraint

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


def _obs(inventory: dict, *, action_desc: str, success: bool = True) -> str:
    outcome = "successful" if success else "not successful"
    return (
        f"I am at co-ordinates: (0, 0, 0)\n"
        f"I am holding {json.dumps(inventory)}\n"
        f"Names: ['GreatFire_Region_1', 'Reservoir_1']\n"
        f"I tried to {action_desc} and was {outcome}."
    )


def _write_run(
    tmp_path: Path,
    rows: list[dict],
    *,
    agent_names: tuple[str, ...] = ("Alice",),
    steps: int | None = None,
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"agent_names": list(agent_names), "agent_count": len(agent_names)}),
        encoding="utf-8",
    )

    with (run_dir / "agent_interactions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AGENT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in AGENT_COLUMNS})

    n_steps = steps if steps is not None else max((r["Step"] for r in rows), default=1)
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJ_COLUMNS)
        w.writeheader()
        for s in range(1, n_steps + 1):
            w.writerow(
                {
                    "Step": s,
                    "Actions": json.dumps(["NoOp"] * len(agent_names)),
                    "Successes": json.dumps([True] * len(agent_names)),
                    "TimeoutAgents": "[]",
                    "Coverage": 0.0,
                    "TransportRate": 0.0,
                    "Finished": "False",
                    "EndReason": "",
                    "CompletedSubtasksDelta": "[]",
                }
            )
    return run_dir


def _violations(run_dir: Path) -> list[dict]:
    results = grade_constraint(load_episode(run_dir))
    return [v for r in results for v in r.detail["violations"]]


def _parse_miss(run_dir: Path) -> dict:
    results = grade_constraint(load_episode(run_dir))
    return results[0].detail["parse_miss_counts"]


def test_get_to_full_inventory_is_not_a_violation(tmp_path):
    """动作前未满（2/3），成功取到满仓（3/3）—— 不得判违规（这是 40 起误报的模式）。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "Reservoir_1"}),
                "Action": "NavigateTo(Reservoir_1)",
                "Observation": _obs(
                    {"Sand": 2, "Water": 0, "Person": 0},
                    action_desc="navigate to Reservoir_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Sand"}),
                "Action": "GetSupply(Reservoir_1, Sand)",
                # 动作后库存满仓（3/3）—— 旧实现正是在这里误判
                "Observation": _obs(
                    {"Sand": 3, "Water": 0, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []


def test_get_while_already_full_is_a_violation(tmp_path):
    """动作前已满（3/3）仍调用 GetSupply —— 规则真实意图必须保留，判违规。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Sand"}),
                "Action": "GetSupply(Reservoir_1, Sand)",
                "Observation": _obs(
                    {"Sand": 3, "Water": 0, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Sand"}),
                "Action": "GetSupply(Reservoir_1, Sand)",
                # 动作前已是 3/3（Step 1 的动作后快照），此次调用不该发生
                "Observation": _obs(
                    {"Sand": 3, "Water": 0, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                    success=False,
                ),
            },
        ],
    )
    rules = [v["rule"] for v in _violations(run)]
    assert rules.count("full_inventory_get") == 1


def test_first_interaction_without_prior_snapshot_counts_parse_miss(tmp_path):
    """首条交互无前序快照 —— 不指控，记 parse_miss（无证据不构成指控）。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "get_supply",
                "ToolArgs": json.dumps({"source_id": "Reservoir_1", "supply_type": "Sand"}),
                "Action": "GetSupply(Reservoir_1, Sand)",
                "Observation": _obs(
                    {"Sand": 3, "Water": 0, "Person": 0},
                    action_desc="get supply from Reservoir_1",
                ),
            },
        ],
    )
    assert [v["rule"] for v in _violations(run)] == []
    assert _parse_miss(run)["inventory_before"] == 1

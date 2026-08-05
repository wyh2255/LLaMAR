"""P1 Phase 0 — LLM judge 工具的显示/证据边界（全部 RED，P0.3）。

用真实 `init_agent_env()` + minimal `EpisodeDataset`（经 `load_episode` 落盘加载，
不用 MagicMock）钉住三个观察面：

1. `get_step_evidence()` 每条 interaction 的显示状态必须读 `ai.succeeded`
   （OK/FAIL/UNKNOWN），trajectory 的 Actions/Successes 作为独立 header 保留；
   当前实现用 trajectory Successes 显示所有行，同 step 内失败 attempt 被显示成 OK。
2. `get_agent_trace()` 的主步骤动作来自 trajectory step（`sr.actions[idx]`），
   不得把 query 首行代表行伪装成最终环境动作。
3. `get_observation_claims()` 不得把 query 的首行 observation 标为 environment
   ground truth；无环境 action 时必须明确标作 unavailable / attempt evidence。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.eval.agent import tools
from sar_orch.eval.dataset import load_episode

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


def _write_run(
    tmp_path: Path,
    rows: list[dict],
    *,
    agent_names: tuple[str, ...] = ("Alice",),
    traj_rows: list[dict] | None = None,
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

    if traj_rows is None:
        n_steps = max((r["Step"] for r in rows), default=1)
        traj_rows = [
            _traj_row(s, ["NoOp"] * len(agent_names), [True] * len(agent_names))
            for s in range(1, n_steps + 1)
        ]
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAJ_COLUMNS)
        w.writeheader()
        for r in traj_rows:
            w.writerow({c: r.get(c, "") for c in TRAJ_COLUMNS})
    return run_dir


def _init_env(tmp_path: Path, run_dir: Path):
    ep = load_episode(run_dir)
    tools.init_agent_env(ep, tmp_path / "eval_workspace")
    return ep


# --------------------------------------------------------------------------
# 1. get_step_evidence：interaction 显示状态来自 ai.succeeded
# --------------------------------------------------------------------------


def test_step_evidence_status_reads_ai_succeeded(tmp_path):
    """同 step 内失败 attempt 必须显示 FAIL，即使 trajectory 该步记成功。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps({"fire_id": "GreatFire_Region_1", "supply_type": "Water"}),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                    success=False,
                ),
            },
        ],
        traj_rows=[_traj_row(1, ["UseSupply(GreatFire_Region_1, Water)"], [True])],
    )
    _init_env(tmp_path, run)
    text = tools.get_step_evidence.invoke({"step": 1})

    # 失败 attempt 的显示状态必须来自 ai.succeeded —— 当前在最终 step 错误地显示 ?。
    assert "Alice [FAIL]: UseSupply" in text  # RED：当前 "Alice [OK]: UseSupply"
    assert "Alice [OK]: NavigateTo" in text
    # trajectory 摘要作为独立 header 保留
    assert "Actions: ['UseSupply" in text
    assert "Successes: [True]" in text


def test_step_evidence_unknown_status_for_unparseable_observation(tmp_path):
    """succeeded=None 的 interaction 显示 UNKNOWN，不得借 trajectory 显示 OK/FAIL。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "map_agent__query_natural",
                "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
                "Action": _QUERY_ACTION,
                "Observation": '{"answer": "Reservoir_1 is to the north"}',
            },
        ],
        traj_rows=[_traj_row(1, ["NoOp"], [True])],
    )
    _init_env(tmp_path, run)
    text = tools.get_step_evidence.invoke({"step": 1})

    assert "Alice [UNKNOWN]: map_agent__query_natural" in text  # RED：当前 "Alice [OK]: ..."
    assert "Step 1:" in text


# --------------------------------------------------------------------------
# 2. get_agent_trace：主步骤动作来自 trajectory，不是 query 首行代表行
# --------------------------------------------------------------------------


def test_agent_trace_main_action_comes_from_trajectory(tmp_path):
    """step 2 首行是 query、后续是 UseSupply —— trace 的主动作必须是
    trajectory 记录的 UseSupply，不得把 query 首行伪装成最终环境动作。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "map_agent__query_natural",
                "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
                "Action": _QUERY_ACTION,
                "Observation": '{"answer": "Reservoir_1 is to the north"}',
            },
            {
                "Step": 2,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps({"fire_id": "GreatFire_Region_1", "supply_type": "Water"}),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                ),
            },
        ],
        traj_rows=[
            _traj_row(1, ["NoOp"], [True]),
            _traj_row(2, ["UseSupply(GreatFire_Region_1, Water)"], [True]),
        ],
    )
    _init_env(tmp_path, run)
    text = tools.get_agent_trace.invoke({"agent": "Alice"})

    # 主动作来自 trajectory step —— 当前错误地显示 query 首行
    assert "Step 2: [✓] UseSupply(GreatFire_Region_1, Water)" in text  # RED
    assert "Step 1: [✓] NoOp" in text


# --------------------------------------------------------------------------
# 3. get_observation_claims：query observation 不是 environment ground truth
# --------------------------------------------------------------------------


def test_observation_claims_do_not_label_query_as_ground_truth(tmp_path):
    """仅有 query 交互的 step：无环境 action 可作真值，必须明确标
    unavailable/attempt evidence，不得把 query 首行 observation 标成 ground truth。"""
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "map_agent__query_natural",
                "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
                "Action": _QUERY_ACTION,
                "Observation": '{"answer": "Reservoir_1 is to the north"}',
            },
        ],
        traj_rows=[_traj_row(1, ["NoOp"], [True])],
    )
    _init_env(tmp_path, run)
    text = tools.get_observation_claims.invoke({"agent": "Alice", "step": 1})

    assert "ground truth" not in text.lower()  # RED：当前明确标注了 "ground truth"
    assert "unavailable" in text.lower()  # RED：当前没有 unavailable 标记
    assert "Total tool calls this step: 1" in text
    assert _QUERY_ACTION in text  # 所有 interaction 仍展示（attempt evidence 可见）


def test_observation_claims_excludes_tool_error_from_environment_evidence(tmp_path):
    """`error_observation=True` 是工具执行异常，可能从未到达环境；即使 action
    名属于 ENV_ACTION_NAMES，也不得当作 environment-action evidence。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": "Error: Tool execution failed: missing fire_id",
            }
        ],
        traj_rows=[_traj_row(1, ["UseSupply(GreatFire_Region_1, Water)"], [False])],
    )
    _init_env(tmp_path, run)
    text = tools.get_observation_claims.invoke({"agent": "Alice", "step": 1})

    assert "UseSupply(GreatFire_Region_1, Water)" in text  # attempt 仍可见
    assert "unavailable" in text.lower()  # 但不能伪装为环境证据


# --------------------------------------------------------------------------
# 4. get_dispatch_context：legacy representative 不能伪装成环境真值
# --------------------------------------------------------------------------


def test_dispatch_context_marks_query_shadow_as_legacy_representative(tmp_path):
    """首行 query 遮蔽后续环境 action 时，dispatch context 必须明示 team status
    使用的是 legacy representative evidence，不能把 query 行的位置/库存暗示为
    本 step 的环境真值。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "map_agent__query_natural",
                "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
                "Action": _QUERY_ACTION,
                "Observation": '{"answer": "Reservoir_1 is to the north"}',
            },
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="use supply on GreatFire_Region_1",
                ),
            },
        ],
        traj_rows=[_traj_row(1, ["UseSupply(GreatFire_Region_1, Water)"], [True])],
    )
    _init_env(tmp_path, run)
    text = tools.get_dispatch_context.invoke({"step": 1})

    assert "legacy representative evidence" in text.lower()
    assert "query_shadow" in text


def test_dispatch_context_marks_phantom_first_row_as_legacy_representative(tmp_path):
    """首行工具异常且同 step 有后续 interaction 时，legacy representative 标记
    必须对 dispatch judge 可见，不能把该异常行伪装为环境真值。
    """
    run = _write_run(
        tmp_path,
        [
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "use_supply",
                "ToolArgs": json.dumps(
                    {"fire_id": "GreatFire_Region_1", "supply_type": "Water"}
                ),
                "Action": "UseSupply(GreatFire_Region_1, Water)",
                "Observation": "Error: Tool execution failed: missing fire_id",
            },
            {
                "Step": 1,
                "Agent": "Alice",
                "ToolName": "navigate_to",
                "ToolArgs": json.dumps({"target_id": "GreatFire_Region_1"}),
                "Action": "NavigateTo(GreatFire_Region_1)",
                "Observation": _obs(
                    {"Sand": 0, "Water": 0, "Person": 0},
                    action_desc="navigate to GreatFire_Region_1",
                ),
            },
        ],
        traj_rows=[_traj_row(1, ["NavigateTo(GreatFire_Region_1)"], [True])],
    )
    _init_env(tmp_path, run)
    text = tools.get_dispatch_context.invoke({"step": 1})

    assert "legacy representative evidence" in text.lower()
    assert "phantom_first_row" in text

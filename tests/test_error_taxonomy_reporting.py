"""P1 Phase 0 — Attempt-level ErrorTaxonomy 合同 + 证据残差可见性（全部 RED）。

钉住两个不可混合的事实（P0.1 / P1.1）：

1. **attempt-level 计数**：`ErrorTaxonomy` 的 failure taxonomy 只统计
   *已观察到的环境 action attempt 失败*（`ai.succeeded is False` 且
   `error_observation is False` 且 action 在环境动作白名单内）。
   trajectory.csv 的 `Successes` 不再作为 observed-attempt 的失败门控。
2. **四类证据残差互斥**：infrastructure（timeout 独占 agent-slot）/
   tool-execution（error-observation 独占）／unobserved trajectory residual／
   unmapped（非 SAR 失败 query），任何一个 (step, agent) 不得同时落入两个桶。

以及 P0.4 的 report/aggregate 面：新 report 必须携带冻结字面量
`EVAL_SEMANTICS_VERSION = "attempt-stream-v1"`（P3.1），四个诊断桶必须以
`failure_diagnostics` 提升到 report JSON 并在 Markdown 单列为“证据残差”
（P3.1），aggregate 必须单独聚合各桶计数（P3.2）。

Phase 0 规则：每个新 detail/report 键**先断言存在**（AssertionError 而非
KeyError），再断言修复后合同值；当前语义下这些断言必须 RED。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.eval.aggregate import aggregate_group, write_aggregate_report_md
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.error_taxonomy import grade_error_taxonomy
from sar_orch.eval.report import merge_results, write_report_md

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

#: 新合同下 ErrorTaxonomy detail 必须存在的四个证据残差桶。
_NEW_BUCKETS = (
    "infrastructure_timeout_failures",
    "tool_execution_failures",
    "unobserved_trajectory_failures",
    "unmapped_failures",
)

#: P0.4 冻结的 evaluator semantics 版本字面量（Phase 0 冻结，实施时不得临场决定）。
EVAL_SEMANTICS_VERSION = "attempt-stream-v1"

_QUERY_ACTION = "map_agent__query_natural(Where is Reservoir_1?)"

_ERROR_OBS = (
    "Error: Tool execution failed: TypeError: UseSupplyTool.execute() missing 1 "
    "required positional argument: 'fire_id'"
)


def _obs(
    inventory: dict,
    *,
    action_desc: str,
    success: bool = True,
    names: tuple[str, ...] = ("GreatFire_Region_1", "Reservoir_1"),
) -> str:
    outcome = "successful" if success else "not successful"
    return (
        f"I am at co-ordinates: (0, 0, 0)\n"
        f"I am holding {json.dumps(inventory)}\n"
        f"Names: {list(names)}\n"
        f"I tried to {action_desc} and was {outcome}."
    )


def _traj_row(step: int, actions: list[str], successes: list[bool], timeout_agents=()) -> dict:
    return {
        "Step": step,
        "Actions": json.dumps(actions),
        "Successes": json.dumps(list(successes)),
        "TimeoutAgents": json.dumps(list(timeout_agents)),
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
    """落盘最小 run 目录；trajectory 行可显式指定（Successes/TimeoutAgents 需要）。"""
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


def _tax_detail(run_dir: Path) -> dict:
    return grade_error_taxonomy(load_episode(run_dir))[0].detail


def _assert_bucket_keys(detail: dict) -> None:
    """先断言四个新 detail 键存在 —— 键缺失时这里是 AssertionError，不是 KeyError。"""
    for key in _NEW_BUCKETS:
        assert key in detail, f"missing new ErrorTaxonomy detail key: {key}"


def _query_row(step: int = 1, observation: str = "{}") -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "map_agent__query_natural",
        "ToolArgs": json.dumps({"question": "Where is Reservoir_1?"}),
        "Action": _QUERY_ACTION,
        "Observation": observation,
    }


def _move_row(step: int, direction: str, *, success: bool) -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "move",
        "ToolArgs": json.dumps({"direction": direction}),
        "Action": f"Move({direction})",
        "Observation": _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc=f"move {direction}",
            success=success,
        ),
    }


def _use_supply_row(step: int, *, error_observation: bool = False) -> dict:
    return {
        "Step": step,
        "Agent": "Alice",
        "ToolName": "use_supply",
        "ToolArgs": json.dumps({"fire_id": "GreatFire_Region_1", "supply_type": "Water"}),
        "Action": "UseSupply(GreatFire_Region_1, Water)",
        "Observation": _ERROR_OBS if error_observation else _obs(
            {"Sand": 0, "Water": 0, "Person": 0},
            action_desc="use supply on GreatFire_Region_1",
            success=False,
        ),
    }


# --------------------------------------------------------------------------
# P0.1 Case 1：同 step 失败环境 action + 后续成功 action，trajectory 记录后者成功
#
# 当前错误行为：trajectory Successes=[True] 直接把整个 (step, agent) 跳过，
# 已观察到的失败 attempt 静默消失。新合同：失败 attempt 计入环境 taxonomy，
# evidence 指向失败 attempt 的 agent_interactions.csv 行。
# --------------------------------------------------------------------------


def test_failed_env_attempt_counts_even_when_trajectory_succeeds(tmp_path):
    run = _write_run(
        tmp_path,
        [
            _move_row(1, "DownLeft", success=False),  # L2 —— 失败 attempt
            _move_row(1, "Up", success=True),  # L3 —— 后续成功（trajectory 记录的）
        ],
        traj_rows=[_traj_row(1, ["Move(Up)"], [True])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert d["total_failures"] == 1  # RED：当前 0（trajectory 成功门控吞掉了失败 attempt）
    assert d["failure_taxonomy"] == {"obstacle_blocked": 1}
    assert sum(d["failure_taxonomy"].values()) == d["total_failures"]
    assert d["failure_details"][0]["evidence_ref"] == "agent_interactions.csv:L2"


# --------------------------------------------------------------------------
# P0.1 Case 2：非 timeout 的 trajectory Successes=[False]
#
# 子例 a：零 failed interaction（或仅 succeeded=None query）→ 只能进 generic
#         residual（unobserved_trajectory_failures），不得伪装成 unmapped。
# 子例 b：一条 succeeded=False 的非 SAR query → 只能进 unmapped_failures，
#         不得同时创建 generic residual。
# --------------------------------------------------------------------------


def test_traj_false_without_observed_failure_is_unobserved_residual(tmp_path):
    """零 interactions —— trajectory 说失败但没有任何已观察失败证据。"""
    run = _write_run(
        tmp_path,
        [],
        traj_rows=[_traj_row(1, ["NoOp"], [False])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["unobserved_trajectory_failures"]) == 1  # RED：键缺失
    entry = d["unobserved_trajectory_failures"][0]
    assert entry["step"] == 1 and entry["agent"] == "Alice"
    assert d["unmapped_failures"] == []  # RED：当前 1 条 "no interaction row"
    assert d["failure_taxonomy"] == {}


def test_unknown_query_only_is_unobserved_not_unmapped(tmp_path):
    """仅有 succeeded=None 的 query 行（无失败证据）→ generic residual，非 unmapped。"""
    run = _write_run(
        tmp_path,
        [_query_row(observation='{"answer": "Reservoir_1 is to the north"}')],
        traj_rows=[_traj_row(1, ["NoOp"], [False])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["unobserved_trajectory_failures"]) == 1  # RED：键缺失
    assert d["unmapped_failures"] == []  # RED：当前把 query 行计入 unmapped


def test_failed_non_sar_query_stays_unmapped_only(tmp_path):
    """succeeded=False 的非 SAR query：有已观察失败证据 → unmapped 专属，不得
    再进 generic residual（避免同一 (step, agent) 双重归因）。"""
    run = _write_run(
        tmp_path,
        [_query_row(observation=_obs({"Sand": 0}, action_desc="query", success=False))],
        traj_rows=[_traj_row(1, ["NoOp"], [False])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["unmapped_failures"]) == 1  # 当前即如此（动作不在环境白名单）
    assert d["unmapped_failures"][0]["interaction_action"] == _QUERY_ACTION
    assert d["unobserved_trajectory_failures"] == []  # RED：键缺失（修复后不得双计）
    assert d["tool_execution_failures"] == []
    assert d["failure_taxonomy"] == {}


# --------------------------------------------------------------------------
# P0.1 Case 3：SAR 工具调用产生 Error: / error_observation=True 且 trajectory
# 也失败 —— 这是工具执行异常，必须进 tool_execution_failures，不是 obstacle 等
# 环境 taxonomy，同一 (step, agent) 不得同时进 unobserved residual。
# --------------------------------------------------------------------------


def test_sar_error_observation_is_tool_execution_not_env_taxonomy(tmp_path):
    run = _write_run(
        tmp_path,
        [_use_supply_row(1, error_observation=True)],
        traj_rows=[_traj_row(1, ["UseSupply(GreatFire_Region_1, Water)"], [False])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["tool_execution_failures"]) == 1  # RED：键缺失
    entry = d["tool_execution_failures"][0]
    assert entry["step"] == 1 and entry["agent"] == "Alice"
    assert "UseSupply" in entry["action"]
    assert d["failure_taxonomy"] == {}  # RED：当前把它错归为 {"unknown": 1}
    assert d["total_failures"] == 0  # RED：当前 1
    assert d["unobserved_trajectory_failures"] == []
    assert d["unmapped_failures"] == []


# --------------------------------------------------------------------------
# P0.1 Case 4：TimeoutAgents 含该 agent 且 trajectory Successes=[False]
#
# 两个子例（零 failed interaction / 带一条 failed SAR raw attempt）都必须只进
# infrastructure_timeout_failures —— 不依赖 interaction 存在，且对 timeout
# agent-slot 独占归因：不得落入环境 taxonomy、tool/unmapped 或 generic residual。
# --------------------------------------------------------------------------


def test_timeout_without_interactions_is_infrastructure_only(tmp_path):
    run = _write_run(
        tmp_path,
        [],
        traj_rows=[_traj_row(1, ["NoOp"], [False], timeout_agents=[0])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["infrastructure_timeout_failures"]) == 1  # RED：键缺失
    entry = d["infrastructure_timeout_failures"][0]
    assert entry["step"] == 1 and entry["agent"] == "Alice"
    assert "trajectory.csv" in entry.get("evidence_ref", "")
    assert d["unmapped_failures"] == []  # RED：当前 1 条 "no interaction row"
    assert d["failure_taxonomy"] == {}
    assert d["unobserved_trajectory_failures"] == []


def test_timeout_slot_owns_failed_raw_attempt_exclusively(tmp_path):
    run = _write_run(
        tmp_path,
        [_move_row(1, "DownLeft", success=False)],
        traj_rows=[_traj_row(1, ["Move(DownLeft)"], [False], timeout_agents=[0])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["infrastructure_timeout_failures"]) == 1  # RED：键缺失
    assert "infrastructure" not in d["failure_taxonomy"]  # RED：当前 taxonomy 里有
    assert d["failure_taxonomy"] == {}
    assert d["total_failures"] == 0  # RED：当前 1
    assert d["unmapped_failures"] == []
    assert d["tool_execution_failures"] == []
    assert d["unobserved_trajectory_failures"] == []


def test_timeout_agent_with_trajectory_success_uses_observed_attempt_rules(tmp_path):
    """TimeoutAgents 只有同时满足 trajectory Successes=False 才独占 infrastructure。

    若 trajectory 明确记成功，timeout 标记本身不能吞掉同 slot 已观察到的失败
    environment attempt；该 attempt 仍按普通环境 taxonomy 归因。
    """
    run = _write_run(
        tmp_path,
        [_move_row(1, "DownLeft", success=False)],
        traj_rows=[_traj_row(1, ["Move(Up)"], [True], timeout_agents=[0])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert d["failure_taxonomy"] == {"obstacle_blocked": 1}
    assert d["total_failures"] == 1
    assert d["infrastructure_timeout_failures"] == []
    assert d["tool_execution_failures"] == []
    assert d["unmapped_failures"] == []
    assert d["unobserved_trajectory_failures"] == []


def test_unknown_metadata_agent_failed_attempt_is_unmapped_not_taxonomy(tmp_path):
    """无法映射到 trajectory agent-slot 的 raw interaction 必须显式 unmapped。

    即使其 action 名是环境动作，也不能伪造为可归因的 trajectory agent 行为；
    error-observation 的优先级另由既有工具错误合同覆盖。
    """
    ghost_row = _move_row(1, "DownLeft", success=False)
    ghost_row["Agent"] = "Ghost"
    run = _write_run(
        tmp_path,
        [ghost_row],
        agent_names=("Alice",),
        traj_rows=[_traj_row(1, ["NoOp"], [True])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert d["failure_taxonomy"] == {}
    assert d["total_failures"] == 0
    assert len(d["unmapped_failures"]) == 1
    assert d["unmapped_failures"][0]["agent"] == "Ghost"
    assert d["infrastructure_timeout_failures"] == []
    assert d["tool_execution_failures"] == []
    assert d["unobserved_trajectory_failures"] == []


# --------------------------------------------------------------------------
# P0.1 Case 5：非 SAR query 工具产生 Error: / error_observation=True
# error-observation 的结构性事实优先于“非 SAR”标签 → 只进 tool_execution_failures。
# --------------------------------------------------------------------------


def test_non_sar_error_observation_is_tool_execution_not_unmapped(tmp_path):
    run = _write_run(
        tmp_path,
        [_query_row(observation=_ERROR_OBS)],
        traj_rows=[_traj_row(1, ["NoOp"], [False])],
    )
    d = _tax_detail(run)
    _assert_bucket_keys(d)

    assert len(d["tool_execution_failures"]) == 1  # RED：键缺失
    assert d["tool_execution_failures"][0]["step"] == 1
    assert d["unmapped_failures"] == []  # RED：当前把 error-observation query 计入 unmapped
    assert d["unobserved_trajectory_failures"] == []
    assert d["failure_taxonomy"] == {}


# --------------------------------------------------------------------------
# P0.4：新 report 必须带冻结的 evaluator semantics version（P3.1），
# 四个诊断桶以 failure_diagnostics 提升到 report，并在 Markdown 单列。
# --------------------------------------------------------------------------


def _timeout_episode(tmp_path: Path):
    """一条已观察失败 raw attempt + timeout + trajectory False 的 episode。"""
    run = _write_run(
        tmp_path,
        [_move_row(1, "DownLeft", success=False)],
        traj_rows=[_traj_row(1, ["Move(DownLeft)"], [False], timeout_agents=[0])],
    )
    return load_episode(run)


def test_report_metadata_carries_frozen_eval_semantics_version(tmp_path):
    ep = _timeout_episode(tmp_path)
    report = merge_results(ep, grade_error_taxonomy(ep))
    assert "eval_semantics_version" in report["metadata"]  # RED：键缺失
    assert report["metadata"]["eval_semantics_version"] == EVAL_SEMANTICS_VERSION


def test_report_exposes_failure_diagnostics_buckets(tmp_path):
    ep = _timeout_episode(tmp_path)
    report = merge_results(ep, grade_error_taxonomy(ep))
    assert "failure_diagnostics" in report  # RED：键缺失
    fd = report["failure_diagnostics"]
    assert fd["infrastructure_timeout_failures"] == 1
    # 其余桶显式存在（0 也是显式事实，不是缺键）
    for key in ("tool_execution_failures", "unobserved_trajectory_failures", "unmapped_failures"):
        assert key in fd
    # 诊断桶不是 failure_taxonomy：后者仍只表示 observed environment attempts
    assert "infrastructure" not in report["failure_taxonomy"]


def test_report_md_lists_evidence_residuals(tmp_path):
    ep = _timeout_episode(tmp_path)
    report = merge_results(ep, grade_error_taxonomy(ep))
    write_report_md(report, tmp_path / "eval_report.md")
    md = (tmp_path / "eval_report.md").read_text(encoding="utf-8")
    assert "证据残差" in md  # RED：当前 Markdown 无此单列
    for key in _NEW_BUCKETS:
        assert key in md


def _diag_report(run: str, **diag) -> dict:
    return {
        "run_dir": run,
        "metadata": {"scene": 1, "agents": 2, "seed": 42},
        "episode": {"finished": True, "coverage": 0.9, "transport_rate": 0.8},
        "failure_diagnostics": diag,
    }


def test_aggregate_sums_failure_diagnostics_per_run():
    r1 = _diag_report(
        "a",
        infrastructure_timeout_failures=1,
        tool_execution_failures=0,
        unobserved_trajectory_failures=2,
        unmapped_failures=0,
    )
    r2 = _diag_report(
        "b",
        infrastructure_timeout_failures=1,
        tool_execution_failures=1,
        unobserved_trajectory_failures=0,
        unmapped_failures=1,
    )
    g = aggregate_group([r1, r2])
    assert "failure_diagnostics" in g  # RED：键缺失
    assert g["failure_diagnostics"]["infrastructure_timeout_failures"] == 2
    assert g["failure_diagnostics"]["tool_execution_failures"] == 1
    assert g["failure_diagnostics"]["unobserved_trajectory_failures"] == 2
    assert g["failure_diagnostics"]["unmapped_failures"] == 1


def test_aggregate_md_lists_failure_diagnostics_separately(tmp_path):
    """Aggregate Markdown 必须把 diagnostics 单列为“证据残差汇总”，不混进
    failure_taxonomy 或 constraint violations 的分母/表格。
    """
    group = aggregate_group(
        [
            _diag_report(
                "a",
                infrastructure_timeout_failures=1,
                tool_execution_failures=0,
                unobserved_trajectory_failures=2,
                unmapped_failures=0,
            ),
            _diag_report(
                "b",
                infrastructure_timeout_failures=1,
                tool_execution_failures=1,
                unobserved_trajectory_failures=0,
                unmapped_failures=1,
            ),
        ]
    )
    out = tmp_path / "aggregate_report.md"
    write_aggregate_report_md({"root_dir": "synthetic", "groups": [group]}, out)
    md = out.read_text(encoding="utf-8")

    assert "证据残差汇总" in md
    for key in _NEW_BUCKETS:
        assert key in md

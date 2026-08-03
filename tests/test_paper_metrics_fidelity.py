"""论文口径指标的保真度回归测试。

对应 LLaMAR 论文 §5 Metrics 的 5 个指标（SR / TR / C / B / L）。
重点锁住 Balance —— 它曾被实现成 `1.0 if finished else transport_rate`
这种与论文毫无关系的占位符，这里用可执行断言把定义钉住。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sar_orch.aggregate import aggregate
from sar_orch.eval.aggregate import (
    _numeric_stats,
    _t_ppf,
    aggregate_group,
    clopper_pearson_interval,
    write_aggregate_report_md,
)
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.outcome import (
    _BALANCE_EPSILON,
    compute_balance,
    episode_agent_count,
)

_TRAJ_HEADER = [
    "Step",
    "Actions",
    "Successes",
    "Observations",
    "Coverage",
    "TransportRate",
    "Finished",
    "TimeoutAgents",
    "CompletedSubtasksDelta",
    "EndReason",
]


def _write_run(
    run_dir: Path,
    steps: list[tuple[list[str], list[bool]]],
    *,
    agent_count: int | None = None,
    coverage: float = 0.0,
    transport_rate: float = 0.0,
    finished: bool = False,
) -> Path:
    """造一个最小 run 目录：trajectory.csv + metadata.json。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(_TRAJ_HEADER)
        for idx, (actions, successes) in enumerate(steps, start=1):
            writer.writerow(
                [
                    idx,
                    repr(actions),
                    repr(successes),
                    "",
                    coverage,
                    transport_rate,
                    str(finished and idx == len(steps)),
                    "[]",
                    "[]",
                    "",
                ]
            )
    meta: dict = {"scene": 1, "seed": 42}
    if agent_count is not None:
        meta["agent_count"] = agent_count
    (run_dir / "metadata.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    return run_dir


def _set_deltas(run_dir: Path, deltas: dict[int, list[str]]) -> None:
    """重写 trajectory.csv 的 CompletedSubtasksDelta 列。"""
    path = run_dir / "trajectory.csv"
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(_TRAJ_HEADER)
        for row in rows:
            row["CompletedSubtasksDelta"] = repr(deltas.get(int(row["Step"]), []))
            writer.writerow([row[c] for c in _TRAJ_HEADER])


def _outcome_detail(run_dir: Path) -> dict:
    """跑 OutcomeGrader，返回 episode 级 detail。"""
    from sar_orch.eval.graders.outcome import grade_outcome

    results = grade_outcome(load_episode(run_dir))
    return results[0].detail


# ── Balance ───────────────────────────────────────────────────────────────


def test_balance_is_min_over_max_plus_epsilon(tmp_path: Path):
    """B := min(s_i)/(max(s_i)+eps)，eps=1e-4。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
    )
    # agent0: 3 次成功；agent1: 1 次成功
    assert compute_balance(load_episode(run)) == pytest.approx(
        1 / (3 + _BALANCE_EPSILON)
    )


def test_balance_is_zero_when_an_agent_never_succeeds(tmp_path: Path):
    """论文：balance=0 表示至少一个智能体没有任何成功高层动作。

    这是最容易被写错的一条 —— 若只统计"出现过成功动作"的智能体，
    零成功者会被静默漏掉，2 智能体会算出 1.0（"完美均衡"）。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
    )
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_counts_all_agents_from_metadata_not_just_active_ones(tmp_path: Path):
    """智能体总数 n 取自 metadata，第 3 个智能体全程失败也必须计入。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (
                ["NavigateTo(FireA)", "NavigateTo(FireB)", "Move(Up)"],
                [True, True, False],
            ),
        ],
        agent_count=3,
    )
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_perfect_when_all_agents_equal(tmp_path: Path):
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["GetSupply(ReservoirYork)", "GetSupply(ReservoirUtah)"], [True, True]),
        ],
        agent_count=2,
    )
    balance = compute_balance(load_episode(run))
    assert balance == pytest.approx(2 / (2 + _BALANCE_EPSILON))
    assert balance < 1.0  # epsilon 使其严格小于 1


def test_balance_excludes_noop_actions(tmp_path: Path):
    """NoOp 是空动作，不算"成功高层动作"。"""
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NoOp()"], [True, True]),
            (["NavigateTo(FireB)", "NoOp()"], [True, True]),
        ],
        agent_count=2,
    )
    # agent1 全是 NoOp → 0 次成功高层动作 → balance 0
    assert compute_balance(load_episode(run)) == 0.0


def test_balance_never_uses_finished_or_transport_rate_placeholder(tmp_path: Path):
    """回归：balance 不得退化成 `1.0 if finished else transport_rate`。

    构造一个 finished=True、transport_rate=1.0，但两智能体贡献严重失衡的
    episode。占位符实现会给出 1.0；正确实现必须给出接近 1/4 的值。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
            (["Carry(LostPersonTimmy)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
        transport_rate=1.0,
        coverage=1.0,
        finished=True,
    )
    balance = compute_balance(load_episode(run))
    assert balance == pytest.approx(1 / (4 + _BALANCE_EPSILON))
    assert balance < 0.9


def test_tsv_aggregate_balance_matches_grader(tmp_path: Path):
    """standalone TSV 聚合器必须复用同一个 balance 实现，而非占位符。"""
    seed_dir = tmp_path / "benchmark" / "scene_1" / "agents_2" / "seed_42"
    _write_run(
        seed_dir,
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "Move(Up)"], [True, False]),
        ],
        agent_count=2,
        transport_rate=1.0,
        finished=True,
    )
    (seed_dir / "result.json").write_text(
        json.dumps(
            {
                "finished": True,
                "steps": 2,
                "coverage": 1.0,
                "transport_rate": 1.0,
                "end_reason": "",
                "log_dir": str(seed_dir),
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8"), delimiter="\t"))

    expected = 1 / (2 + _BALANCE_EPSILON)
    assert float(rows[0]["balance"]) == pytest.approx(expected)
    # 占位符会给出 1.0（finished=True）——确保不是它
    assert float(rows[0]["balance"]) != pytest.approx(1.0)


# ── Success Rate + 置信区间 ────────────────────────────────────────────────


def _report(finished: bool, **episode) -> dict:
    ep = {"finished": finished}
    ep.update(episode)
    return {
        "metadata": {"scene": 1, "agents": 2, "seed": 0},
        "run_dir": "r",
        "episode": ep,
    }


def test_success_rate_is_reported_with_clopper_pearson_ci():
    reports = [
        _report(True, coverage=1.0, transport_rate=1.0, balance=0.9, steps=10),
        _report(False, coverage=0.5, transport_rate=0.4, balance=0.5, steps=30),
        _report(True, coverage=0.8, transport_rate=0.9, balance=0.7, steps=20),
    ]
    group = aggregate_group(reports)
    sr = group["success_rate"]
    assert sr["mean"] == pytest.approx(2 / 3)
    assert sr["successes"] == 2 and sr["n"] == 3
    assert sr["ci_method"] == "clopper-pearson"
    # scipy/statsmodels 参考值：proportion_confint(2, 3, 0.05, method="beta")
    assert sr["ci95_low"] == pytest.approx(0.09429, abs=1e-3)
    assert sr["ci95_high"] == pytest.approx(0.99157, abs=1e-3)
    # SR 数值上等于 pass@1
    assert sr["mean"] == pytest.approx(group["pass_at_k"]["1"])


def test_clopper_pearson_matches_statsmodels_reference():
    """对齐 meta/result_analysis/confidence_intervals.py 的 get_CP_interval。"""
    for successes, n, low, high in [
        (7, 10, 0.34755, 0.93295),
        (0, 10, 0.0, 0.30850),
        (10, 10, 0.69150, 1.0),
        (3, 20, 0.03207, 0.37893),
    ]:
        got_low, got_high = clopper_pearson_interval(successes, n)
        assert got_low == pytest.approx(low, abs=1e-3)
        assert got_high == pytest.approx(high, abs=1e-3)


def test_clopper_pearson_handles_empty_sample():
    assert clopper_pearson_interval(0, 0) == (None, None)


def test_t_quantile_matches_scipy_reference():
    """对齐 scipy.stats.t.ppf(0.975, df)。"""
    for df, expected in {1: 12.7062, 2: 4.3027, 9: 2.2622, 29: 2.0452}.items():
        assert _t_ppf(0.975, df) == pytest.approx(expected, abs=1e-2)


def test_numeric_stats_reports_t_interval():
    stats = _numeric_stats([0.1, 0.2, 0.3, 0.4, 0.5])
    assert stats["mean"] == pytest.approx(0.3)
    # scipy 参考：mean ± t.ppf(0.975, 4) * sem = 0.3 ± 0.19632
    assert stats["ci95_low"] == pytest.approx(0.10368, abs=1e-3)
    assert stats["ci95_high"] == pytest.approx(0.49632, abs=1e-3)


def test_numeric_stats_degenerate_samples():
    single = _numeric_stats([0.5])
    assert single["ci95_low"] == 0.5 and single["ci95_high"] == 0.5
    empty = _numeric_stats([])
    assert empty["ci95_low"] is None and empty["ci95_high"] is None


def test_numeric_stats_no_data_is_none_not_zero():
    """无数据必须是 None，不能是 0.0 —— 否则下游把它当成"分数为零"。

    `episode_stats` 的键由 `aggregate_group` 无条件建出，所以"某指标全组一次都
    没解析成功"永远不表现为键缺失。若值是 0.0，门禁就会把 lower-is-better 指标
    （total_tokens）判成 ~100% 改进而放行。全部字段都要是 None，不只 CI。
    """
    empty = _numeric_stats([])
    assert empty == {
        "mean": None,
        "std": None,
        "min": None,
        "max": None,
        "ci95_low": None,
        "ci95_high": None,
    }


def test_numeric_stats_genuine_zeros_stay_zero():
    """真实的全零数据仍然是 0.0，不能被"顺手"归成 None。

    与上一个测试成对存在：`[]`（无数据）与 `[0.0, ...]`（测到了，就是零）
    是两件不同的事，任何一次"修回去"都会让其中一个断言失败。
    """
    zeros = _numeric_stats([0.0, 0.0, 0.0])
    assert zeros["mean"] == 0.0
    assert zeros["std"] == 0.0
    assert zeros["min"] == 0.0
    assert zeros["max"] == 0.0
    # CI 也是实打实算出来的 0，而非"无从计算"的 None
    assert zeros["ci95_low"] == 0.0 and zeros["ci95_high"] == 0.0
    # 与无数据的情形必须可区分
    assert _numeric_stats([])["mean"] is not zeros["mean"]


def test_numeric_stats_none_only_input_is_no_data():
    """全是 None 的输入等价于无数据（`_maybe_add` 之外的路径也要守住）。"""
    assert _numeric_stats([None, None])["mean"] is None  # type: ignore[list-item]


def test_aggregate_group_missing_metric_reports_none():
    """episode 里从未出现的数值字段，在 group 里必须是 None 而不是 0.0。"""
    reports = [
        {
            "run_dir": f"r{i}",
            "metadata": {"scene": 1, "agents": 2, "seed": i},
            # total_tokens 全组缺失（模拟解析失败），coverage 真实为 0
            "episode": {"finished": False, "coverage": 0.0, "steps": 10},
        }
        for i in range(3)
    ]
    group = aggregate_group(reports)
    es = group["episode_stats"]
    assert es["total_tokens"]["mean"] is None
    assert es["token_efficiency"]["mean"] is None
    # 同一份数据里真实测到的 0 依旧是 0
    assert es["coverage"]["mean"] == 0.0
    assert es["steps"]["mean"] == pytest.approx(10.0)


def test_aggregate_md_renders_missing_metric_as_dash(tmp_path: Path):
    """markdown 渲染遇到全 None 的指标要出 `-`，不能出 `0.0000`。"""
    reports = [
        {
            "run_dir": "r0",
            "metadata": {"scene": 1, "agents": 2, "seed": 0},
            "episode": {"finished": True, "coverage": 0.5, "steps": 7},
        }
    ]
    report = {
        "root_dir": str(tmp_path),
        "generated_at": "now",
        "valid_run_dirs": 1,
        "total_run_dirs_found": 1,
        "skipped_dirs": [],
        "num_groups": 1,
        "groups": [aggregate_group(reports)],
    }
    out = tmp_path / "agg.md"
    write_aggregate_report_md(report, out)
    text = out.read_text(encoding="utf-8")
    # Total Tokens 无数据 → 各列均为 "-"
    row = [ln for ln in text.splitlines() if ln.startswith("| Total Tokens ")]
    assert row, "缺少 Total Tokens 行"
    assert row[0].count("| -") >= 4, row[0]


def test_agent_count_ignores_non_env_agents(tmp_path: Path):
    """智能体数必须取 metadata.agent_count，不能用 agent_names 的长度。

    agent_names 里含 MapAgent / MapSummarizer 等非环境角色。若用它当 balance
    的分母 n，这些永不行动的"智能体"会被当成零成功者，把 balance 压成 0。
    """
    run = _write_run(
        tmp_path / "run",
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
    )
    episode = load_episode(run)
    # 模拟 dataset 把辅助角色也收进 agent_names 的真实情况
    episode.agent_names = ["Alice", "Bob", "MapAgent", "MapSummarizer"]
    assert episode_agent_count(episode) == 2
    # 1 步，两个 agent 各 1 次成功 → 1/(1+eps)，而非被虚构角色压成 0
    assert compute_balance(episode) == pytest.approx(1 / (1 + _BALANCE_EPSILON))


# ── Coverage (verified) ───────────────────────────────────────────────────
#
# 成功感知覆盖率，与论文口径 `coverage` **并列**、不替代它。旧口径只做动作
# **文本**子串匹配、完全不看成功与否（`SAR/Scenes/base_checker.py:180-182`），
# 于是"在动作文本里念出目标名字"就能零成本刷高 —— 两次**失败**的
# `NavigateTo(CaldorFire)` 也能把 coverage 抬到 2/N。而 `coverage_mean` 是
# 当前受回归门禁约束的指标，对自进化回路来说这是最便宜的伪改进通道。
#
# 下面的断言全部**并排钉住两个口径**：只断言新指标会漏掉真正的风险 ——
# 新旧口径在同一份数据上分道扬镳，才是这个指标存在的理由。

_INTERACTION_HEADER = [
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

#: 环境结果句的**当前措辞**（`dataset._CURRENT_OUTCOME_RE`）。成功/失败判定
#: 全仓只有这一个入口，这里复用它的措辞而不自己造，措辞变动由
#: `tests/test_eval_success_detection.py` 负责钉住。
_OK = "I tried to {} and was successful."
_BAD = "I tried to {} and was not successful."


def _write_interactions(
    run_dir: Path, rows: list[tuple[int, str, str, str, str]]
) -> None:
    """写 agent_interactions.csv。

    每行 `(step, agent, tool_name, action, observation)`。

    用交互流而非 trajectory.csv 的 Actions 列，与 `compute_coverage_verified`
    的数据源一致：后者每个 (step, agent) 只留**一行代表动作**，而同一步里
    agent 可能提交过多个环境动作，checker 是**每个**都过一遍的。
    """
    with (run_dir / "agent_interactions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(_INTERACTION_HEADER)
        for step, agent, tool, action, obs in rows:
            writer.writerow(
                [step, agent, tool, "", action, obs, "", "", "", "", "", "", "", ""]
            )


def _old_coverage(actions: list[str], scene: int = 1) -> float:
    """用 `base_checker.check_coverage` 的**原样逻辑**复算旧口径。

    刻意不 import base_checker：那需要构造完整 checker（场景对象、event、
    callback）。这里逐字复刻 `base_checker.py:180-182` 的三行——子串匹配、
    去重、不看 success——好让"新口径与旧口径分道扬镳"这件事在同一个测试里
    可见，而不是要读者去另一个文件里对照。旧口径本身的行为由
    `SAR/core_unittest.py` 与既有 87 个 run 的实测数据守着，本文件不动它。
    """
    from sar_orch.eval.graders.outcome import coverage_targets_for_scene

    targets = coverage_targets_for_scene(scene)
    assert targets, "场景目标名单复算失败，测试前提不成立"
    completed = [o for o in targets if any(o in a for a in actions)]
    return len(completed) / len(targets)


def test_failed_action_naming_a_target_counts_for_coverage_but_not_verified(
    tmp_path: Path,
):
    """**本指标存在的核心理由**：两次**失败**的 NavigateTo 念到了两个目标名。

    旧口径给 2/6（只看文本），新口径给 0/6（要求 succeeded is True）。
    两者在同一份数据上分道扬镳 —— 这正是"念出名字刷覆盖"的攻击面。
    """
    from sar_orch.eval.graders.outcome import compute_coverage_verified

    actions = ["NavigateTo(CaldorFire)", "NavigateTo(LostPersonTimmy)"]
    run = _write_run(
        tmp_path / "run", steps=[(actions, [False, False])], agent_count=2
    )
    _write_interactions(
        run,
        [
            (1, "Alice", "navigate_to", actions[0], _BAD.format("navigate")),
            (1, "Bob", "navigate_to", actions[1], _BAD.format("navigate")),
        ],
    )
    # 旧口径：念到名字就算，2/6
    assert _old_coverage(actions) == pytest.approx(2 / 6)
    # 新口径：一次都没成功，0/6
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(0.0)


def test_successful_action_naming_a_target_counts_for_both(tmp_path: Path):
    """成功交互必须**两个口径都算** —— 新指标只收紧 success 这一条，
    不得顺手改变目标名单或匹配方式，否则两个口径不再可比。"""
    from sar_orch.eval.graders.outcome import compute_coverage_verified

    actions = ["NavigateTo(CaldorFire)", "NavigateTo(LostPersonTimmy)"]
    run = _write_run(
        tmp_path / "run", steps=[(actions, [True, True])], agent_count=2
    )
    _write_interactions(
        run,
        [
            (1, "Alice", "navigate_to", actions[0], _OK.format("navigate")),
            (1, "Bob", "navigate_to", actions[1], _OK.format("navigate")),
        ],
    )
    assert _old_coverage(actions) == pytest.approx(2 / 6)
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(2 / 6)


def test_query_tool_text_mentioning_a_target_does_not_count_as_verified(
    tmp_path: Path,
):
    """查询类工具的**文本**里也会出现目标名字，但它们从未经过 checker。

    `report_observation` / `map_agent__*` 这类调用即使 `succeeded=True` 也不
    是环境动作 —— 计入会凭空抬高覆盖率，且这条通道比"失败的 NavigateTo"更
    便宜：只要在观测里念一遍名字就行，连动作都不用提交。
    """
    from sar_orch.eval.graders.outcome import compute_coverage_verified

    run = _write_run(tmp_path / "run", steps=[(["NoOp", "NoOp"], [True, True])])
    _write_interactions(
        run,
        [
            # 成功的工具调用，文本里点名两个目标 —— 但都不是环境动作
            (
                1,
                "Alice",
                "report_observation",
                "report_observation(CaldorFire is at (3,4))",
                _OK.format("report observation"),
            ),
            (
                1,
                "Bob",
                "map_agent__query_semantic_map",
                "query_semantic_map(LostPersonTimmy)",
                _OK.format("query the map"),
            ),
        ],
    )
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(0.0)


def test_verified_coverage_matches_old_when_every_action_succeeds(tmp_path: Path):
    """全成功时两个口径必须**恒等** —— 这是"只加 success 条件"的直接推论。

    若此断言失败，说明新口径的目标名单或匹配方式与 checker 已经漂移，
    那时它就不再是"同一个量的成功感知版本"，任何对比都失去意义。
    """
    from sar_orch.eval.graders.outcome import compute_coverage_verified

    actions = [
        "NavigateTo(CaldorFire)",
        "UseSupply(GreatFire, Water)",
        "GetSupply(ReservoirUtah, Water)",
        "Carry(LostPersonTimmy)",
        "DropOff(LostPersonTimmy, DepositFacility)",
        "GetSupply(ReservoirYork, Sand)",
    ]
    run = _write_run(
        tmp_path / "run", steps=[(actions, [True] * len(actions))], agent_count=2
    )
    _write_interactions(
        run,
        [(1, f"A{i}", "act", a, _OK.format("act")) for i, a in enumerate(actions)],
    )
    verified = compute_coverage_verified(load_episode(run))
    assert verified == pytest.approx(_old_coverage(actions))
    assert verified == pytest.approx(1.0), "6 个目标全部成功触及"


def test_verified_coverage_is_none_when_scene_is_unknown(tmp_path: Path):
    """scene 判不出来 → None（"无从判定"），**不是 0.0**。

    与 `_numeric_stats([]) -> None` 同一条约定：0.0 会被门禁当成真实的
    "覆盖率为零"参与判定，而事实是我们没有数据。
    """
    from sar_orch.eval.graders.outcome import compute_coverage_verified

    run = _write_run(tmp_path / "run", steps=[(["NavigateTo(CaldorFire)"], [True])])
    (run / "metadata.json").write_text(json.dumps({"seed": 42}), encoding="utf-8")
    _write_interactions(
        run, [(1, "Alice", "navigate_to", "NavigateTo(CaldorFire)", _OK.format("nav"))]
    )
    assert compute_coverage_verified(load_episode(run)) is None


def test_verified_coverage_is_graded_into_the_episode_detail(tmp_path: Path):
    """必须真的出现在 grader 产出里 —— 算得对但没接进 detail 等于没有。"""
    run = _write_run(
        tmp_path / "run", steps=[(["NavigateTo(CaldorFire)"], [False])], agent_count=2
    )
    _write_interactions(
        run,
        [(1, "Alice", "navigate_to", "NavigateTo(CaldorFire)", _BAD.format("nav"))],
    )
    detail = _outcome_detail(run)
    assert "coverage_verified" in detail
    assert detail["coverage_verified"] == pytest.approx(0.0)


def test_verified_coverage_aggregates_with_mean_and_ci_like_old_coverage():
    """并列指标必须走完整的 `_numeric_stats` 路径（mean/std/CI），
    否则它在 aggregate 报告里只是个装饰。"""
    reports = [
        {
            "run_dir": f"r{i}",
            "metadata": {"scene": 1, "agents": 2, "seed": i},
            "episode": {
                "finished": False,
                "coverage": 0.6,
                "coverage_verified": cv,
                "steps": 10,
            },
        }
        for i, cv in enumerate((0.2, 0.4, 0.6))
    ]
    es = aggregate_group(reports)["episode_stats"]
    assert es["coverage_verified"]["mean"] == pytest.approx(0.4)
    assert es["coverage_verified"]["ci95_low"] is not None
    assert es["coverage_verified"]["ci95_high"] is not None
    # 与旧口径并列存在，互不干扰
    assert es["coverage"]["mean"] == pytest.approx(0.6)


def test_old_format_report_without_verified_coverage_reports_none_not_zero():
    """既有 `sar_orch/results/` 下上百个 eval_report.json 没有这个键。

    缺失必须是 None 而不是 0.0：0.0 会让门禁把 higher-is-better 指标从基线
    0.6 掉到 0 读成"崩塌"，或反向把 actual 0.0 当成真实测量。这是本仓已经
    修过一次的缺陷类（见 `_numeric_stats` 的注释），不得重新引入。
    """
    reports = [
        {
            "run_dir": f"r{i}",
            "metadata": {"scene": 1, "agents": 2, "seed": i},
            # 旧格式：只有 coverage，没有 coverage_verified
            "episode": {"finished": False, "coverage": 0.6, "steps": 10},
        }
        for i in range(3)
    ]
    es = aggregate_group(reports)["episode_stats"]
    assert es["coverage_verified"]["mean"] is None
    assert es["coverage_verified"]["ci95_low"] is None
    # 同一份数据里的旧口径照常工作 —— 缺新键不得连带影响旧键
    assert es["coverage"]["mean"] == pytest.approx(0.6)


def test_genuine_zero_verified_coverage_stays_zero_not_none():
    """真实测到的 0（全部动作失败）与"缺失"必须保持可区分。"""
    reports = [
        {
            "run_dir": "r0",
            "metadata": {"scene": 1, "agents": 2, "seed": 0},
            "episode": {"finished": False, "coverage": 0.6,
                        "coverage_verified": 0.0, "steps": 10},
        }
    ]
    es = aggregate_group(reports)["episode_stats"]
    assert es["coverage_verified"]["mean"] == 0.0
    assert es["coverage_verified"]["mean"] is not None


def test_old_format_report_renders_verified_coverage_as_dash(tmp_path: Path):
    """旧格式 report 过渲染路径不得崩，且要出 `-` 而不是 `0.0%`。"""
    reports = [
        {
            "run_dir": "r0",
            "metadata": {"scene": 1, "agents": 2, "seed": 0},
            "episode": {"finished": True, "coverage": 0.5, "steps": 7},
        }
    ]
    report = {
        "root_dir": str(tmp_path),
        "generated_at": "now",
        "valid_run_dirs": 1,
        "total_run_dirs_found": 1,
        "skipped_dirs": [],
        "num_groups": 1,
        "groups": [aggregate_group(reports)],
    }
    out = tmp_path / "agg.md"
    write_aggregate_report_md(report, out)
    text = out.read_text(encoding="utf-8")
    row = [ln for ln in text.splitlines() if "Coverage (verified)" in ln]
    assert row, "缺少 Coverage (verified) 行"
    assert "| -" in row[0], row[0]


def test_verified_coverage_is_not_a_gated_metric():
    """**并列诊断量，不进门禁。** 论文 Table 7 的已发布数字出自 success-agnostic
    的旧口径；把新口径加进 `METRICS` / `DEFAULT_CONFIG` 会同时打断与论文数字
    和与既有几百个 eval_report.json 的可比性。这条断言防的是"顺手也门禁一下"。
    """
    from sar_orch.eval import gate

    assert "coverage_verified" not in gate.METRICS_BY_KEY
    assert "coverage_verified" not in gate.DEFAULT_CONFIG["absolute"]
    assert "coverage_verified" not in gate.DEFAULT_CONFIG["regression"]
    # 旧口径仍然在门禁里，位置不变
    assert "coverage_mean" in gate.METRICS_BY_KEY


def test_agent_count_falls_back_to_trajectory_width(tmp_path: Path):
    """metadata 缺 agent_count 时，回退到轨迹里 Actions 的宽度。"""
    run = _write_run(
        tmp_path / "run",
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)", "Move(Up)"], [True, True, True])],
        agent_count=None,
    )
    episode = load_episode(run)
    episode.agent_names = []
    assert episode_agent_count(episode) == 3


# ── 自定义指标：checker 口径 vs dispatch 口径 ──────────────────────────────


def test_step_efficiency_uses_checker_completions_not_dispatch_count(tmp_path: Path):
    """step_efficiency 分子必须是 checker 真实完成量，不能是 dispatch 条数。

    subtasks.csv 记的是协调器下发的自然语言任务（常仅 2 条），
    checker 的原子子任务是十几个。混用会把该指标压成与"效率"无关的数。
    """
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[
            (["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True]),
            (["UseSupply(FireA, Water)", "UseSupply(FireB, Water)"], [True, True]),
        ],
        agent_count=2,
        transport_rate=0.5,
    )
    # 4 个 checker 完成项分布在 2 步
    _set_deltas(run, {1: ["NavigateTo(FireA)", "NavigateTo(FireB)"], 2: ["EndFire(FireA)", "EndFire(FireB)"]})
    # dispatch 只有 1 条
    (run / "subtasks.csv").write_text(
        '"RunID","Step","SubtaskID","Status","AssignedTo","Subtask"\n'
        '"r","0","dispatch-1","assigned","Alice","explore"\n',
        encoding="utf-8",
    )

    detail = _outcome_detail(run)
    assert detail["completed_subtasks_trajectory"] == 4
    assert detail["dispatch_count"] == 1
    # 4 completions / 2 steps = 2.0；若误用 dispatch_count 会得 0.5
    assert detail["step_efficiency"] == pytest.approx(2.0)


def test_checker_subtask_total_is_derived_from_transport_rate(tmp_path: Path):
    """TR 的分母可由 completed/TR 反推，用于报告里同源的 x/y 显示。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
        transport_rate=13 / 15,
    )
    _set_deltas(run, {1: [f"Sub{i}" for i in range(13)]})
    detail = _outcome_detail(run)
    assert detail["checker_subtask_total"] == 15
    assert detail["completed_subtasks_trajectory"] == 13


def test_checker_subtask_total_is_none_when_transport_rate_zero(tmp_path: Path):
    """TR=0 时无法反推分母，必须返回 None 而不是猜一个数。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["Move(Up)", "Move(Up)"], [True, True])],
        agent_count=2,
        transport_rate=0.0,
    )
    assert _outcome_detail(run)["checker_subtask_total"] is None


def test_token_efficiency_shares_denominator_with_step_efficiency(tmp_path: Path):
    """两个效率指标必须同源：token/completions 与 completions/steps。"""
    run = tmp_path / "run"
    _write_run(
        run,
        steps=[(["NavigateTo(FireA)", "NavigateTo(FireB)"], [True, True])],
        agent_count=2,
        transport_rate=0.5,
    )
    _set_deltas(run, {1: ["A", "B", "C", "D"]})
    (run / "token_usage.csv").write_text(
        '"Agent","TotalTokens"\n"Alice","400"\n"Bob","400"\n', encoding="utf-8"
    )
    detail = _outcome_detail(run)
    assert detail["completed_subtasks_trajectory"] == 4
    assert detail["token_efficiency"] == pytest.approx(800 / 4)
    assert detail["step_efficiency"] == pytest.approx(4 / 1)


# ── Average steps (L) ─────────────────────────────────────────────────────


def test_paper_max_steps_is_30():
    """论文 §5：L=30，超出即判定失败。"""
    from sar_orch.benchmark import PAPER_MAX_STEPS as bench_l

    assert bench_l == 30


def test_benchmark_and_experiment_agree_on_l():
    """benchmark 与 experiment 各自定义 L，必须一致（前者不能 import 后者）。"""
    import ast

    from sar_orch.benchmark import PAPER_MAX_STEPS as bench_l

    src = Path(__file__).resolve().parent.parent / "sar_orch" / "experiment.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "PAPER_MAX_STEPS"
        if isinstance(node.value, ast.Constant)
    ]
    assert values == [bench_l]


def test_markdown_report_contains_paper_metric_table(tmp_path: Path):
    reports = [
        _report(True, coverage=1.0, transport_rate=1.0, balance=0.9, steps=10),
        _report(False, coverage=0.5, transport_rate=0.4, balance=0.5, steps=30),
    ]
    agg = {
        "root_dir": str(tmp_path),
        "generated_at": "now",
        "valid_run_dirs": 2,
        "total_run_dirs_found": 2,
        "skipped_dirs": [],
        "groups": [aggregate_group(reports)],
    }
    out = tmp_path / "aggregate_report.md"
    write_aggregate_report_md(agg, out)
    text = out.read_text(encoding="utf-8")
    assert "论文口径指标" in text
    assert "SR (95% CI)" in text
    assert "Clopper-Pearson" in text

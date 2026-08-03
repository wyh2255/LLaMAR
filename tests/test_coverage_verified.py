"""成功感知覆盖率（`coverage_verified`）的回归测试。

背景 —— 为什么是**两个**并列指标而不是修一个：
`SAR/Scenes/base_checker.py:check_coverage()` 只把动作**文本**与目标对象名做
子串匹配，压根没收到 `success` 参数（`perform_metric_check` 把 success 转给了
`_check_subtask`，却没转给 `check_coverage`）。于是两次**失败**的
`NavigateTo(CaldorFire)` 也能把 coverage 抬到 2/N。

这不是单纯的 bug：论文正文把 Coverage 定义为「与目标对象**成功**交互的比例」，
但论文作者的参考实现（本项目照搬的就是它）是 success-agnostic 的，Table 7 的
已发布数字即由该版本产出。直接改旧口径会同时打断与论文数字、以及与
`sar_orch/results/` 下上百个既有 eval_report.json 的可比性。

所以：旧口径**原样不动**，`coverage_verified` 并列新增。本文件的核心职责就是
把「两者刻意不同」这件事钉死 —— 任何一次把它们悄悄合并/对齐的改动都应在此
失败。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sar_orch.eval.aggregate import (
    _numeric_stats,
    aggregate_group,
    write_aggregate_report_md,
)
from sar_orch.eval.dataset import load_episode
from sar_orch.eval.graders.outcome import (
    compute_coverage_verified,
    coverage_targets_for_scene,
    grade_outcome,
)
from sar_orch.eval.report import merge_results, write_report_md

# scene 1 的覆盖目标名单（由 SAR/Scenes/scene_1.py 的字面量推导）。
_SCENE1_TARGET = "CaldorFire"

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

_AI_HEADER = [
    "Step",
    "Agent",
    "ToolName",
    "ToolArgs",
    "Action",
    "Observation",
    "LLMInput",
    "LLMOutput",
    "Thinking",
    "ErrorType",
    "ToolLatencyMs",
]


def _import_base_checker():
    """导入 SAR 的 BaseChecker（真实旧口径实现）。

    SAR/ 用扁平 import（不是 package），必须显式加进 sys.path —— 不能依赖
    别的测试先跑过并顺带注入了路径，否则单独跑本测试会 ModuleNotFoundError。
    """
    import sys

    sar_dir = Path(__file__).resolve().parents[1] / "SAR"
    if str(sar_dir) not in sys.path:
        sys.path.insert(0, str(sar_dir))
    from Scenes.base_checker import BaseChecker

    return BaseChecker


def _obs(action: str, *, success: bool) -> str:
    """构造环境口径的结果句。

    措辞必须与 `dataset._CURRENT_OUTCOME_RE` 完全一致（整行锚定），否则
    `succeeded` 会被判成 None（未知）而不是 True/False。
    """
    tail = "successful." if success else "not successful."
    return f"I tried to {action} and was {tail}"


def _write_run(
    run_dir: Path,
    interactions: list[tuple[int, str, str, bool]],
    *,
    scene: int = 1,
    agent_count: int = 2,
    coverage: float = 0.0,
    transport_rate: float = 0.0,
    finished: bool = False,
) -> Path:
    """造一个最小 run 目录：trajectory.csv + agent_interactions.csv + metadata。

    `interactions` 的元素为 (step, agent, action, success)。trajectory.csv 的
    Coverage 列由调用方显式给定 —— 它模拟的是**环境已记录的旧口径值**，
    与本文件复算的新口径彼此独立，正是要对照的两个量。
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    steps = sorted({s for s, _, _, _ in interactions}) or [1]
    with (run_dir / "trajectory.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(_TRAJ_HEADER)
        for step in steps:
            rows = [r for r in interactions if r[0] == step]
            w.writerow(
                [
                    step,
                    repr([r[2] for r in rows]),
                    repr([r[3] for r in rows]),
                    "",
                    coverage,
                    transport_rate,
                    str(finished and step == steps[-1]),
                    "[]",
                    "[]",
                    "",
                ]
            )

    with (run_dir / "agent_interactions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(_AI_HEADER)
        for step, agent, action, success in interactions:
            w.writerow(
                [
                    step,
                    agent,
                    "submit_action",
                    "",
                    action,
                    _obs(action, success=success),
                    "",
                    "",
                    "",
                    "",
                    "0",
                ]
            )

    (run_dir / "metadata.json").write_text(
        json.dumps({"scene": scene, "seed": 42, "agent_count": agent_count}),
        encoding="utf-8",
    )
    return run_dir


def _episode_report(run: Path) -> dict:
    ep = load_episode(run)
    return merge_results(ep, grade_outcome(ep))["episode"]


def _render_md(report: dict, tmp_path: Path) -> str:
    out = tmp_path / "eval_report.md"
    write_report_md(report, out)
    return out.read_text(encoding="utf-8")


# ── 目标名单复算 ──────────────────────────────────────────────────────────


def test_coverage_targets_reconstructed_from_scene_params():
    """eval 层能脱离 env/barrier 静态复算 checker.coverage 名单。

    这是选择「纯 eval 层实现」的前提：coverage 名单只由 scene_N.py 的字面量
    决定，不含运行期状态，故可离线复算。
    """
    targets = coverage_targets_for_scene(1)
    assert targets is not None
    # 与 SAR/Scenes/checker.py:64-76 的推导结果逐字对齐。
    assert set(targets) == {
        "CaldorFire",
        "GreatFire",
        "LostPersonTimmy",
        "DepositFacility",
        "ReservoirUtah",
        "ReservoirYork",
    }
    # reservoirs 只计入与 fire 的 tp 匹配的那些；agents 一律不计。
    assert not any(t in {"Alice", "Bob"} for t in targets)


def test_coverage_targets_unknown_scene_is_none_not_empty():
    """scene 无从判定时必须是 None（缺失），不能是 [] 或 0 分母。"""
    assert coverage_targets_for_scene(None) is None
    assert coverage_targets_for_scene("not-a-scene") is None
    assert coverage_targets_for_scene(999) is None


# ── 核心：新旧两口径的分歧 ────────────────────────────────────────────────


def test_failed_action_inflates_old_coverage_but_not_verified(tmp_path: Path):
    """失败动作念到目标名字：旧口径照记，新口径不记。

    这是本次改动的全部意义 —— 把两个口径并排钉住。若某次改动让它们一致了
    （无论是「修」了旧口径还是让新口径退回文本匹配），本测试必须失败。
    """
    # 旧口径：base_checker 的真实行为 —— 两次失败的 NavigateTo 也算覆盖 2/6。
    BaseChecker = _import_base_checker()

    class _Checker(BaseChecker):
        def callback(self):  # 场景回调与覆盖率无关，此处置空
            pass

    old = _Checker(
        subtasks=["NavigateTo(CaldorFire)"],
        coverage=sorted(coverage_targets_for_scene(1) or []),
    )
    old.perform_metric_check("NavigateTo(CaldorFire)", False, {})
    old.perform_metric_check("NavigateTo(GreatFire)", False, {})
    # 旧口径被失败动作抬高 —— 这是既有行为，**刻意保留**。
    assert old.coverage_completed == ["CaldorFire", "GreatFire"]
    assert old.get_coverage() == pytest.approx(2 / 6)
    assert old.subtasks_completed == []  # 一个子任务都没真完成

    # 新口径：同样的失败动作，一个都不算。
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", "NavigateTo(CaldorFire)", False),
            (1, "Bob", "NavigateTo(GreatFire)", False),
        ],
        coverage=2 / 6,  # 环境记录的旧口径值
    )
    ep = load_episode(run)
    assert compute_coverage_verified(ep) == pytest.approx(0.0)

    # 同一份 report 里两个字段并存且不等 —— 分歧对消费者可见。
    episode = _episode_report(run)
    assert episode["coverage"] == pytest.approx(2 / 6)
    assert episode["coverage_verified"] == pytest.approx(0.0)


def test_successful_action_counts_toward_both_metrics(tmp_path: Path):
    """成功动作念到目标名字：两个口径都记。"""
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", f"NavigateTo({_SCENE1_TARGET})", True),
            (1, "Bob", "GetSupply(ReservoirUtah)", True),
        ],
        coverage=2 / 6,
    )
    ep = load_episode(run)
    assert compute_coverage_verified(ep) == pytest.approx(2 / 6)

    episode = _episode_report(run)
    assert episode["coverage"] == pytest.approx(2 / 6)
    assert episode["coverage_verified"] == pytest.approx(2 / 6)


def test_verified_coverage_counts_each_target_once(tmp_path: Path):
    """重复成功交互同一目标只算一次（与旧口径的去重语义一致）。"""
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", f"NavigateTo({_SCENE1_TARGET})", True),
            (2, "Alice", f"UseSupply({_SCENE1_TARGET}, Water)", True),
            (3, "Alice", f"NavigateTo({_SCENE1_TARGET})", True),
        ],
    )
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(1 / 6)


def test_mixed_success_counts_only_the_successful_target(tmp_path: Path):
    """一成一败：新口径只记成功那个，旧口径两个都记。"""
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", "NavigateTo(CaldorFire)", True),
            (1, "Bob", "NavigateTo(GreatFire)", False),
        ],
        coverage=2 / 6,
    )
    episode = _episode_report(run)
    assert episode["coverage"] == pytest.approx(2 / 6)
    assert episode["coverage_verified"] == pytest.approx(1 / 6)


# ── 查询工具文本的刷分通道 ────────────────────────────────────────────────


def test_query_tool_text_mentioning_target_does_not_count(tmp_path: Path):
    """工具调用文本里出现目标名字**不算**覆盖，即使该调用本身成功。

    `map_agent__get_fire_info(CaldorFire)` / `report_observation(... CaldorFire
    ...)` 这类调用从未提交给环境、checker 根本看不到它们。若按文本匹配计入，
    覆盖率可以纯靠「查询目标信息」刷满 —— 比失败动作更便宜的刷分通道。
    """
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", f"map_agent__get_fire_info({_SCENE1_TARGET})", True),
            (1, "Bob", f"report_observation({{'x': 1}}, {_SCENE1_TARGET}, seen)", True),
            (2, "Alice", "map_agent__query_natural(Where is GreatFire located?)", True),
            (2, "Bob", "get_skill(navigation for LostPersonTimmy)", True),
        ],
    )
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(0.0)


def test_env_action_and_query_tool_are_distinguished(tmp_path: Path):
    """同一目标：查询工具不算，真实环境动作算 —— 二者必须可区分。"""
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", f"map_agent__get_fire_info({_SCENE1_TARGET})", True),
            (2, "Alice", f"NavigateTo({_SCENE1_TARGET})", True),
        ],
    )
    assert compute_coverage_verified(load_episode(run)) == pytest.approx(1 / 6)


def test_unknown_scene_yields_none_not_zero(tmp_path: Path):
    """scene 无从判定 → None（缺失），不是 0.0。

    0.0 会被下游当成「真的一个都没覆盖」，把解析问题伪装成行为问题。
    """
    run = _write_run(
        tmp_path / "run",
        [(1, "Alice", f"NavigateTo({_SCENE1_TARGET})", True)],
        scene=999,
    )
    assert compute_coverage_verified(load_episode(run)) is None
    assert _episode_report(run)["coverage_verified"] is None


# ── 聚合 ──────────────────────────────────────────────────────────────────


def _report(coverage: float | None, verified: float | None, **extra) -> dict:
    ep: dict = {"finished": True, "steps": 10, "coverage": coverage}
    if verified is not None:
        ep["coverage_verified"] = verified
    ep.update(extra)
    return {"run_dir": "r", "metadata": {"scene": 1, "agent_count": 2, "seed": 1}, "episode": ep}


def test_aggregate_coverage_verified_parallel_to_coverage():
    """新口径经 `_numeric_stats` 聚合出 mean/std/CI，与旧口径完全平行。"""
    group = aggregate_group(
        [
            _report(1.0, 0.5),
            _report(0.5, 0.25),
            _report(0.8, 0.4),
        ]
    )
    es = group["episode_stats"]
    assert es["coverage"]["mean"] == pytest.approx((1.0 + 0.5 + 0.8) / 3)
    assert es["coverage_verified"]["mean"] == pytest.approx((0.5 + 0.25 + 0.4) / 3)
    # 与旧口径同结构：同样的键、同样的 CI 机制。
    assert set(es["coverage_verified"]) == set(es["coverage"])
    assert es["coverage_verified"]["ci95_low"] is not None
    assert es["coverage_verified"]["ci95_high"] is not None
    assert es["coverage_verified"]["std"] == pytest.approx(
        _numeric_stats([0.5, 0.25, 0.4])["std"]
    )
    # 新口径整体低于旧口径 —— 聚合层没把两者混淆。
    assert es["coverage_verified"]["mean"] < es["coverage"]["mean"]


def test_old_format_reports_missing_field_aggregate_as_none():
    """旧格式 report 缺 `coverage_verified` → 聚合为 None，**不是 0.0**。

    复用 `_numeric_stats([]) -> None` 约定。若这里回落成 0.0，门禁会把
    「我们没有数据」当成「指标真的是 0」来判定。
    """
    group = aggregate_group([_report(1.0, None), _report(0.5, None)])
    es = group["episode_stats"]
    # 旧口径照常有值
    assert es["coverage"]["mean"] == pytest.approx(0.75)
    # 新口径全字段 None
    cv = es["coverage_verified"]
    assert cv["mean"] is None
    assert cv["std"] is None
    assert cv["ci95_low"] is None and cv["ci95_high"] is None
    assert cv["min"] is None and cv["max"] is None
    # 关键：区分「缺失」与「真实的 0」—— 后者 mean 是 0.0 而非 None。
    assert cv != _numeric_stats([0.0, 0.0])
    assert _numeric_stats([0.0, 0.0])["mean"] == 0.0


def test_partial_coverage_verified_uses_only_present_values():
    """新旧混合数据：只用存在的值求均值，缺失的不当 0 参与。"""
    group = aggregate_group([_report(1.0, 0.5), _report(0.6, None)])
    es = group["episode_stats"]
    assert es["coverage"]["mean"] == pytest.approx(0.8)
    # 只有一个 run 有新口径 → mean 就是它本身，而不是 (0.5+0)/2
    assert es["coverage_verified"]["mean"] == pytest.approx(0.5)


# ── 渲染 ──────────────────────────────────────────────────────────────────


def test_aggregate_markdown_renders_verified_column(tmp_path: Path):
    """聚合报告渲染新口径列，且旧格式数据渲染成 `-` 而不崩。"""
    groups = [
        aggregate_group([_report(1.0, 0.5), _report(0.5, 0.25)]),
        aggregate_group([_report(1.0, None), _report(0.5, None)]),
    ]
    out = tmp_path / "agg.md"
    write_aggregate_report_md({"groups": groups, "skipped": []}, out)
    md = out.read_text(encoding="utf-8")
    assert "C_verified" in md
    assert "Coverage (verified)" in md
    # 缺失组渲染为 "-"（_ci_cell/_fmt 的 None 约定），没有 0.0% 假值。
    assert "-" in md


def test_episode_markdown_renders_verified_row(tmp_path: Path):
    """单 run 报告同时渲染两行 Coverage，缺失时为 `-`。"""
    run = _write_run(
        tmp_path / "run",
        [
            (1, "Alice", "NavigateTo(CaldorFire)", True),
            (1, "Bob", "NavigateTo(GreatFire)", False),
        ],
        coverage=2 / 6,
    )
    ep = load_episode(run)
    md = _render_md(merge_results(ep, grade_outcome(ep)), tmp_path)
    assert "Coverage (verified)" in md
    # 两个口径都出现在指标总表里，值不同。
    assert "33.3%" in md  # 旧口径 2/6
    assert "16.7%" in md  # 新口径 1/6


def test_render_missing_verified_does_not_crash(tmp_path: Path):  # noqa: D401
    """旧格式 episode dict（无该键）渲染不崩，且不显示为 0。"""
    report = {
        "run_dir": "r",
        "metadata": {"scene": 1, "model": "m", "state_mode": "semantic"},
        "episode": {"coverage": 0.5, "transport_rate": 0.4, "steps": 3},
        "grader_results": [],
        "failure_taxonomy": {},
        "constraint_violations": [],
        "trajectory_checks": [],
    }
    md = _render_md(report, tmp_path)
    assert "Coverage (verified)" in md
    # 缺失渲染为 "-"（_fmt 的 None 约定），而不是 0.0%
    assert "| Coverage (verified) | - |" in md

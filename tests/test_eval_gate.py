"""Regression gate + recursive aggregate scan tests (二期)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sar_orch.eval.aggregate import aggregate_all, scan_results
from sar_orch.eval.gate import (
    DEFAULT_CONFIG,
    evaluate_gate,
    extract_metrics,
    group_label,
    load_config,
    main,
    render_gate_md,
    resolve_aggregate,
    write_gate_reports,
)


def _eval_report(
    *,
    scene: int = 1,
    agents: int = 2,
    seed: int = 42,
    finished: bool = True,
    coverage: float = 1.0,
    transport: float = 0.9,
    balance: float = 0.8,
    violations: int = 0,
    dispatch_pass: float | None = 0.9,
    halluc: float | None = 0.02,
) -> dict:
    report: dict = {
        "run_dir": f"s{scene}_a{agents}_s{seed}",
        "metadata": {"scene": scene, "agents": agents, "seed": seed},
        "episode": {
            "finished": finished,
            "coverage": coverage,
            "transport_rate": transport,
            "balance": balance,
            "steps": 30,
            "total_tokens": 1000,
            "end_reason": "finished" if finished else "max_steps_reached",
        },
        "failure_taxonomy": {"not_visible": 2},
        "constraint_violations": [
            {"rule": "empty_supply_use", "step": i} for i in range(violations)
        ],
        "trajectory_checks": [{"check": "rescue_flow", "passed": True}],
    }
    if dispatch_pass is not None or halluc is not None:
        report["llm_judge"] = {
            "judge_model": "m",
            "dispatch": {"pass_rate": dispatch_pass},
            "observation": {"hallucination_rate": halluc},
        }
    return report


def _write_run(root: Path, rel: str, report: dict) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "eval_report.json").write_text(json.dumps(report), encoding="utf-8")
    return d


# ── recursive scan ────────────────────────────────────────────────────────


def test_scan_finds_nested_benchmark_layout(tmp_path: Path):
    _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_42", _eval_report(seed=42))
    _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_43", _eval_report(seed=43))
    _write_run(tmp_path, "20260719_flat_run", _eval_report(scene=2, agents=4))

    reports, skipped = scan_results(tmp_path)

    assert len(reports) == 3
    assert skipped == []


def test_scan_prunes_workspace_and_retry_backups(tmp_path: Path):
    run = _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_42", _eval_report())
    # A nested eval_workspace must not be walked into.
    (run / "eval_workspace").mkdir()
    (run / "eval_workspace" / "eval_report.json").write_text("{}", encoding="utf-8")
    # A superseded retry backup must not be counted as an extra run.
    _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_42_pass_0", _eval_report())

    reports, skipped = scan_results(tmp_path)

    assert len(reports) == 1
    assert skipped == []


def test_scan_reports_leaf_dirs_without_report(tmp_path: Path):
    _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_42", _eval_report())
    (tmp_path / "benchmark/scene_1/agents_2/seed_99").mkdir(parents=True)

    reports, skipped = scan_results(tmp_path)

    assert len(reports) == 1
    assert skipped == ["benchmark/scene_1/agents_2/seed_99"]


def test_scan_unevaluated_run_reported_once_not_per_subdir(tmp_path: Path):
    """An un-evaluated run dir is one skip entry, not one per internal subdir."""
    run = tmp_path / "benchmark/scene_1/agents_2/seed_42"
    run.mkdir(parents=True)
    (run / "trajectory.csv").write_text("Step\n1\n", encoding="utf-8")
    for sub in ("workers", "coordinator", "supervision"):
        (run / sub).mkdir()
        (run / sub / "note.txt").write_text("x", encoding="utf-8")

    reports, skipped = scan_results(tmp_path)

    assert reports == []
    assert skipped == ["benchmark/scene_1/agents_2/seed_42"]


def test_scan_parse_error_is_surfaced(tmp_path: Path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "eval_report.json").write_text("{not json", encoding="utf-8")

    reports, skipped = scan_results(tmp_path)

    assert reports == []
    assert len(skipped) == 1 and "parse error" in skipped[0]


def test_scan_missing_root(tmp_path: Path):
    reports, skipped = scan_results(tmp_path / "nope")
    assert reports == []
    assert len(skipped) == 1 and "not found" in skipped[0]


# ── metric extraction ─────────────────────────────────────────────────────


def _agg(root: Path, runs: list[tuple[str, dict]]) -> dict:
    for rel, rep in runs:
        _write_run(root, rel, rep)
    return aggregate_all(root)


def test_extract_metrics_maps_all_canonical_fields(tmp_path: Path):
    agg = _agg(
        tmp_path,
        [
            ("r1", _eval_report(seed=1, finished=True, violations=2)),
            ("r2", _eval_report(seed=2, finished=False, violations=4)),
        ],
    )
    group = agg["groups"][0]
    assert group_label(group) == "S1xA2"

    m = extract_metrics(group)
    assert m["pass_at_1"] == pytest.approx(0.5)
    assert m["finished_rate"] == pytest.approx(0.5)
    assert m["coverage_mean"] == pytest.approx(1.0)
    assert m["transport_rate_mean"] == pytest.approx(0.9)
    assert m["balance_mean"] == pytest.approx(0.8)
    assert m["violations_per_run"] == pytest.approx(3.0)
    assert m["dispatch_pass_rate_mean"] == pytest.approx(0.9)
    assert m["hallucination_rate_mean"] == pytest.approx(0.02)


def test_extract_metrics_absent_judge_is_none(tmp_path: Path):
    agg = _agg(
        tmp_path,
        [("r1", _eval_report(dispatch_pass=None, halluc=None))],
    )
    m = extract_metrics(agg["groups"][0])
    assert m["dispatch_pass_rate_mean"] is None
    assert m["hallucination_rate_mean"] is None
    assert m["pass_at_1"] == pytest.approx(1.0)


def test_extract_metrics_unparsed_episode_field_is_none_not_zero(tmp_path: Path):
    """整组都没解析出 total_tokens → None，不是 0.0。

    `episode_stats` 的键永远存在（`aggregate_group` 无条件建），所以缺失只体现
    在值上。这里同时钉住"真实的零仍是零"：coverage=0.0 是测到的结果。
    """
    reports = []
    for i in range(3):
        rep = _eval_report(seed=i, coverage=0.0)
        del rep["episode"]["total_tokens"]
        reports.append((f"r{i}", rep))
    agg = _agg(tmp_path, reports)

    m = extract_metrics(agg["groups"][0])
    assert m["total_tokens_mean"] is None
    assert m["step_efficiency_mean"] is None
    # 真实测到的 0 不受影响 —— 两种情形必须可区分
    assert m["coverage_mean"] == 0.0


def test_unparsed_metric_skips_regression_instead_of_faking_improvement(
    tmp_path: Path,
):
    """token 全组解析失败时，退化检查必须 skip，不能报成"改进"。

    这是本 bug 的实际危害：`_numeric_stats([])` 曾返回 mean=0.0，门禁于是把
    lower-is-better 的 total_tokens 拿基线 908955 与 actual 0.0 相比，算出
    delta=-908955（"token 用量降到零"）判 pass —— 真实情况是没有数据。
    """
    base = _agg(tmp_path / "base", [("r1", _eval_report())])
    base["groups"][0]["episode_stats"]["total_tokens"]["mean"] = 908955.0

    cur_rep = _eval_report()
    del cur_rep["episode"]["total_tokens"]
    cur = _agg(tmp_path / "cur", [("r1", cur_rep)])

    cfg = {
        "min_runs": 1,
        "absolute": {},
        "regression": {"total_tokens_mean": {"relative": 0.20}},
    }
    res = evaluate_gate(cur, base, cfg)

    check = _by(res, "total_tokens_mean", "regression")[0]
    assert check.status == "skip", f"expected skip, got {check.status}: {check.reason}"
    assert check.actual is None
    # 关键：不得出现"改进"的假象（delta 为负会被读成 token 下降）
    assert check.delta is None


def test_unparsed_metric_skips_absolute_check_instead_of_scoring_zero(tmp_path: Path):
    """同理，绝对下限检查也必须 skip 而不是拿 0.0 去比。

    对 higher_is_better 指标（如 coverage）这个方向的错误是假 fail；对
    lower_is_better 指标（如 total_tokens 的上限）是假 pass。两者都不可接受。
    """
    rep = _eval_report()
    del rep["episode"]["total_tokens"]
    del rep["episode"]["coverage"]
    agg = _agg(tmp_path, [("r1", rep)])

    cfg = {
        "min_runs": 1,
        "absolute": {
            "total_tokens_mean": {"max": 500000.0},
            "coverage_mean": {"min": 0.60},
        },
        "regression": {},
    }
    res = evaluate_gate(agg, None, cfg)

    assert _by(res, "total_tokens_mean", "absolute")[0].status == "skip"
    assert _by(res, "coverage_mean", "absolute")[0].status == "skip"


# ── absolute checks ───────────────────────────────────────────────────────

_CFG_ABS_ONLY = {"min_runs": 1, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}


def _by(result, metric: str, check: str):
    return [c for c in result.checks if c.metric == metric and c.check == check]


def test_absolute_min_fails_below_bound(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(coverage=0.4))])
    res = evaluate_gate(agg, None, _CFG_ABS_ONLY)

    assert res.passed is False
    check = _by(res, "coverage_mean", "absolute")[0]
    assert check.status == "fail"
    assert check.actual == pytest.approx(0.4)
    assert check.threshold == pytest.approx(0.6)


def test_absolute_max_fails_above_bound(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(violations=30))])
    cfg = {"min_runs": 1, "absolute": {"violations_per_run": {"max": 20.0}}, "regression": {}}
    res = evaluate_gate(agg, None, cfg)

    assert res.passed is False
    assert _by(res, "violations_per_run", "absolute")[0].status == "fail"


def test_absolute_pass_within_bound(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(coverage=0.95))])
    res = evaluate_gate(agg, None, _CFG_ABS_ONLY)

    assert res.passed is True
    assert _by(res, "coverage_mean", "absolute")[0].status == "pass"


def test_absent_metric_skips_not_fails(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(dispatch_pass=None, halluc=None))])
    cfg = {
        "min_runs": 1,
        "absolute": {"dispatch_pass_rate_mean": {"min": 0.9}},
        "regression": {},
    }
    res = evaluate_gate(agg, None, cfg)

    assert res.passed is True
    assert _by(res, "dispatch_pass_rate_mean", "absolute")[0].status == "skip"


# ── regression checks ─────────────────────────────────────────────────────


def test_regression_fails_when_higher_is_better_metric_drops(tmp_path: Path):
    base = _agg(tmp_path / "base", [("r1", _eval_report(coverage=0.95))])
    cur = _agg(tmp_path / "cur", [("r1", _eval_report(coverage=0.70))])
    cfg = {"min_runs": 1, "absolute": {}, "regression": {"coverage_mean": 0.10}}

    res = evaluate_gate(cur, base, cfg)

    assert res.passed is False
    check = _by(res, "coverage_mean", "regression")[0]
    assert check.status == "fail"
    assert check.delta == pytest.approx(0.25)
    assert check.baseline == pytest.approx(0.95)


def test_regression_fails_when_lower_is_better_metric_rises(tmp_path: Path):
    base = _agg(tmp_path / "base", [("r1", _eval_report(halluc=0.01))])
    cur = _agg(tmp_path / "cur", [("r1", _eval_report(halluc=0.20))])
    cfg = {"min_runs": 1, "absolute": {}, "regression": {"hallucination_rate_mean": 0.05}}

    res = evaluate_gate(cur, base, cfg)

    assert res.passed is False
    check = _by(res, "hallucination_rate_mean", "regression")[0]
    assert check.status == "fail"
    assert check.delta == pytest.approx(0.19)


def test_improvement_never_fails_regression(tmp_path: Path):
    base = _agg(tmp_path / "base", [("r1", _eval_report(coverage=0.50, halluc=0.30))])
    cur = _agg(tmp_path / "cur", [("r1", _eval_report(coverage=1.00, halluc=0.00))])
    cfg = {
        "min_runs": 1,
        "absolute": {},
        "regression": {"coverage_mean": 0.10, "hallucination_rate_mean": 0.05},
    }

    res = evaluate_gate(cur, base, cfg)

    assert res.passed is True
    assert _by(res, "coverage_mean", "regression")[0].delta == pytest.approx(-0.50)
    assert _by(res, "hallucination_rate_mean", "regression")[0].delta == pytest.approx(-0.30)


def test_regression_within_tolerance_passes(tmp_path: Path):
    base = _agg(tmp_path / "base", [("r1", _eval_report(coverage=0.95))])
    cur = _agg(tmp_path / "cur", [("r1", _eval_report(coverage=0.90))])
    cfg = {"min_runs": 1, "absolute": {}, "regression": {"coverage_mean": 0.10}}

    res = evaluate_gate(cur, base, cfg)

    assert res.passed is True
    assert _by(res, "coverage_mean", "regression")[0].status == "pass"


def test_no_baseline_runs_absolute_only(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(coverage=0.9))])
    res = evaluate_gate(agg, None, DEFAULT_CONFIG)

    assert res.baseline_root is None
    assert [c for c in res.checks if c.check == "regression"] == []


# ── meta checks ───────────────────────────────────────────────────────────


def test_underpowered_group_downgrades_failure_to_warn(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(coverage=0.1))])
    cfg = {"min_runs": 3, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}

    res = evaluate_gate(agg, None, cfg)

    assert res.passed is True
    assert _by(res, "coverage_mean", "absolute")[0].status == "warn"
    assert _by(res, "min_runs", "meta")[0].status == "warn"


# ── 三态：PASS / FAIL / INCONCLUSIVE ──────────────────────────────────────
#
# `passed = not failures` 单独用是危险的：样本量不足时每条 fail 都被降级成
# warn，于是"数据显示全面崩塌"会以 passed=True 收场。下面几条把三态钉住 ——
# 关键是 (d)：只有**真的掩盖了 fail** 才算无法判定，否则每个小批次都被标成
# inconclusive，这个信号就没用了。


def _collapse_pair() -> tuple[dict, dict]:
    """候选全面崩塌（n=1）对上一个正常基线（n=9）。"""
    current = {
        "root_dir": "x",
        "groups": [
            {
                "key": {"scene": 1, "agents": 2},
                "n": 1,
                "finished_count": 0,
                "pass_at_k": {"1": 0.0},
                "episode_stats": {
                    "coverage": {"mean": 0.10},
                    "transport_rate": {"mean": 0.05},
                    "total_tokens": {"mean": 5_000_000},
                    "balance": {"mean": 0.1},
                    "idle_ratio": {"mean": 0.9},
                    "step_efficiency": {"mean": 0.1},
                },
                "constraint_violations": {"per_run_violations": 40.0},
                "config_issues": [],
            }
        ],
    }
    baseline = {
        "root_dir": "y",
        "groups": [
            {
                "key": {"scene": 1, "agents": 2},
                "n": 9,
                "finished_count": 6,
                "pass_at_k": {"1": 0.667},
                "episode_stats": {
                    "coverage": {"mean": 0.956},
                    "transport_rate": {"mean": 0.942},
                    "total_tokens": {"mean": 908955},
                    "balance": {"mean": 0.8},
                    "idle_ratio": {"mean": 0.15},
                    "step_efficiency": {"mean": 0.57},
                },
                "constraint_violations": {"per_run_violations": 0.44},
                "config_issues": [],
            }
        ],
    }
    return current, baseline


def test_total_collapse_at_repeats_1_is_inconclusive_not_pass():
    """--repeats 1 下的全面崩塌不得以干净 PASS 收场。"""
    current, baseline = _collapse_pair()

    res = evaluate_gate(current, baseline, load_config(None))

    # passed 的含义不变（没有硬 fail），但它不再是唯一的判据。
    assert res.failures == []
    assert res.passed is True
    assert res.inconclusive is True
    # 每条被掩盖的 fail 都带显式标记，而不是靠解析 reason 文本。
    assert len(res.downgraded) >= 5
    assert all(c.status == "warn" and c.downgraded for c in res.downgraded)
    masked = {c.metric for c in res.downgraded}
    assert {"coverage_mean", "transport_rate_mean", "violations_per_run"} <= masked
    assert res.as_dict()["inconclusive"] is True


def test_inconclusive_md_banner_does_not_read_as_clean_pass():
    current, baseline = _collapse_pair()
    res = evaluate_gate(current, baseline, load_config(None))

    md = render_gate_md(res)
    banner = md.splitlines()[0]

    assert "INCONCLUSIVE" in banner
    assert banner != "# SAR Regression Gate — PASS"
    assert "min_runs" in banner
    # 既有的计数/检查表保持原样。
    assert "## 全部检查" in md
    assert "Coverage μ" in md


def test_cli_exit_code_2_when_inconclusive(tmp_path: Path, capsys):
    """CLI 层：无法判定必须与通过区分开，否则 CI 会放行崩塌的批次。"""
    current, baseline = _collapse_pair()
    cur_path = tmp_path / "cur.json"
    base_path = tmp_path / "base.json"
    cur_path.write_text(json.dumps(current), encoding="utf-8")
    base_path.write_text(json.dumps(baseline), encoding="utf-8")

    rc = main(["--results-root", str(cur_path), "--baseline", str(base_path)])

    assert rc == 2
    out = capsys.readouterr().out
    assert "Gate: INCONCLUSIVE (underpowered" in out
    assert "Gate: PASS" not in out
    data = json.loads((tmp_path / "gate_report.json").read_text(encoding="utf-8"))
    assert data["inconclusive"] is True
    assert data["downgraded_count"] >= 5

    # --warn-only 的语义是"永不阻塞"，对 inconclusive 也一样。
    rc = main(
        [
            "--results-root",
            str(cur_path),
            "--baseline",
            str(base_path),
            "--warn-only",
        ]
    )
    assert rc == 0


def test_cli_powered_batch_without_failures_still_plain_pass(tmp_path: Path, capsys):
    """(b) n >= min_runs 且无 fail —— 仍是干净 PASS + 退出码 0。"""
    cur = tmp_path / "cur"
    for seed in (1, 2, 3):
        _write_run(cur, f"r{seed}", _eval_report(seed=seed, coverage=0.95))
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(
        json.dumps(
            {"min_runs": 2, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    rc = main(["--results-root", str(cur), "--config", str(cfg_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Gate: PASS" in out
    assert "INCONCLUSIVE" not in out
    data = json.loads((cur / "gate_report.json").read_text(encoding="utf-8"))
    assert data["passed"] is True
    assert data["inconclusive"] is False
    assert data["downgraded_count"] == 0
    md = (cur / "gate_report.md").read_text(encoding="utf-8")
    assert md.splitlines()[0] == "# SAR Regression Gate — PASS"


def test_cli_powered_batch_with_real_failure_still_fails(tmp_path: Path, capsys):
    """(c) n >= min_runs 且有真 fail —— 仍是 FAIL + 退出码 1，不被 2 抢走。"""
    cur = tmp_path / "cur"
    for seed in (1, 2, 3):
        _write_run(cur, f"r{seed}", _eval_report(seed=seed, coverage=0.1))
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(
        json.dumps(
            {"min_runs": 2, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    rc = main(["--results-root", str(cur), "--config", str(cfg_path)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "Gate: FAIL" in out
    assert "INCONCLUSIVE" not in out
    data = json.loads((cur / "gate_report.json").read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert data["inconclusive"] is False


def test_underpowered_batch_with_zero_failures_is_not_inconclusive(tmp_path: Path, capsys):
    """(d) n < min_runs 但压根没有 fail 可降级 —— 这是真通过，不是无法判定。

    smoke test（--repeats 1）各项都在容差内时报 inconclusive 会让这个信号
    退化成"小批次"的同义词，从而被忽略。只有降级**真的掩盖了 fail** 才标记。
    """
    cur = tmp_path / "cur"
    _write_run(cur, "r1", _eval_report(coverage=0.95))
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(
        json.dumps(
            {"min_runs": 5, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    rc = main(["--results-root", str(cur), "--config", str(cfg_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Gate: PASS" in out
    assert "INCONCLUSIVE" not in out
    data = json.loads((cur / "gate_report.json").read_text(encoding="utf-8"))
    assert data["inconclusive"] is False
    assert data["downgraded_count"] == 0
    # min_runs 本身仍然告警（样本量小这件事必须可见），但它不是被掩盖的 fail。
    warn_metrics = {c["metric"] for c in data["checks"] if c["status"] == "warn"}
    assert "min_runs" in warn_metrics


def test_hard_failure_outranks_downgrade(tmp_path: Path):
    """同批里既有真 fail 又有被掩盖的 fail —— FAIL 优先，不能降级成 2。

    config drift 判 fail 不受 n 影响（同文件既有的刻意设计），所以它正好能
    构造出这个组合：不加区分地把 downgraded 映射到 inconclusive 会让一个
    确定的 FAIL 变成"无法判定"，那是把已知的坏消息变模糊。
    """
    agg = {
        "root_dir": "x",
        "groups": [
            {
                "key": {"scene": 1, "agents": 2},
                "n": 1,
                "finished_count": 0,
                "pass_at_k": {"1": 0.0},
                "episode_stats": {"coverage": {"mean": 0.1}},
                "constraint_violations": {"per_run_violations": 0.0},
                "config_issues": [
                    {"field": "model", "values": {"a": 1, "b": 1}},
                ],
            }
        ],
    }
    cfg = {"min_runs": 3, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}

    res = evaluate_gate(agg, None, cfg)

    assert res.passed is False
    assert res.downgraded  # coverage 的 fail 确实被掩盖了
    assert res.inconclusive is False  # 但整体判定是确定的 FAIL
    assert "INCONCLUSIVE" not in render_gate_md(res).splitlines()[0]


def test_group_missing_from_baseline_warns(tmp_path: Path):
    base = _agg(tmp_path / "base", [("r1", _eval_report(scene=1, agents=2))])
    cur = _agg(tmp_path / "cur", [("r1", _eval_report(scene=5, agents=6))])
    cfg = {"min_runs": 1, "absolute": {}, "regression": {"coverage_mean": 0.1}}

    res = evaluate_gate(cur, base, cfg)

    assert res.passed is True
    reasons = {c.metric for c in res.warnings}
    assert "baseline_group" in reasons  # new group in current
    assert "missing_group" in reasons  # baseline group vanished


def test_empty_current_report_fails(tmp_path: Path):
    res = evaluate_gate({"root_dir": str(tmp_path), "groups": []}, None, DEFAULT_CONFIG)
    assert res.passed is False
    assert res.failures[0].metric == "groups_present"


# ── config ────────────────────────────────────────────────────────────────


def test_load_config_defaults_when_absent():
    cfg = load_config(None)
    assert cfg["min_runs"] == DEFAULT_CONFIG["min_runs"]
    assert cfg["absolute"]["coverage_mean"]["min"] == pytest.approx(0.60)
    # returned dicts must be copies — mutating must not poison DEFAULT_CONFIG
    cfg["absolute"]["coverage_mean"] = {"min": 0.0}
    assert DEFAULT_CONFIG["absolute"]["coverage_mean"]["min"] == pytest.approx(0.60)


def test_load_config_merges_user_overrides(tmp_path: Path):
    p = tmp_path / "gate.json"
    p.write_text(
        json.dumps({"min_runs": 5, "regression": {"coverage_mean": 0.02}}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["min_runs"] == 5
    assert cfg["regression"]["coverage_mean"] == pytest.approx(0.02)
    # untouched sections keep defaults
    assert cfg["absolute"]["coverage_mean"]["min"] == pytest.approx(0.60)
    # 用 pass_at_1 而非 balance_mean 断言"未被覆盖的项保留默认"：
    # balance_mean 已按 DESIGN P5 的显式批准移出 regression（依据是 min/max
    # 结构性惩罚角色分工，非"与成功反相关" —— 后者补检验后 p=0.485 不显著），
    # 它的缺席是**预期状态**。这条断言要检的是浅合并行为，换个仍在门禁里的
    # 指标即可，不必为此把降级项加回来。
    assert cfg["regression"]["pass_at_1"] == pytest.approx(0.20)


def test_load_config_rejects_unknown_metric(tmp_path: Path):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"absolute": {"nonsense": {"min": 1}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown gate metric"):
        load_config(p)


# ── output / resolve / CLI ────────────────────────────────────────────────


def test_write_gate_reports_and_md(tmp_path: Path):
    agg = _agg(tmp_path, [("r1", _eval_report(coverage=0.1))])
    cfg = {"min_runs": 1, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
    res = evaluate_gate(agg, None, cfg)

    json_path, md_path = write_gate_reports(res, None, tmp_path)

    assert json_path == tmp_path / "gate_report.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert data["counts"]["fail"] == 1
    md = md_path.read_text(encoding="utf-8")
    assert "SAR Regression Gate — FAIL" in md
    assert "阻塞项" in md
    assert "Coverage μ" in render_gate_md(res)


def test_resolve_aggregate_from_dir_computes_on_the_fly(tmp_path: Path):
    _write_run(tmp_path, "benchmark/scene_1/agents_2/seed_42", _eval_report())
    agg = resolve_aggregate(tmp_path)
    assert agg["num_groups"] == 1


def test_resolve_aggregate_prefers_existing_report(tmp_path: Path):
    (tmp_path / "aggregate_report.json").write_text(
        json.dumps({"root_dir": "sentinel", "groups": []}), encoding="utf-8"
    )
    assert resolve_aggregate(tmp_path)["root_dir"] == "sentinel"


def test_resolve_aggregate_missing_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        resolve_aggregate(tmp_path / "nope")


def test_cli_exit_codes(tmp_path: Path, capsys):
    cur = tmp_path / "cur"
    _write_run(cur, "r1", _eval_report(coverage=0.1))
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(
        json.dumps(
            {"min_runs": 1, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    rc = main(["--results-root", str(cur), "--config", str(cfg_path)])
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out
    assert (cur / "gate_report.json").exists()

    rc = main(
        ["--results-root", str(cur), "--config", str(cfg_path), "--warn-only"]
    )
    assert rc == 0


def test_cli_bad_config_returns_2(tmp_path: Path, capsys):
    cur = tmp_path / "cur"
    _write_run(cur, "r1", _eval_report())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    rc = main(["--results-root", str(cur), "--config", str(bad)])
    assert rc == 2
    assert "invalid gate config" in capsys.readouterr().err


def test_cli_baseline_regression_flow(tmp_path: Path):
    base = tmp_path / "base"
    cur = tmp_path / "cur"
    _write_run(base, "r1", _eval_report(coverage=1.0))
    _write_run(cur, "r1", _eval_report(coverage=0.2))
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(
        json.dumps({"min_runs": 1, "absolute": {}, "regression": {"coverage_mean": 0.1}}),
        encoding="utf-8",
    )

    rc = main(
        [
            "--results-root",
            str(cur),
            "--baseline",
            str(base),
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == 1
    data = json.loads((cur / "gate_report.json").read_text(encoding="utf-8"))
    assert data["baseline_root"] == str(base)

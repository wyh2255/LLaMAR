"""benchmark.py 的 --eval / --gate 接线测试（不启动任何实验进程）."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import sar_orch.benchmark as bm


def _args(**kw) -> argparse.Namespace:
    base = {
        "eval": False,
        "gate": False,
        "gate_baseline": None,
        "gate_config": None,
        "gate_warn_only": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _runs(statuses: list[str]) -> list[bm.BenchmarkRun]:
    return [
        bm.BenchmarkRun(scene=1, agents=2, seed=42 + i, status=s)
        for i, s in enumerate(statuses)
    ]


def test_finalize_without_flags_skips_eval(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    called = []
    monkeypatch.setattr(
        bm, "evaluate_run_dirs", lambda dirs: called.append(dirs) or (0, 0, 0)
    )

    rc = bm._finalize(_args(), _runs(["success"]))

    assert rc == 0
    assert called == []
    assert (tmp_path / "index.json").exists()
    assert "BENCHMARK SUMMARY" in capsys.readouterr().out


def test_finalize_eval_only_returns_zero_even_on_eval_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: (1, 1, 0))
    gate_called = []
    monkeypatch.setattr(
        bm, "run_gate", lambda *a: gate_called.append(a) or True
    )

    rc = bm._finalize(_args(eval=True), _runs(["success"]))

    assert rc == 0
    assert gate_called == []


def test_finalize_gate_failure_exits_one(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: (1, 0, 0))
    monkeypatch.setattr(bm, "run_gate", lambda *a, **kw: False)

    assert bm._finalize(_args(gate=True), _runs(["success"])) == 1


def test_finalize_gate_pass_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: (1, 0, 0))
    monkeypatch.setattr(bm, "run_gate", lambda *a, **kw: True)

    assert bm._finalize(_args(gate=True), _runs(["success"])) == 0


def test_finalize_gate_crash_exits_two(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: (1, 0, 0))

    def boom(*a):
        raise RuntimeError("no aggregate")

    monkeypatch.setattr(bm, "run_gate", boom)

    assert bm._finalize(_args(gate=True), _runs(["success"])) == 2


def test_interrupted_sweep_skips_eval_and_gate(tmp_path, monkeypatch):
    """Ctrl-C leaves partial data; a gate verdict over it would be misleading."""
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    called = []
    monkeypatch.setattr(
        bm, "evaluate_run_dirs", lambda dirs: called.append(dirs) or (0, 0, 0)
    )
    monkeypatch.setattr(bm, "run_gate", lambda *a: False)
    bm._shutdown_event.set()
    try:
        rc = bm._finalize(_args(gate=True), _runs(["success"]))
    finally:
        bm._shutdown_event.clear()

    assert rc == 0
    assert called == []
    # index.json is still written — the partial sweep record is useful
    assert (tmp_path / "index.json").exists()


def test_finalize_gate_implies_eval(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: seen.append(dirs) or (1, 0, 0))
    monkeypatch.setattr(bm, "run_gate", lambda *a: True)

    bm._finalize(_args(gate=True, eval=False), _runs(["success"]))

    assert len(seen) == 1


def test_eval_dirs_exclude_failed_and_nonexistent(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    runs = _runs(["success", "failed", "timeout", "skipped"])
    # Only create dirs for the success + timeout runs.
    for r in runs:
        if r.status in ("success", "timeout"):
            bm.run_dir(r).mkdir(parents=True, exist_ok=True)

    seen: list[list[Path]] = []
    monkeypatch.setattr(bm, "evaluate_run_dirs", lambda dirs: seen.append(dirs) or (2, 0, 0))

    bm._finalize(_args(eval=True), runs)

    assert len(seen) == 1
    names = {d.name for d in seen[0]}
    assert names == {"seed_42", "seed_44"}  # success + timeout; failed/skipped absent


def test_evaluate_run_dirs_skips_dirs_without_trajectory(tmp_path):
    """No trajectory.csv → skip, not a hollow eval_report.

    ``load_episode`` degrades gracefully on missing files, so evaluating a
    crashed run would emit an empty episode block that the aggregator counts
    as a failed episode — infrastructure noise masquerading as regression.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "metadata.json").write_text(
        json.dumps({"scene": 1, "agent_count": 2, "seed": 42}), encoding="utf-8"
    )

    ok, failed, skipped = bm.evaluate_run_dirs(
        [empty, partial, tmp_path / "missing"]
    )

    assert (ok, failed, skipped) == (0, 0, 3)
    assert not (empty / "eval_report.json").exists()
    assert not (partial / "eval_report.json").exists()


_REAL_RUN = (
    Path(__file__).resolve().parents[1]
    / "sar_orch/results/20260719_141217_s2_s42_a4"
)


@pytest.mark.skipif(
    not (_REAL_RUN / "trajectory.csv").exists(),
    reason="reference run dir absent (results/ is gitignored)",
)
def test_evaluate_run_dirs_end_to_end_on_reference_run(tmp_path):
    """Deterministic eval over a real run dir produces a usable attempt-family report."""
    import shutil

    dst = tmp_path / "seed_42"
    dst.mkdir()
    for name in (
        "trajectory.csv",
        "summary.csv",
        "metadata.json",
        "agent_interactions.csv",
        "router_interactions.csv",
        "subtasks.csv",
        "token_usage.csv",
        "run_metrics.json",
    ):
        src = _REAL_RUN / name
        if src.exists():
            shutil.copy2(src, dst / name)

    ok, failed, skipped = bm.evaluate_run_dirs([dst])

    assert (ok, failed, skipped) == (1, 0, 0)
    # P5: attempt-family report lives under eval_attempts/<run>/attempts/<id>/reports/,
    # and the run dir root has no legacy eval_report.json (families never mix).
    reports = list((dst / "eval_attempts").rglob("reports/eval_report.json"))
    assert len(reports) == 1
    assert not (dst / "eval_report.json").exists()
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["report_family"] == "attempt-v2"
    assert report["metadata"]["scene"] is not None
    assert report["episode"]["coverage"] is not None
    # deterministic-only: no judge block content
    assert not report.get("llm_judge")


def test_run_gate_reads_results_dir_and_writes_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    run = tmp_path / "scene_1" / "agents_2" / "seed_42"
    run.mkdir(parents=True)
    (run / "eval_report.json").write_text(
        json.dumps(
            {
                "run_dir": str(run),
                "metadata": {"scene": 1, "agents": 2, "seed": 42},
                "episode": {
                    "finished": True,
                    "coverage": 1.0,
                    "transport_rate": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = tmp_path / "gate.json"
    cfg.write_text(
        json.dumps(
            {"min_runs": 1, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    assert bm.run_gate(None, str(cfg), False) is True
    assert (tmp_path / "gate_report.json").exists()
    assert "REGRESSION GATE: PASS" in capsys.readouterr().out


def test_run_gate_warn_only_reports_true_despite_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_RESULTS_DIR", tmp_path)
    run = tmp_path / "scene_1" / "agents_2" / "seed_42"
    run.mkdir(parents=True)
    (run / "eval_report.json").write_text(
        json.dumps(
            {
                "run_dir": str(run),
                "metadata": {"scene": 1, "agents": 2, "seed": 42},
                "episode": {"finished": False, "coverage": 0.1, "transport_rate": 0.1},
            }
        ),
        encoding="utf-8",
    )
    cfg = tmp_path / "gate.json"
    cfg.write_text(
        json.dumps(
            {"min_runs": 1, "absolute": {"coverage_mean": {"min": 0.6}}, "regression": {}}
        ),
        encoding="utf-8",
    )

    assert bm.run_gate(None, str(cfg), False) is False
    assert bm.run_gate(None, str(cfg), True) is True

"""Tests for AI2Thor benchmark metric aggregation."""

from __future__ import annotations

from ai2thor_orch.benchmark import BenchmarkRun, _apply_summary_metrics


def test_apply_summary_metrics_reads_v2_schema_without_losing_coverage_compatibility():
    run = BenchmarkRun()

    _apply_summary_metrics(
        run,
        {
            "verified_completion": True,
            "rounds_completed": 7,
            "coverage": 0.8,
            "goal_coverage": 0.8,
            "interaction_coverage": 0.5,
            "transport_rate": 0.625,
            "action_success_rate": 0.9,
            "timeout_count": 2,
            "balance": 0.75,
            "log_dir": "/tmp/run",
        },
    )

    assert run.verified_completion is True
    assert run.rounds == 7
    assert run.coverage == 0.8
    assert run.goal_coverage == 0.8
    assert run.interaction_coverage == 0.5
    assert run.transport_rate == 0.625
    assert run.action_success_rate == 0.9
    assert run.timeout_count == 2
    assert run.balance == 0.75
    assert run.log_dir == "/tmp/run"


def test_apply_summary_metrics_uses_legacy_coverage_as_goal_coverage_fallback():
    run = BenchmarkRun()

    _apply_summary_metrics(run, {"coverage": 0.4})

    assert run.coverage == 0.4
    assert run.goal_coverage == 0.4
    assert run.interaction_coverage == 0.0
    assert run.transport_rate == 0.0

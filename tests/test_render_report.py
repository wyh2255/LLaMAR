import sys
from pathlib import Path

import pytest

_skills_path = str(Path(__file__).parent.parent / "skills" / "render-sar-report")
if _skills_path not in sys.path:
    sys.path.insert(0, _skills_path)

from render_sar_report.cli import main  # noqa: E402
from render_sar_report.html import render_html  # noqa: E402
from render_sar_report.loaders import load_report_data  # noqa: E402


@pytest.fixture
def real_results_dir() -> Path:
    return (
        Path(__file__).parent.parent
        / "sar_orch"
        / "results"
        / "sar_experiment_20260705_192717"
    )


@pytest.fixture
def real_logs_dir() -> Path:
    return Path(__file__).parent.parent / "logs"


def test_load_report_data(real_results_dir: Path, real_logs_dir: Path):
    data = load_report_data(real_results_dir, real_logs_dir)
    assert data.meta is not None
    assert data.meta.scene == 1
    assert data.meta.agent_count == 2
    assert data.metrics is not None
    assert data.metrics.steps == 30
    assert len(data.steps) == 30
    assert len(data.tokens) > 0
    assert len(data.agent_tokens) >= 2
    assert any(t.agent == "Alice" for t in data.agent_tokens)
    assert any(t.agent == "Bob" for t in data.agent_tokens)
    assert len(data.semantic_objects) > 0
    assert len(data.llm_traces) > 0


def test_render_html_contains_expected_sections(
    real_results_dir: Path, real_logs_dir: Path
):
    data = load_report_data(real_results_dir, real_logs_dir)
    html = render_html(data)
    assert html.startswith("<!DOCTYPE html>")
    assert "SAR Experiment Report" in html
    assert 'id="content-timeline"' in html
    assert 'id="content-coordinator"' in html
    assert 'id="content-tokens"' in html
    assert 'id="content-semantic"' in html
    assert 'id="content-llm"' in html
    assert "Pos:" in html


def test_render_report_cli(tmp_path: Path, real_results_dir: Path, real_logs_dir: Path):
    output = tmp_path / "test_report.html"
    rc = main(
        [
            "--results-dir",
            str(real_results_dir),
            "--logs-dir",
            str(real_logs_dir),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert content.startswith("<!DOCTYPE html>")
    assert "SAR Experiment Report" in content

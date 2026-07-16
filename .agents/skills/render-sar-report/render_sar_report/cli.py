import argparse
import sys
from pathlib import Path

from render_sar_report.html import render_html
from render_sar_report.loaders import load_report_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a SAR experiment run into a self-contained HTML report."
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        type=Path,
        help="Path to the experiment results directory (e.g. sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS).",
    )
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=Path,
        help="Path to the logs directory (e.g. sar_orch/logs).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output HTML path. Defaults to <results-dir>/report.html.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    results_dir = args.results_dir.resolve()
    logs_dir = args.logs_dir.resolve()
    output_path = args.output or results_dir / "report.html"

    if not results_dir.exists():
        print(f"ERROR: results directory not found: {results_dir}", file=sys.stderr)
        return 1

    data = load_report_data(results_dir, logs_dir)
    html = render_html(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

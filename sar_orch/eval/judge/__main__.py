"""CLI entry point: ``python -m sar_orch.eval.judge``.

Exit codes:
    0  artifact written; at least one requested metric reached ``ok``
    2  usage error (bad arguments / unknown metric name / bad sample size)
    3  results dir missing or not a directory
    4  judge model unconfigured (no usable ``.env`` keys) — artifact still
       written with ``judge_unconfigured`` statuses
    5  every requested metric ended in a non-ok status (nothing measurable)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import (
    DEFAULT_OUTPUT,
    DEFAULT_SAMPLE_SIZE,
    METRIC_NAMES,
    evaluate_run,
    write_artifact,
)
from .client import JudgeClient, build_judge_client

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID_INPUT = 3
EXIT_JUDGE_UNCONFIGURED = 4
EXIT_METRICS_FAILED = 5


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sar_orch.eval.judge",
        description="V1-Judge metrics (sar-metrics v1): planning_path + "
        "observation_ignore",
    )
    parser.add_argument("--results-dir", required=True, help="Run results directory")
    parser.add_argument(
        "--metrics",
        default=",".join(METRIC_NAMES),
        help="Comma-separated metric names (default: both)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="observation_ignore sample pairs per run (default: 20)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=f"Output path (default: <results_dir>/{DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Env file with the judge model keys (default: repo-root .env)",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: JudgeClient | None = None,
    judge_info: dict[str, Any] | None = None,
) -> int:
    """Run the judge CLI.  ``client`` is a test seam; None builds the real one."""
    args = _parse_args(argv)
    run_dir = Path(args.results_dir)
    if not run_dir.is_dir():
        print(f"sar_orch.eval.judge: results dir not found: {run_dir}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    metrics = [name.strip() for name in str(args.metrics).split(",") if name.strip()]
    unknown = [name for name in metrics if name not in METRIC_NAMES]
    if not metrics or unknown:
        print(
            "sar_orch.eval.judge: --metrics must be a comma-separated subset of "
            f"{', '.join(METRIC_NAMES)} (got: {args.metrics})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.sample_size < 1:
        print("sar_orch.eval.judge: --sample-size must be >= 1", file=sys.stderr)
        return EXIT_USAGE

    if client is None and judge_info is None:
        client, config = build_judge_client(args.env_file)
        judge_info = config.public_info() if config is not None else None
        if client is None:
            print(
                "sar_orch.eval.judge: judge model unconfigured — no usable "
                "reflection_*/generic keys in "
                f"{args.env_file or '<repo-root .env>'}; writing "
                "judge_unconfigured statuses",
                file=sys.stderr,
            )

    payload = evaluate_run(
        run_dir,
        metrics=metrics,
        sample_size=args.sample_size,
        client=client,
        judge_info=judge_info,
    )
    target = write_artifact(run_dir, payload, args.output)

    statuses = {name: payload["metrics"][name].get("status") for name in metrics}
    print(f"sar_orch.eval.judge: wrote {target}")
    for name, status in statuses.items():
        print(f"  - {name}: {status}")
    if client is None:
        return EXIT_JUDGE_UNCONFIGURED
    if all(status != "ok" for status in statuses.values()):
        return EXIT_METRICS_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

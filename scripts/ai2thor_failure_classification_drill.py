#!/usr/bin/env python3
"""离线失败归类演练 —— 把录制 run 的失败信息喂进错误分类器。

用途
----
读取录制 run 的 ``trajectory.csv`` ``ErrorTypes`` 列（每条为该回合真实
失败描述符，逐字原样），逐条喂给 ``Agent.error_taxonomy.classify_error``，
输出映射分布表：多少条落到哪个类目、还剩多少 ``unclassified``。

默认样本 = A100 首跑录制
（``reports/a100_firstrun_20260914/l3_full`` 与 ``l3_short``）；也可用
``--run-dir`` 指向任意 run 目录重复演练（可传多个）。

同时统计 ``agent_interactions.csv`` 的失败行数与其历史 ``ErrorType``
（旧分类器产物，对照用：这些失败在录制时全部落 ``unclassified_tool_error``）。

用法
----
    uv run python scripts/ai2thor_failure_classification_drill.py
    uv run python scripts/ai2thor_failure_classification_drill.py --json
    uv run python scripts/ai2thor_failure_classification_drill.py \
        --run-dir sar_orch/results/benchmark/... # 任意 run 目录

退出码：0 = 演练完成；1 = 没有读到任何失败样本。
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from Agent.error_taxonomy import UNCLASSIFIED_TOOL_ERROR, classify_error

_DEFAULT_RUN_DIRS = (
    _ROOT / "reports" / "a100_firstrun_20260914" / "l3_full",
    _ROOT / "reports" / "a100_firstrun_20260914" / "l3_short",
)


def _read_failure_descriptors(run_dir: Path) -> list[str]:
    """trajectory.csv ErrorTypes 列（Python-repr 列表）→ 逐条失败描述符。"""
    out: list[str] = []
    with (run_dir / "trajectory.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("ErrorTypes") or "").strip()
            if not raw:
                continue
            try:
                items = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                try:
                    items = json.loads(raw)
                except ValueError:
                    items = [raw]
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, (list, tuple)):
                items = [items]
            for item in items:
                text = str(item).strip()
                if text:
                    out.append(text)
    return out


def _count_agent_interactions(run_dir: Path) -> tuple[int, int, Counter[str]]:
    """返回 (总行数, 失败行数, 失败行历史 ErrorType 计数)。"""
    path = run_dir / "agent_interactions.csv"
    if not path.exists():
        return 0, 0, Counter()
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    failures = [
        row
        for row in rows
        if str(row.get("Success", "")).strip().lower() not in {"true", "1"}
    ]
    historical = Counter((row.get("ErrorType") or "").strip() for row in failures)
    return len(rows), len(failures), historical


def run_drill(run_dirs: list[Path]) -> dict:
    report: dict = {"runs": [], "totals": {}}
    totals: Counter[str] = Counter()
    for run_dir in run_dirs:
        descriptors = _read_failure_descriptors(run_dir)
        classified = Counter(classify_error(text) for text in descriptors)
        totals.update(classified)
        rows, failures, historical = _count_agent_interactions(run_dir)
        report["runs"].append(
            {
                "run_dir": str(run_dir),
                "trajectory_failure_descriptors": len(descriptors),
                "classification": dict(classified),
                "unclassified_samples": [
                    text
                    for text in descriptors
                    if classify_error(text) == UNCLASSIFIED_TOOL_ERROR
                ],
                "agent_interactions_rows": rows,
                "agent_interactions_failures": failures,
                "historical_error_types": dict(historical),
            }
        )
    report["totals"] = dict(totals)
    return report


def _print_report(report: dict) -> None:
    for run in report["runs"]:
        print(f"\n=== {run['run_dir']} ===")
        print(f"trajectory 失败描述符: {run['trajectory_failure_descriptors']}")
        for code, count in sorted(run["classification"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:4d}  {code}")
        for sample in run["unclassified_samples"][:5]:
            print(f"    (unclassified 样本) {sample[:140]}")
        print(
            "agent_interactions.csv: 失败 {fails} / {rows} 行；历史 ErrorType: {hist}".format(
                fails=run["agent_interactions_failures"],
                rows=run["agent_interactions_rows"],
                hist=run["historical_error_types"],
            )
        )
    total = report["totals"]
    n_all = sum(total.values())
    print("\n=== 汇总（全部 run 的 trajectory 失败描述符） ===")
    for code, count in sorted(total.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {code}")
    mapped = n_all - total.get(UNCLASSIFIED_TOOL_ERROR, 0)
    print(f"  ---- 共 {n_all} 条，已归类 {mapped}，未归类 {n_all - mapped}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-dir",
        action="append",
        default=None,
        help="run 目录（可重复；缺省 = A100 首跑 l3_full + l3_short）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    if args.run_dir:
        run_dirs = [Path(p) for p in args.run_dir]
    else:
        run_dirs = list(_DEFAULT_RUN_DIRS)

    usable: list[Path] = []
    for run_dir in run_dirs:
        if (run_dir / "trajectory.csv").exists():
            usable.append(run_dir)
        else:
            print(f"missing trajectory.csv: {run_dir}", file=sys.stderr)
    if not usable:
        return 1

    report = run_drill(usable)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0 if sum(report["totals"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

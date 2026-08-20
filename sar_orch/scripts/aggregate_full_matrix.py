#!/usr/bin/env python3
"""聚合全功能矩阵（peer mail + long-term read + diagnosis + read_port）。

用法:
    uv run python sar_orch/scripts/aggregate_full_matrix.py [--dir <结果父目录>] [--out <json>]

按目录扫描 scene_*_agents_* 子目录（每 run 一个目录，含 run_metrics.json /
agent_interactions.csv），输出:
    - 每 run: cov / tr / steps / finished / end_reason
    - memory acceptance: failed_tool_rows + 3 类 framework error
    - long-term: written / diagnosis status / rounds / written / SH 注入情况
    - peer mail: a2a_send_mail 调用数、成功投递数、read_mailbox 数
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

_FW_ERRORS = ("worker_busy", "task_not_routable_yet", "unknown_task_id")


def _csv_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.reader(f)]


def count_tool_calls(interactions_path: Path) -> dict:
    """统计 agent_interactions.csv 里 peer mail 相关工具调用。"""
    counts = {"a2a_send_mail": 0, "read_mailbox": 0, "send_delivered": 0}
    for row in _csv_rows(interactions_path):
        if len(row) < 6:
            continue
        tool, obs = row[2].strip('"'), row[5]
        if tool == "a2a_send_mail":
            counts["a2a_send_mail"] += 1
            if "delivered" in obs.lower():
                counts["send_delivered"] += 1
        elif tool == "read_mailbox":
            counts["read_mailbox"] += 1
    return counts


def diagnosis_store_rows(diag_db: Path) -> int:
    if not diag_db.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{diag_db}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            )
            tables = [r[0] for r in cur.fetchall()]
            if not tables:
                return 0
            for t in tables:
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    if n:
                        return n
                except sqlite3.DatabaseError:
                    continue
            return 0
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return 0


def analyze_run(rundir: Path) -> dict:
    m = rundir / "run_metrics.json"
    metrics = json.loads(m.read_text()) if m.exists() else {}
    res = {
        "name": rundir.name,
        "steps": metrics.get("steps", 0),
        "coverage": metrics.get("coverage", 0.0),
        "transport_rate": metrics.get("transport_rate", 0.0),
        "finished": metrics.get("finished", False),
        "end_reason": metrics.get("end_reason", ""),
    }
    # memory acceptance / framework errors
    errs = {"worker_busy": 0, "task_not_routable_yet": 0, "unknown_task_id": 0}
    mem = metrics.get("memory_terminal", {}) or {}
    acc = mem.get("acceptance", {}) or {}
    for k in _FW_ERRORS:
        errs[k] = acc.get("framework_error_counts", {}).get(k, 0)
    res["framework_errors"] = errs
    res["acceptance_gate"] = mem.get("acceptance_gate", "missing")
    res["failed_tool_rows"] = acc.get("failed_tool_rows", None)
    # long-term + diagnosis
    lt = metrics.get("long_term_reflection", {}) or {}
    res["long_term_written"] = lt.get("long_term_memory_written", None)
    diag = lt.get("diagnosis", {}) or {}
    res["diagnosis_status"] = diag.get("status", "none")
    res["diagnosis_rounds"] = diag.get("rounds", 0)
    res["diagnosis_written"] = diag.get("written", 0)
    res["diagnosis_reason"] = diag.get("reason")
    # diagnosis store rows
    diag_db = rundir / "coordinator" / "diagnosis" / "diagnosis.sqlite3"
    res["diagnosis_store_rows"] = diagnosis_store_rows(diag_db)
    # peer mail
    res.update(count_tool_calls(rundir / "agent_interactions.csv"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dir",
        default=str(Path(__file__).resolve().parent.parent / "results"),
        help="结果父目录（含 scene_*_agents_* 子目录）",
    )
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    args = ap.parse_args()
    base = Path(args.dir)
    rundirs = sorted(
        d
        for d in base.iterdir()
        if d.is_dir() and "scene_" in d.name and "agents_" in d.name
    )
    if not rundirs:
        print(f"未找到 scene_*_agents_* 子目录 under {base}")
        return
    runs = [analyze_run(d) for d in rundirs]
    valid = [r for r in runs if r["steps"] > 0]
    fw = {k: sum(r["framework_errors"][k] for r in runs) for k in _FW_ERRORS}
    summary = {
        "run_count": len(runs),
        "valid_run_count": len(valid),
        "valid_avg_coverage": (
            round(sum(r["coverage"] for r in valid) / len(valid), 4) if valid else None
        ),
        "valid_avg_transport_rate": (
            round(sum(r["transport_rate"] for r in valid) / len(valid), 4)
            if valid
            else None
        ),
        "finished_count": sum(1 for r in runs if r["finished"]),
        "framework_errors_total": fw,
        "zero_framework_errors": all(v == 0 for v in fw.values()),
        "peer_mail_send_total": sum(r["a2a_send_mail"] for r in runs),
        "peer_mail_delivered_total": sum(r["send_delivered"] for r in runs),
        "peer_mail_read_total": sum(r["read_mailbox"] for r in runs),
        "diagnosis_status_distribution": {
            s: sum(1 for r in runs if r["diagnosis_status"] == s)
            for s in sorted({r["diagnosis_status"] for r in runs})
        },
        "diagnosis_store_nonempty_runs": sum(
            1 for r in runs if r["diagnosis_store_rows"] > 0
        ),
        "acceptance_gate_distribution": {
            s: sum(1 for r in runs if r["acceptance_gate"] == s)
            for s in sorted({r["acceptance_gate"] for r in runs})
        },
    }
    out = {"summary": summary, "per_run": runs}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"written: {args.out}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

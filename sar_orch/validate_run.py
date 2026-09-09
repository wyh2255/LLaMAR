#!/usr/bin/env python3
"""Validate a SAR run directory against the documented output contract.

L1 (default, current contract): checks existence and key columns of the
artifacts listed in AGENTS.md "Output Files":
    trajectory.csv / agent_interactions.csv / router_interactions.csv /
    token_usage.csv / summary.csv / events.ndjson / subtasks.csv /
    metadata.json / run_metrics.json

L2 (target shape): checks the target run-directory layout described in
docs/plans/trajectory-audit/README.md section 6 (appendix; all of §3
recommendations implemented). Each item prints one line:
    PASS: <item>
    FAIL: <item> - <reason>          (artifact exists but shape is wrong)
    MISSING(target): <item> - <why>  (artifact/key entirely absent)

Exit code is 0 only when every checked item passes; any FAIL or MISSING
yields exit code 1 (L2 on historical runs is expected to fail loudly).

Pure stdlib (csv/json/sqlite3/argparse/pathlib). Usage:
    uv run python sar_orch/validate_run.py --results-dir <run_dir> [--mode l1|l2]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# L1: current contract per AGENTS.md "Output Files"
# ---------------------------------------------------------------------------

L1_FILES = [
    "trajectory.csv",
    "agent_interactions.csv",
    "router_interactions.csv",
    "token_usage.csv",
    "summary.csv",
    "events.ndjson",
    "subtasks.csv",
    "metadata.json",
    "run_metrics.json",
]

L1_CSV_COLUMNS = {
    "trajectory.csv": ["Step", "Actions", "Coverage", "TransportRate", "TimeoutAgents"],
    "agent_interactions.csv": ["Step", "Agent", "ToolName", "ToolArgs", "Observation", "LLMOutput"],
    "router_interactions.csv": ["Step", "Subtask", "AssignedTo", "EventType"],
    "token_usage.csv": [
        "Step", "Agent", "PromptTokens", "CompletionTokens", "TotalTokens",
        "CacheHitTokens", "CacheMissTokens",
    ],
    "subtasks.csv": ["RunID", "SubtaskID", "Status", "AssignedTo", "Subtask"],
}

# summary.csv: aggregate metrics plus per-agent cumulative token totals
SUMMARY_COLUMNS = ["TotalSteps", "FinalCoverage", "FinalTransportRate", "Finished"]
SUMMARY_TOKEN_COLUMN_HINT = "TotalTokens"

METADATA_KEYS = ["scene", "seed", "agent_count", "model", "prompt_version", "code_commit"]
RUN_METRICS_KEYS = ["run_id", "steps", "coverage", "transport_rate", "finished"]


class Counts:
    """Running tallies of PASS / FAIL / MISSING(target) outcomes."""

    def __init__(self) -> None:
        self.pass_ = 0
        self.fail = 0
        self.missing = 0

    def total(self) -> int:
        return self.fail + self.missing


def read_csv_header(path: Path) -> list[str] | None:
    """Return the header row of a CSV, or None when unreadable/empty."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if row:
                    return [c.strip() for c in row]
    except (OSError, csv.Error):
        return None
    return None


def read_json(path: Path) -> dict | None:
    """Load a JSON object from a file, or None when missing/unparseable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_ndjson_events(path: Path) -> tuple[int, int]:
    """Return (lines_with_event_type, total_nonempty_lines) of an NDJSON file."""
    good = 0
    total = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and "event_type" in rec:
                        good += 1
                except json.JSONDecodeError:
                    pass
    except OSError:
        return 0, 0
    return good, total


def validate_l1(results_dir: Path, out, counts: Counts) -> None:
    """L1: existence + key columns per AGENTS.md Output Files."""
    # 1) file existence
    for name in L1_FILES:
        if (results_dir / name).is_file():
            out(f"PASS: {name} (exists)")
            counts.pass_ += 1
        else:
            out(f"MISSING(target): {name} - artifact absent")
            counts.missing += 1

    # 2) key CSV columns (only for files that exist)
    for name, required in L1_CSV_COLUMNS.items():
        path = results_dir / name
        header = read_csv_header(path) if path.is_file() else None
        if header is None:
            if path.exists():
                out(f"FAIL: {name} - unreadable or empty CSV")
                counts.fail += 1
            continue  # missing file already reported above
        missing = [c for c in required if c not in header]
        if missing:
            out("FAIL: {} - missing key column(s): {} (actual: {})".format(name, ", ".join(missing), ", ".join(header)))
            counts.fail += 1
        else:
            out(f"PASS: {name} key columns ok")
            counts.pass_ += 1

    # 3) summary.csv aggregate + per-agent token columns
    path = results_dir / "summary.csv"
    header = read_csv_header(path) if path.is_file() else None
    if header is not None:
        missing = [c for c in SUMMARY_COLUMNS if c not in header]
        has_token_col = any(c.endswith(SUMMARY_TOKEN_COLUMN_HINT) for c in header)
        if missing or not has_token_col:
            out("FAIL: summary.csv - aggregate column(s) missing: {}; per-agent {} column(s): {}".format(", ".join(missing) if missing else "none",
                   SUMMARY_TOKEN_COLUMN_HINT,
                   "absent" if not has_token_col else "present"))
            counts.fail += 1
        else:
            out("PASS: summary.csv aggregate + per-agent token columns ok")
            counts.pass_ += 1
    elif path.exists():
        out("FAIL: summary.csv - unreadable or empty CSV")
        counts.fail += 1

    # 4) events.ndjson: every line must parse and carry event_type
    path = results_dir / "events.ndjson"
    if path.is_file():
        good, total = check_ndjson_events(path)
        if total == 0:
            out("FAIL: events.ndjson - empty file")
            counts.fail += 1
        elif good != total:
            out(f"FAIL: events.ndjson - {good}/{total} lines parse with event_type")
            counts.fail += 1
        else:
            out(f"PASS: events.ndjson ({total} lines, all with event_type)")
            counts.pass_ += 1

    # 5) metadata.json / run_metrics.json required keys
    for name, keys in (("metadata.json", METADATA_KEYS), ("run_metrics.json", RUN_METRICS_KEYS)):
        path = results_dir / name
        data = read_json(path) if path.is_file() else None
        if data is None:
            if path.exists():
                out(f"FAIL: {name} - unparseable or non-object JSON")
                counts.fail += 1
            continue  # missing file already reported above
        missing = [k for k in keys if k not in data]
        if missing:
            out("FAIL: {} - missing key(s): {}".format(name, ", ".join(missing)))
            counts.fail += 1
        else:
            out(f"PASS: {name} required keys ok")
            counts.pass_ += 1


# ---------------------------------------------------------------------------
# L2: target shape per docs/plans/trajectory-audit/README.md section 6
# ---------------------------------------------------------------------------

# The four router decision kinds that must all appear in the router CSV
# (coordinator.py _log_send_message docstring).
ACTION_TYPES = ["assign_task", "reply_to_help", "cancel_task", "activate_plan_node"]
ROUTER_TARGET_COLUMNS = 9

SUBTASK_TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def csv_column_values(path: Path, column: str, limit: int = 1_000_000) -> set[str]:
    """All distinct values of one CSV column (bounded reads)."""
    values: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                val = row.get(column)
                if val:
                    values.add(val.strip())
    except (OSError, csv.Error):
        pass
    return values


def ndjson_has_event(dir_: Path, event: str) -> dict[str, bool]:
    """For each direct subdirectory of dir_, whether any nested *.ndjson
    contains a line with event == <event>. Fast substring pre-check first;
    only candidate files/lines are fully parsed."""
    result: dict[str, bool] = {}
    if not dir_.is_dir():
        return result
    needle = f'"{event}"'
    for agent_dir in sorted(p for p in dir_.iterdir() if p.is_dir()):
        found = False
        for nd in sorted(agent_dir.rglob("*.ndjson")):
            try:
                text = nd.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle not in text:
                continue
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("event") == event:
                    found = True
                    break
            if found:
                break
        result[agent_dir.name] = found
    return result


def ndjson_top_level_key(files: list[Path], key: str) -> bool:
    """Whether any line of any file in <files> has top-level dict key <key>."""
    needle = f'"{key}"'
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle not in text:
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and key in rec:
                return True
    return False


def validate_l2(results_dir: Path, out, counts: Counts) -> None:
    """L2: target shape per README section 6."""

    # --- metadata.json extensions (#11) -------------------------------------
    meta = read_json(results_dir / "metadata.json")
    if meta is None:
        out("MISSING(target): metadata.json - cannot evaluate L2 metadata items")
        counts.missing += 1
        meta = {}

    sha_key = next((k for k in meta if "sha256" in k.lower() or "prompt_sha" in k.lower()), None)
    if sha_key:
        out(f"PASS: metadata.{sha_key} (worker prompt sha256 fingerprint)")
        counts.pass_ += 1
    else:
        out("MISSING(target): metadata worker prompt sha256 fingerprint key (e.g. worker_prompt_sha256)")
        counts.missing += 1

    truth_dir_val = meta.get("truth_dir")
    if isinstance(truth_dir_val, str) and truth_dir_val:
        truth_dir = Path(truth_dir_val)
        if truth_dir.is_dir() and (truth_dir / "truth_trace.jsonl").is_file() \
                and (truth_dir / "truth_manifest.json").is_file():
            out(f"PASS: metadata.truth_dir={truth_dir_val} (truth_trace.jsonl + truth_manifest.json present)")
            counts.pass_ += 1
        else:
            out(f"FAIL: metadata.truth_dir={truth_dir_val} - target dir or truth files missing")
            counts.fail += 1
    else:
        out("MISSING(target): metadata.truth_dir pointer (#11; truth stays outside the run dir)")
        counts.missing += 1

    # --- scene_config.json (#13) --------------------------------------------
    if (results_dir / "scene_config.json").is_file():
        out("PASS: scene_config.json (initial grid/object layout snapshot)")
        counts.pass_ += 1
    else:
        out("MISSING(target): scene_config.json (#13)")
        counts.missing += 1

    # --- trajectory.csv NoOp source column (#8) ------------------------------
    traj_path = results_dir / "trajectory.csv"
    traj_header = read_csv_header(traj_path) if traj_path.is_file() else None
    if traj_header is None:
        if traj_path.exists():
            out("FAIL: trajectory.csv - unreadable or empty CSV")
            counts.fail += 1
        else:
            out("MISSING(target): trajectory.csv - cannot check NoOp source column")
            counts.missing += 1
    else:
        noop_cols = [c for c in traj_header if "noop" in c.lower() and c.lower() != "actions"]
        if noop_cols:
            out("PASS: trajectory.csv NoOp source column(s): {}".format(", ".join(noop_cols)))
            counts.pass_ += 1
        else:
            out("FAIL: trajectory.csv - NoOp rows carry no source marker column "
                "(LLM-initiated / idle-heartbeat / timeout-injected)")
            counts.fail += 1

    # --- agent_interactions.csv unchanged -----------------------------------
    if (results_dir / "agent_interactions.csv").is_file():
        out("PASS: agent_interactions.csv (unchanged)")
        counts.pass_ += 1
    else:
        out("MISSING(target): agent_interactions.csv")
        counts.missing += 1

    # --- router_interactions.csv: 9 columns + all four ActionTypes (#3) ------
    router_path = results_dir / "router_interactions.csv"
    router_header = read_csv_header(router_path) if router_path.is_file() else None
    if router_header is None:
        if router_path.exists():
            out("FAIL: router_interactions.csv - unreadable or empty CSV")
            counts.fail += 1
        else:
            out("MISSING(target): router_interactions.csv - cannot check target shape")
            counts.missing += 1
    else:
        type_col = ("EventType" if "EventType" in router_header
                    else ("ActionType" if "ActionType" in router_header else None))
        col_ok = len(router_header) == ROUTER_TARGET_COLUMNS
        present = csv_column_values(router_path, type_col) if type_col else set()
        missing_types = [t for t in ACTION_TYPES if t not in present]
        if col_ok and not missing_types:
            out(f"PASS: router_interactions.csv {len(router_header)} columns, "
                f"ActionTypes {', '.join(sorted(present))} all present")
            counts.pass_ += 1
        else:
            reasons = []
            if not col_ok:
                reasons.append(f"{len(router_header)} columns (target {ROUTER_TARGET_COLUMNS})")
            if missing_types:
                reasons.append("missing ActionType(s): {} (have: {})".format(", ".join(missing_types), ", ".join(sorted(present)) or "none"))
            out("FAIL: router_interactions.csv - {}".format("; ".join(reasons)))
            counts.fail += 1

    # --- token_usage.csv anomaly/failure marker rows (#12) --------------------
    tu_path = results_dir / "token_usage.csv"
    tu_header = read_csv_header(tu_path) if tu_path.is_file() else None
    if tu_header is None:
        if tu_path.exists():
            out("FAIL: token_usage.csv - unreadable or empty CSV")
            counts.fail += 1
        else:
            out("MISSING(target): token_usage.csv - cannot check anomaly markers")
            counts.missing += 1
    else:
        err_cols = [c for c in tu_header if "error" in c.lower()]
        if not err_cols:
            out("FAIL: token_usage.csv - no anomaly/failure marker column "
                "(ErrorType/ErrorCode); the 4%% token gap stays invisible")
            counts.fail += 1
        elif not csv_column_values(tu_path, err_cols[0]):
            out(f"FAIL: token_usage.csv - marker column {err_cols[0]} exists but has no marked rows")
            counts.fail += 1
        else:
            out(f"PASS: token_usage.csv anomaly marker column {err_cols[0]} with marked rows")
            counts.pass_ += 1

    # --- subtasks.csv terminal status rows (#10) -------------------------------
    sub_path = results_dir / "subtasks.csv"
    sub_header = read_csv_header(sub_path) if sub_path.is_file() else None
    if sub_header is None:
        if sub_path.exists():
            out("FAIL: subtasks.csv - unreadable or empty CSV")
            counts.fail += 1
        else:
            out("MISSING(target): subtasks.csv - cannot check terminal statuses")
            counts.missing += 1
    elif "Status" not in sub_header:
        out("FAIL: subtasks.csv - no Status column")
        counts.fail += 1
    else:
        statuses = csv_column_values(sub_path, "Status")
        terminal = sorted(SUBTASK_TERMINAL_STATUSES & statuses)
        if terminal:
            out("PASS: subtasks.csv terminal statuses observed: {}".format(", ".join(terminal)))
            counts.pass_ += 1
        else:
            out("FAIL: subtasks.csv - only non-terminal status(es): %s"
                % (", ".join(sorted(statuses)) or "none"))
            counts.fail += 1

    # --- summary.csv / events.ndjson / run_metrics.json unchanged ---------------
    for name in ("summary.csv", "events.ndjson", "run_metrics.json"):
        if (results_dir / name).is_file():
            out(f"PASS: {name} (unchanged)")
            counts.pass_ += 1
        else:
            out(f"MISSING(target): {name}")
            counts.missing += 1

    # --- coordinator/<task_id>.ndjson naming (#7) --------------------------------
    coord_dir = results_dir / "coordinator"
    coord_ndjsons = sorted(coord_dir.glob("*.ndjson")) if coord_dir.is_dir() else []
    # events_*.ndjson are the EventStore stream and supervision_dsp_* relocate
    # to supervision/ (#9) — both checked separately; only task-trace ndjson
    # names count for the #7 naming check.
    non_default = [p.name for p in coord_ndjsons
                   if p.name not in ("unnamed_task.ndjson", "unknown.ndjson")
                   and not p.name.startswith("events_")
                   and not p.name.startswith("supervision_dsp_")]
    if non_default:
        out("PASS: coordinator/<task_id>.ndjson named files: {}".format(", ".join(non_default[:3])))
        counts.pass_ += 1
    elif coord_ndjsons:
        out("FAIL: coordinator/ only has default-named ndjson(s): {}".format(", ".join(p.name for p in coord_ndjsons)))
        counts.fail += 1
    else:
        out("MISSING(target): coordinator/ ndjson traces")
        counts.missing += 1

    # --- coordinator/events_<task>.ndjson structured observation (#2) ------------
    events_files = sorted(coord_dir.glob("events_*.ndjson")) if coord_dir.is_dir() else []
    if events_files:
        if ndjson_top_level_key(events_files, "observation"):
            out("PASS: coordinator/events_*.ndjson carry structured observation field")
            counts.pass_ += 1
        else:
            out("FAIL: coordinator/events_*.ndjson - no structured observation field in lines")
            counts.fail += 1
    else:
        out("MISSING(target): coordinator/events_<task>.ndjson event stream")
        counts.missing += 1

    # --- coordinator/semantic_map.jsonl (semantic mode) --------------------------
    semantic_mode = isinstance(meta.get("state_mode"), str) and meta["state_mode"] == "semantic"
    if (coord_dir / "semantic_map.jsonl").is_file() or (results_dir / "semantic_map.jsonl").is_file():
        out("PASS: semantic_map.jsonl (noteworthy event stream)")
        counts.pass_ += 1
    elif semantic_mode:
        out("MISSING(target): semantic_map.jsonl - state_mode=semantic but no file "
            "(top-level or coordinator/)")
        counts.missing += 1
    else:
        out("PASS: semantic_map.jsonl (N/A - state_mode={})".format(meta.get("state_mode", "unknown")))
        counts.pass_ += 1

    # --- coordinator/snapshot_<task_id>.json (#7) ---------------------------------
    if coord_dir.is_dir() and list(coord_dir.glob("snapshot_*.json")):
        out("PASS: coordinator/snapshot_<task_id>.json present")
        counts.pass_ += 1
    else:
        out("MISSING(target): coordinator/snapshot_<task_id>.json (INPUT_REQUIRED guard chain fix)")
        counts.missing += 1

    # --- coordinator/context/ audit (#6) -------------------------------------------
    context_dir = coord_dir / "context"
    if (context_dir / "prune_events.ndjson").is_file():
        out("PASS: coordinator/context/prune_events.ndjson")
        counts.pass_ += 1
    else:
        out("MISSING(target): coordinator/context/prune_events.ndjson (phase-1 prune triggers)")
        counts.missing += 1
    if (context_dir / "discards").is_dir():
        out("PASS: coordinator/context/discards/ (pruned message originals)")
        counts.pass_ += 1
    else:
        out("MISSING(target): coordinator/context/discards/ directory")
        counts.missing += 1

    # --- coordinator/long_term/long_term.sqlite3 (conditional) ---------------------
    lt_path = coord_dir / "long_term" / "long_term.sqlite3"
    if lt_path.is_file():
        out("PASS: coordinator/long_term/long_term.sqlite3")
        counts.pass_ += 1
    else:
        out("MISSING(target): coordinator/long_term/long_term.sqlite3 "
            "(conditional: required only when --long-term-mode != off; "
            "run metadata offers no confirmation)")
        counts.missing += 1

    # --- coordinator/diagnosis/diagnosis.sqlite3 upsert+revision (#14) --------------
    diag_sqlite = coord_dir / "diagnosis" / "diagnosis.sqlite3"
    if diag_sqlite.is_file():
        revision_cols = []
        try:
            con = sqlite3.connect(f"file:{diag_sqlite}?mode=ro", uri=True)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                for t in tables:
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
                    if any("revision" in c.lower() for c in cols):
                        revision_cols.append(t)
            finally:
                con.close()
        except sqlite3.Error:
            pass
        if revision_cols:
            out("PASS: diagnosis.sqlite3 revision column(s) in table(s): {}".format(", ".join(revision_cols)))
            counts.pass_ += 1
        else:
            out("FAIL: diagnosis.sqlite3 - no revision column (INSERT OR IGNORE swallows updates)")
            counts.fail += 1
    else:
        out("MISSING(target): coordinator/diagnosis/diagnosis.sqlite3")
        counts.missing += 1

    # --- coordinator/diagnosis/transcripts.ndjson (#5) -------------------------------
    if (coord_dir / "diagnosis" / "transcripts.ndjson").is_file():
        out("PASS: coordinator/diagnosis/transcripts.ndjson "
            "(per-round evidence views + rolling states)")
        counts.pass_ += 1
    else:
        out("MISSING(target): coordinator/diagnosis/transcripts.ndjson")
        counts.missing += 1

    # --- supervision/ relocation (#9) --------------------------------------------------
    sup_dir = results_dir / "supervision"
    if sup_dir.is_dir():
        sup_files = sorted(sup_dir.glob("supervision_dsp_*.ndjson"))
        if sup_files:
            out(f"PASS: supervision/ relocated with {len(sup_files)} supervision_dsp_*.ndjson file(s)")
            counts.pass_ += 1
        else:
            out("FAIL: supervision/ exists but is an empty shell (no supervision_dsp_*.ndjson)")
            counts.fail += 1
    else:
        out("MISSING(target): supervision/ directory "
            "(supervision_dsp_*.ndjson relocated from coordinator/)")
        counts.missing += 1

    # --- workers/<Agent>/<task>.ndjson tool_start events (#4) --------------------------
    workers_dir = results_dir / "workers"
    if not workers_dir.is_dir():
        out("MISSING(target): workers/ directory")
        counts.missing += 1
    else:
        per_agent = ndjson_has_event(workers_dir, "tool_start")
        if not per_agent:
            out("MISSING(target): workers/<AgentName>/ ndjson traces")
            counts.missing += 1
        else:
            missing = [a for a, ok in per_agent.items() if not ok]
            if missing:
                out("FAIL: workers ndjson missing tool_start in: {}".format(", ".join(missing)))
                counts.fail += 1
            else:
                out("PASS: workers ndjson tool_start present for: {}".format(", ".join(sorted(per_agent))))
                counts.pass_ += 1


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a SAR run directory against the documented output contract.")
    parser.add_argument("--results-dir", required=True, type=Path,
                        help="Path to a run output directory, "
                             "e.g. sar_orch/results/20260721_201943_s3_s42_a4")
    parser.add_argument("--mode", choices=["l1", "l2"], default="l1",
                        help="l1 = current contract (AGENTS.md Output Files), "
                             "l2 = target shape (README section 6)")
    args = parser.parse_args()

    if not args.results_dir.is_dir():
        print(f"ERROR: --results-dir {args.results_dir} is not a directory")
        return 2

    counts = Counts()
    if args.mode == "l1":
        print(f"== L1 current contract (AGENTS.md Output Files) == {args.results_dir}")
        validate_l1(args.results_dir, print, counts)
    else:
        print(f"== L2 target shape (docs/plans/trajectory-audit/README.md section 6) == {args.results_dir}")
        validate_l2(args.results_dir, print, counts)

    print(f"summary: PASS={counts.pass_} FAIL={counts.fail} MISSING={counts.missing} "
          f"(exit {1 if counts.total() else 0})")
    return 1 if counts.total() else 0


if __name__ == "__main__":
    sys.exit(main())

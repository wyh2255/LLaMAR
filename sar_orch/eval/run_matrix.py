"""P6 orchestration lifecycle owner（设计 §0.6-7、§3.2、§9 P6.5-6）。

只负责：创建 private work root（0700）→ 调用 preparer → 调用 verifier →
先在工作 root 外 atomic 写 redacted per-attempt summary，再 finally cleanup。
**绝不**选择输入、改写 manifest 或解释结果。默认删除 manifest/staged workdir；
`--keep-workdir` 才保留。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sar_orch.eval import contracts as c
from sar_orch.eval import matrix as mx
from sar_orch.eval import prepare_matrix as pm
from sar_orch.eval import verify_matrix as vm


def _source_status_fingerprint(
    results_root: Path, spec: mx.P6SourceSpec
) -> dict[str, Any]:
    """source+git 状态指纹：summary 需记录 pre/post equality（§0.6-7）。"""
    git_status = ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(results_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            git_status = proc.stdout
    except (OSError, subprocess.SubprocessError) as _unused:
        # 非 git 仓库 / 命令不可用时留空（可接受）。
        git_status = ""
    return {"git_status": git_status}


def _write_summary_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(c.canonical_json(report), encoding="utf-8")
    os.replace(tmp, path)


def _build_redacted_summary(
    *,
    matrix_manifest_sha: str,
    source_spec_digest: str,
    verify_report: dict[str, Any],
    source_status_pre: dict[str, Any],
    source_status_post: dict[str, Any],
) -> dict[str, Any]:
    """redacted per-attempt summary：只保留 digest/status/count/equality 字段，
    不保留 raw payload、raw manifest 或 staged path。"""
    cells = []
    for cell in verify_report.get("cells", []):
        attempts = []
        for attempt in cell.get("attempts", []):
            attempts.append(
                {
                    "attempt_id": attempt.get("attempt_id"),
                    "terminal_status": attempt.get("terminal_status"),
                    "judge_status": attempt.get("judge_status"),
                    "input_manifest_digest": attempt.get("input_manifest_digest"),
                    "final_ledger_digest": attempt.get("final_ledger_digest"),
                    "report_digest": attempt.get("report_digest"),
                    "evidence_digest": attempt.get("evidence_digest"),
                    "deterministic_grader_digest": attempt.get(
                        "deterministic_grader_digest"
                    ),
                    "episode": attempt.get("episode"),
                }
            )
        cells.append(
            {
                "cell_id": cell.get("cell_id"),
                "subject_tuple": cell.get("subject_tuple"),
                "raw_input_digest": cell.get("raw_input_digest"),
                "raw_trace_digest": cell.get("raw_trace_digest"),
                "raw_provenance_digest": cell.get("raw_provenance_digest"),
                "redaction_policy_digest": cell.get("redaction_policy_digest"),
                "staged_semantic_digest": cell.get("staged_semantic_digest"),
                "supervision_state_digest": cell.get("supervision_state_digest"),
                "projection_digests": cell.get("projection_digests"),
                "source_framework_error_counts": cell.get(
                    "source_framework_error_counts"
                ),
                "grader_skip_taxonomy": cell.get("grader_skip_taxonomy"),
                "semantic_fingerprint": cell.get("semantic_fingerprint"),
                "harness_error_counts": cell.get("harness_error_counts"),
                "stripped_metadata_keys": cell.get("stripped_metadata_keys", []),
                "stripped_csv_columns": cell.get("stripped_csv_columns", {}),
                "attempts": attempts,
                "resume": cell.get("resume"),
            }
        )
    return {
        "schema_version": 1,
        "matrix_manifest_sha256": matrix_manifest_sha,
        "source_spec_digest": source_spec_digest,
        "source_status_pre_post_equal": source_status_pre == source_status_post,
        "source_status_pre": source_status_pre,
        "source_status_post": source_status_post,
        "total_harness_error_counts": verify_report.get("total_harness_error_counts"),
        "cells": cells,
    }


def run(
    source_spec: mx.P6SourceSpec,
    results_root: str | Path,
    *,
    summary_out: str | Path,
    resume_cells: set[str] | None = None,
    runner_factory_factory=None,
    keep_workdir: bool = False,
    work_root: str | Path | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """P6 offline orchestration。返回 (exit_code, redacted_summary|None)。"""
    results_root = Path(results_root).resolve()
    if not results_root.exists() or not results_root.is_dir():
        return 1, None
    created_work = False
    if work_root is None:
        work_root = Path(tempfile.mkdtemp(prefix="p6_matrix_"))
        created_work = True
    work_root = Path(work_root).resolve()
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(work_root, 0o700)

    source_status_pre = _source_status_fingerprint(results_root, source_spec)
    manifest_path = work_root / "MATRIX_MANIFEST.json"
    summary: dict[str, Any] | None = None
    try:
        manifest_path = pm.prepare(
            source_spec, results_root=results_root, work_root=work_root
        )
        matrix_manifest_sha = c.sha256_hex(manifest_path.read_bytes())
        report = vm.verify(
            manifest_path,
            recorded_manifest_sha=matrix_manifest_sha,
            expected_source_spec_digest=source_spec.digest(),
            resume_cells=resume_cells,
            runner_factory_factory=runner_factory_factory,
        )
        source_status_post = _source_status_fingerprint(results_root, source_spec)
        summary = _build_redacted_summary(
            matrix_manifest_sha=matrix_manifest_sha,
            source_spec_digest=source_spec.digest(),
            verify_report=report,
            source_status_pre=source_status_pre,
            source_status_post=source_status_post,
        )
        _write_summary_atomic(Path(summary_out), summary)
        total = report["total_harness_error_counts"]
        if any(total.values()):
            return 1, summary
        return 0, summary
    except Exception as exc:  # noqa: BLE001
        print(f"run_matrix: FAILED: {exc}", file=sys.stderr)
        return 1, None
    finally:
        if not keep_workdir:
            shutil.rmtree(work_root, ignore_errors=True)
        elif created_work:
            print(f"run_matrix: kept workdir {work_root}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="run_matrix")
    parser.add_argument("--source-spec", required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--summary-out", required=True)
    parser.add_argument("--resume-cells", default="s3_a4")
    parser.add_argument("--no-llm-judge", action="store_true", default=True)
    parser.add_argument("--keep-workdir", action="store_true")
    parser.add_argument("--work-root")
    args = parser.parse_args(argv)

    spec = mx.load_source_spec(args.source_spec)
    code, _summary = run(
        spec,
        results_root=args.results_root,
        summary_out=args.summary_out,
        resume_cells=set(args.resume_cells.split(",")) if args.resume_cells else set(),
        keep_workdir=args.keep_workdir,
        work_root=args.work_root,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

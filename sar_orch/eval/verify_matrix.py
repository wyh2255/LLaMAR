"""P6 manifest-only verifier / 离线 workflow runner（设计 §0.6-3/7、§9 P6.3-5）。

只消费 `$MATRIX_MANIFEST` + 物理 staged semantic inputs；**绝不** discovery/preparation、
复制 source、生成 manifest、补格或替换 cell。校验：

- schema、caller-recorded manifest SHA、source-spec binding、七格 completeness/uniqueness；
- path containment、file type/link count（regular file、非 symlink/hardlink）；
- raw-input/raw-trace/raw-provenance/policy/staged/projection digest 与 staged 防御扫描；
- frozen subject tuple 与 workflow frozen subject.source_digest == staged_semantic_digest。

离线 workflow runner：`--no-llm-judge` 每 cell 两个独立 attempt，必须 SUCCEEDED/
not_requested、零 runner/model construction、semantic fingerprint 稳定；s3_a4 额外
一次 resume。FakeRunner requested-path probe 单独函数，不与 no-LLM 混用。

输出两族计数：`source_framework_error_counts`（诊断）与 `harness_error_counts`（门）。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sar_orch.eval import artifacts as a
from sar_orch.eval import contracts as c
from sar_orch.eval import matrix as mx
from sar_orch.eval import p6_staging_policy as policy
from sar_orch.eval import workflow as w


class VerifyError(RuntimeError):
    """verifier 越权或校验失败（映射为对应 harness error bucket）。"""


# ─────────────────────────────────────────────────────────────────────────────
# manifest 静态校验（不运行 workflow）
# ─────────────────────────────────────────────────────────────────────────────


def _load_manifest(manifest_path: Path) -> mx.MatrixManifest:
    try:
        data = manifest_path.read_bytes()
    except OSError as exc:
        raise VerifyError(f"manifest unreadable: {exc}") from exc
    try:
        manifest = mx.MatrixManifest.model_validate(json.loads(data))
    except Exception as exc:
        raise VerifyError(f"manifest schema invalid: {exc}") from exc
    if (
        manifest.schema_version != 1
        or manifest.manifest_version != "p6-matrix-manifest-v1"
    ):
        raise VerifyError(f"unsupported manifest version: {manifest.manifest_version}")
    return manifest


def verify_manifest_static(
    manifest_path: Path,
    *,
    recorded_manifest_sha: str | None = None,
    expected_source_spec_digest: str | None = None,
) -> tuple[mx.MatrixManifest, dict[str, int]]:
    """manifest-only 静态校验；返回 (manifest, harness_error_counts)。"""
    harness = mx.empty_harness_error_counts()
    manifest = _load_manifest(manifest_path)
    if recorded_manifest_sha is not None:
        actual = c.sha256_hex(manifest_path.read_bytes())
        if actual != recorded_manifest_sha:
            harness["manifest_validation"] += 1

    # source-spec binding：manifest 内嵌 source_spec_digest；外部期望时校验。
    if (
        expected_source_spec_digest is not None
        and manifest.source_spec_digest != expected_source_spec_digest
    ):
        harness["source_provenance_mismatch"] += 1

    # 七格 completeness/uniqueness（缺格、重复 cell_id/source_rel/tuple 均失败）。
    cell_ids = [cell.cell_id for cell in manifest.cells]
    if len(cell_ids) != len(set(cell_ids)):
        harness["manifest_validation"] += 1
    # 固定 cohort 强制完整七格集合（缺格/增格 → harness error；不补格、不替换）。
    if manifest.cohort == mx.P6_COHORT and set(cell_ids) != set(mx.P6_CELL_IDS):
        harness["manifest_validation"] += 1
    source_rels = [cell.source_rel for cell in manifest.cells]
    if len(source_rels) != len(set(source_rels)):
        harness["manifest_validation"] += 1
    # cell tuple uniqueness 必须包含 repetition（同 tuple 不同 repetition 允许；
    # 缺 repetition 视为唯一性失配候选，由 fail-closed 拒绝）。
    tuples = [
        (cell.scene, cell.agents, cell.seed, cell.subject_tuple.get("repetition"))
        for cell in manifest.cells
    ]
    if len(tuples) != len(set(tuples)):
        harness["manifest_validation"] += 1

    # 每 cell redaction_policy_digest 必须等于当前 policy digest（policy drift）。
    for cell in manifest.cells:
        if cell.redaction_policy_digest != policy.policy_digest():
            harness["source_provenance_mismatch"] += 1
            continue
        if cell.source_spec_digest != manifest.source_spec_digest:
            harness["source_provenance_mismatch"] += 1
        # raw-provenance digest = (spec, raw-input, raw-trace) tuple hash
        expected_provenance = mx.raw_provenance_digest(
            cell.source_spec_digest, cell.raw_input_digest, cell.raw_trace_digest
        )
        if expected_provenance != cell.raw_provenance_digest:
            harness["source_provenance_mismatch"] += 1
        # raw-input digest 可由 manifest 内嵌的文件级 hash 重算（无需 source）。
        expected_raw_input = mx.raw_input_digest_from_files(
            cell.raw_input_files, cell.supervision_state
        )
        if expected_raw_input != cell.raw_input_digest:
            harness["source_provenance_mismatch"] += 1
        expected_raw_trace = mx.raw_trace_digest_from_files(cell.raw_trace_files)
        if expected_raw_trace != cell.raw_trace_digest:
            harness["source_provenance_mismatch"] += 1
    return manifest, harness


def _verify_cell_physical(
    manifest: mx.MatrixManifest, cell: mx.MatrixCellManifest
) -> list[str]:
    """单格物理校验：staged path containment、regular/symlink/hardlink、staged
    digest、projection digest、supervision sentinel。返回 violation list。"""
    violations: list[str] = []
    work_root = Path(manifest.work_root).resolve()
    staged_dir = (work_root / cell.staged_path).resolve()
    if not staged_dir.is_relative_to(work_root):
        violations.append(f"{cell.cell_id}: staged path escapes work root")
        return violations
    if not staged_dir.exists() or not staged_dir.is_dir():
        violations.append(f"{cell.cell_id}: staged dir missing: {cell.staged_path}")
        return violations
    for rel in mx.STAGED_FILE_NAMES:
        target = staged_dir / rel
        if not target.exists():
            continue
        if not target.is_file():
            violations.append(f"{cell.cell_id}: staged {rel} not a regular file")
            continue
        if target.is_symlink():
            violations.append(f"{cell.cell_id}: staged {rel} is a symlink")
        if target.stat().st_nlink > 1:
            violations.append(f"{cell.cell_id}: staged {rel} is a hardlink")
    # staged semantic digest 必须可由物理 staged dir 重算（digest drift）。
    actual_staged = mx.staged_semantic_digest(staged_dir)
    if actual_staged != cell.staged_semantic_digest:
        violations.append(
            f"{cell.cell_id}: staged semantic digest drift "
            f"{actual_staged[:16]}... != {cell.staged_semantic_digest[:16]}..."
        )
    # staged 防御扫描（secret / CoT / LLM 字段 / raw NDJSON / 未知 metadata key）。
    for violation in policy.defense_scan(staged_dir):
        violations.append(f"{cell.cell_id}: staged defense: {violation}")
    # projection digest 可由 projection payload 重算（防止 manifest 篡改）。
    for proj in cell.projections:
        if not proj.projection_digest or proj.digest() != proj.projection_digest:
            violations.append(f"{cell.cell_id}: projection digest mismatch")
            break
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 离线 workflow runner（--no-llm-judge）
# ─────────────────────────────────────────────────────────────────────────────


def _attempt_args(
    staged_dir: Path,
    work_root: Path,
    eval_run_id,
    attempt_id,
    *,
    no_llm_judge: bool,
    judge_sample_steps: int = 20,
) -> SimpleNamespace:
    return SimpleNamespace(
        results_dir=str(staged_dir),
        no_llm_judge=no_llm_judge,
        judge_sample_steps=judge_sample_steps,
        eval_run_id=eval_run_id,
        attempt_id=attempt_id,
        attempt_root=str(
            work_root / "attempts" / str(eval_run_id) / "attempts" / str(attempt_id)
        ),
    )


def run_offline_no_llm_attempt(
    staged_dir: Path,
    work_root: Path,
    eval_run_id,
    attempt_id,
    *,
    runner_factory_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """单个 no-LLM attempt：SUCCEEDED/not_requested、零 runner construction。

    返回 per-attempt 可核验摘要（digest/status/fingerprint 等）。
    """
    args = _attempt_args(
        staged_dir, work_root, eval_run_id, attempt_id, no_llm_judge=True
    )
    stats: dict[str, Any] = {"constructed": 0}

    def _counting_factory(runtime):
        def _factory():
            stats["constructed"] += 1
            raise AssertionError("no-LLM must never construct a runner")

        return _factory

    code = w.run_from_results_dir(
        args, runner_factory_factory=runner_factory_factory or _counting_factory
    )
    if code != 0:
        raise VerifyError(
            f"offline no-LLM workflow exited {code} for attempt {eval_run_id}/{attempt_id}"
        )
    if stats["constructed"] != 0:
        raise VerifyError(
            f"unexpected runner construction: {stats['constructed']} on no-LLM path"
        )
    store = a.ArtifactStore(args.attempt_root)
    manifest = store.read_input_manifest()
    ledger = store.read_final_ledger()
    if ledger is None or ledger.terminal_status is not c.WorkflowStatus.SUCCEEDED:
        raise VerifyError(f"attempt {eval_run_id}/{attempt_id} not SUCCEEDED")
    if ledger.judge_execution_status is not c.JudgeExecutionStatus.NOT_REQUESTED:
        raise VerifyError(
            f"attempt {eval_run_id}/{attempt_id} judge status not not_requested"
        )
    report = json.loads(store.read_bytes(w.REPORT_REL).decode("utf-8"))
    return {
        "attempt_id": str(attempt_id),
        "terminal_status": ledger.terminal_status.value,
        "judge_status": ledger.judge_execution_status.value,
        "input_manifest_digest": manifest.digest,
        "final_ledger_digest": ledger.digest(),
        "report_digest": ledger.report_digest,
        "evidence_digest": _file_digest(store, w.EVIDENCE_BUNDLE_REL),
        "deterministic_grader_digest": _file_digest(store, w.GRADER_RESULTS_REL),
        "subject": {
            "scene": manifest.manifest.subject.scene,
            "agents": manifest.manifest.subject.agents,
            "seed": manifest.manifest.subject.seed,
            "source_digest": manifest.manifest.subject.source_digest,
        },
        "episode": {
            "coverage": report.get("episode", {}).get("coverage"),
            "transport_rate": report.get("episode", {}).get("transport_rate"),
        },
        "runner_constructed": stats["constructed"],
    }


def _file_digest(store: a.ArtifactStore, rel: str) -> str:
    if not store.exists(rel):
        return ""
    return c.sha256_hex(store.read_bytes(rel))


def semantic_fingerprint(
    attempt: dict[str, Any],
    cell: mx.MatrixCellManifest,
    *,
    grader_skip: dict[str, int],
) -> dict[str, Any]:
    """排除 attempt UUID/时间戳/ledger 运行身份字段后的稳定指纹（§9 P6.4）。"""
    return {
        "staged_semantic_digest": cell.staged_semantic_digest,
        "supervision_state_digest": cell.supervision_state_digest,
        "deterministic_grader_digest": attempt["deterministic_grader_digest"],
        "evidence_digest": attempt["evidence_digest"],
        "judge_status": attempt["judge_status"],
        "source_framework_error_counts": cell.source_framework_error_counts,
        "grader_skip_taxonomy": grader_skip,
    }


def verify_cell_offline(
    manifest: mx.MatrixManifest,
    cell: mx.MatrixCellManifest,
    *,
    resume_cell: bool = False,
    runner_factory_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """每 cell 两个独立 no-LLM attempt + 可选一次 resume；fingerprint 稳定。"""
    work_root = Path(manifest.work_root).resolve()
    staged_dir = (work_root / cell.staged_path).resolve()
    harness = mx.empty_harness_error_counts()

    run1 = run_offline_no_llm_attempt(
        staged_dir,
        work_root,
        uuid4(),
        uuid4(),
        runner_factory_factory=runner_factory_factory,
    )
    run2 = run_offline_no_llm_attempt(
        staged_dir,
        work_root,
        uuid4(),
        uuid4(),
        runner_factory_factory=runner_factory_factory,
    )
    grader_skip = _load_grader_skip(staged_dir)
    fp1 = semantic_fingerprint(run1, cell, grader_skip=grader_skip)
    fp2 = semantic_fingerprint(run2, cell, grader_skip=grader_skip)
    if fp1 != fp2:
        harness["workflow_execution"] += 1
    for run in (run1, run2):
        if run["runner_constructed"] != 0:
            harness["unexpected_model_construction"] += 1

    # source taxonomy 与 grader-skip taxonomy 必须逐 cell 等于 spec 预期值
    # （§0.6-5 / §9 P6.3）。非零计数本身是诊断，只有 mismatch 才 fail-closed。
    if cell.source_framework_error_counts != cell.expected_source_error_taxonomy:
        harness["source_provenance_mismatch"] += 1
    if grader_skip != cell.expected_grader_skip_taxonomy:
        harness["source_provenance_mismatch"] += 1

    # subject tuple 必须逐 cell 等于 spec（不接受 workflow default）。
    subject = run1["subject"]
    if (
        subject["scene"] != cell.scene
        or subject["agents"] != cell.agents
        or subject["seed"] != cell.seed
    ):
        harness["artifact_verification"] += 1
    if subject["source_digest"] != cell.staged_semantic_digest:
        harness["artifact_verification"] += 1

    resume: dict[str, Any] | None = None
    if resume_cell:
        resume = _verify_cell_resume(
            staged_dir, work_root, cell, grader_skip=grader_skip
        )

    return {
        "cell_id": cell.cell_id,
        "subject_tuple": {
            "scene": subject["scene"],
            "agents": subject["agents"],
            "seed": subject["seed"],
        },
        "raw_input_digest": cell.raw_input_digest,
        "raw_trace_digest": cell.raw_trace_digest,
        "raw_provenance_digest": cell.raw_provenance_digest,
        "redaction_policy_digest": cell.redaction_policy_digest,
        "staged_semantic_digest": cell.staged_semantic_digest,
        "supervision_state_digest": cell.supervision_state_digest,
        "projection_digests": [proj.projection_digest for proj in cell.projections],
        "source_framework_error_counts": cell.source_framework_error_counts,
        "expected_source_error_taxonomy": cell.expected_source_error_taxonomy,
        "expected_grader_skip_taxonomy": cell.expected_grader_skip_taxonomy,
        "grader_skip_taxonomy": grader_skip,
        "semantic_fingerprint": fp1,
        "harness_error_counts": harness,
        "stripped_metadata_keys": cell.stripped_metadata_keys,
        "stripped_csv_columns": cell.stripped_csv_columns,
        "attempts": [run1, run2],
        "resume": resume,
    }


def _load_grader_skip(staged_dir: Path) -> dict[str, int]:
    from sar_orch.eval.dataset import load_episode

    episode = load_episode(staged_dir)
    return mx.grader_skip_taxonomy_from_episode(episode)


def _verify_cell_resume(
    staged_dir: Path,
    work_root: Path,
    cell: mx.MatrixCellManifest,
    *,
    grader_skip: dict[str, int],
) -> dict[str, Any]:
    """s3_a4：deterministic spine 非终态中断后 resume 一次。仅缺失节点重跑。

    先完整跑一遍（写 evidence/grader/report/ledger），记录已验证 artifact digest；
    删除 final ledger（模拟非终态 kill）后 resume 同 locator：已验证 artifact
    （evidence/grader_results/report/input_manifest）**不得被重写**，只补缺失的
    finalize 节点。
    """
    eval_run_id, attempt_id = uuid4(), uuid4()
    run_offline_no_llm_attempt(staged_dir, work_root, eval_run_id, attempt_id)
    store = a.ArtifactStore(
        work_root / "attempts" / str(eval_run_id) / "attempts" / str(attempt_id)
    )
    evidence_before = _file_digest(store, w.EVIDENCE_BUNDLE_REL)
    grader_before = _file_digest(store, w.GRADER_RESULTS_REL)
    report_before = _file_digest(store, w.REPORT_REL)
    manifest_before = store.read_input_manifest().digest
    if not store.exists(a.ArtifactStore.LEDGER_REL):
        return {"status": "FAILED", "reason": "initial run missing final ledger"}
    os.remove(store.path(a.ArtifactStore.LEDGER_REL))
    # resume：同一 locator，非终态 → 仅路由缺失节点。
    args = _attempt_args(
        staged_dir, work_root, eval_run_id, attempt_id, no_llm_judge=True
    )
    code = w.run_from_results_dir(args)
    if code != 0:
        return {"status": "FAILED", "exit": code}
    store2 = a.ArtifactStore(args.attempt_root)
    ledger2 = store2.read_final_ledger()
    if ledger2 is None or ledger2.terminal_status is not c.WorkflowStatus.SUCCEEDED:
        return {"status": "FAILED", "terminal": None}
    manifest2 = store2.read_input_manifest()
    artifacts_unchanged = (
        _file_digest(store2, w.EVIDENCE_BUNDLE_REL) == evidence_before
        and _file_digest(store2, w.GRADER_RESULTS_REL) == grader_before
        and _file_digest(store2, w.REPORT_REL) == report_before
        and manifest2.digest == manifest_before
    )
    fp = semantic_fingerprint(
        {
            "attempt_id": str(attempt_id),
            "deterministic_grader_digest": grader_before,
            "evidence_digest": evidence_before,
            "judge_status": ledger2.judge_execution_status.value,
        },
        cell,
        grader_skip=grader_skip,
    )
    return {
        "status": "SUCCEEDED",
        "resume_locator": f"{eval_run_id}/{attempt_id}",
        "source_digest": manifest2.manifest.subject.source_digest,
        "artifacts_unchanged_across_resume": artifacts_unchanged,
        "semantic_fingerprint": fp,
    }


def verify(
    manifest_path: Path,
    *,
    recorded_manifest_sha: str | None = None,
    expected_source_spec_digest: str | None = None,
    resume_cells: set[str] | None = None,
    runner_factory_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """完整 verifier：静态校验 + 离线 workflow；返回汇总报告。"""
    manifest, harness = verify_manifest_static(
        manifest_path,
        recorded_manifest_sha=recorded_manifest_sha,
        expected_source_spec_digest=expected_source_spec_digest,
    )
    cells = []
    total_harness = mx.empty_harness_error_counts()
    for cell in manifest.cells:
        violations = _verify_cell_physical(manifest, cell)
        if violations:
            harness["semantic_staging"] += len(violations)
            total_harness["semantic_staging"] += len(violations)
        cell_result = verify_cell_offline(
            manifest,
            cell,
            resume_cell=cell.cell_id in (resume_cells or set()),
            runner_factory_factory=runner_factory_factory,
        )
        for bucket, count in cell_result["harness_error_counts"].items():
            total_harness[bucket] += count
        cells.append(cell_result)
    for bucket, count in harness.items():
        total_harness[bucket] += count
    return {
        "matrix_manifest_sha256": c.sha256_hex(manifest_path.read_bytes()),
        "source_spec_digest": manifest.source_spec_digest,
        "manifest_static_harness": harness,
        "total_harness_error_counts": total_harness,
        "cells": cells,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="verify_matrix")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha")
    parser.add_argument("--source-spec-digest")
    parser.add_argument("--resume-cells", default="s3_a4")
    parser.add_argument("--no-llm-judge", action="store_true", default=True)
    args = parser.parse_args(argv)

    try:
        report = verify(
            Path(args.manifest),
            recorded_manifest_sha=args.manifest_sha,
            expected_source_spec_digest=args.source_spec_digest,
            resume_cells=set(args.resume_cells.split(","))
            if args.resume_cells
            else set(),
        )
    except VerifyError as exc:
        print(f"verify_matrix: FAILED: {exc}", file=sys.stderr)
        return 1
    total = report["total_harness_error_counts"]
    if any(total.values()):
        print(
            f"verify_matrix: harness errors nonzero: {json.dumps(total)}",
            file=sys.stderr,
        )
        return 1
    print("verify_matrix: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

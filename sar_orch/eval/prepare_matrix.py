"""P6 deterministic preparer CLI（设计 §0.6-2/6、§3.2、§9 P6.2）。

只接受外部人工冻结的 `P6SourceSpec`，验证并物理 materialize 七个 cell 到
mode 0700 private work root，生成 canonical `MATRIX_MANIFEST.json`（不内嵌自身
hash）。**绝不**扫描/替换 source、运行 workflow 或解释结果。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from sar_orch.eval import contracts as c
from sar_orch.eval import matrix as mx
from sar_orch.eval import p6_staging_policy as policy


class PrepareError(RuntimeError):
    """preparer 越权/preflight failure（manifest_validation / source_provenance
    / semantic_staging）。"""


def _resolve_cell_source(
    spec: mx.P6SourceSpec, cell: mx.P6CellSpec, results_root: Path
) -> Path:
    source_rel = cell.source_rel
    source_dir = (results_root / source_rel).resolve()
    root = results_root.resolve()
    if not source_dir.is_relative_to(root):
        raise PrepareError(f"source escapes results-root: {source_rel!r}")
    if not source_dir.exists() or not source_dir.is_dir():
        raise PrepareError(f"missing source dir for cell {cell.cell_id}: {source_rel}")
    return source_dir


def _validate_supervision_present_empty(source_dir: Path, cell: mx.P6CellSpec) -> None:
    """source 的 supervision 必须是 present-empty（§3.2/§0.6-2）。missing/nonempty
    都是 preflight failure。"""
    sup_dir = source_dir / "supervision"
    if not sup_dir.exists() or not sup_dir.is_dir():
        raise PrepareError(f"{cell.cell_id}: supervision/ missing (not present-empty)")
    if any(sup_dir.iterdir()):
        raise PrepareError(f"{cell.cell_id}: supervision/ not empty")
    sentinel = source_dir / mx.SUPERVISION_STATE_FILENAME
    if sentinel.exists() and sentinel.read_bytes() != policy.supervision_state_bytes():
        raise PrepareError(
            f"{cell.cell_id}: source supervision_state.json non-canonical"
        )


def _validate_metadata_fingerprint(
    source_dir: Path, cell: mx.P6CellSpec
) -> dict[str, object]:
    """验证 source metadata.json 与 spec 的 expected_metadata_fingerprint 一致。"""
    meta_path = source_dir / "metadata.json"
    if not meta_path.exists():
        raise PrepareError(f"{cell.cell_id}: metadata.json missing")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PrepareError(
            f"{cell.cell_id}: metadata.json unparseable ({exc})"
        ) from exc
    fp = cell.expected_metadata_fingerprint
    mismatches = {}
    for key, expected in fp.items():
        actual = meta.get(key)
        if str(actual) != str(expected):
            mismatches[key] = {"expected": expected, "actual": actual}
    if (
        int(meta.get("scene", -1)) != cell.scene
        or int(meta.get("agent_count", -1)) != cell.agents
    ):
        mismatches["cell_tuple"] = {
            "expected": {"scene": cell.scene, "agents": cell.agents},
            "actual": {"scene": meta.get("scene"), "agents": meta.get("agent_count")},
        }
    if mismatches:
        raise PrepareError(
            f"{cell.cell_id}: metadata fingerprint mismatch: {json.dumps(mismatches)}"
        )
    return meta


def prepare_cell(
    spec: mx.P6SourceSpec,
    cell: mx.P6CellSpec,
    results_root: Path,
    work_root: Path,
    *,
    spec_digest: str,
) -> mx.MatrixCellManifest:
    """验证并 materialize 单个 cell；写回 cell manifest（不扫描/选择 source）。"""
    source_dir = _resolve_cell_source(spec, cell, results_root)
    _validate_supervision_present_empty(source_dir, cell)
    _validate_metadata_fingerprint(source_dir, cell)

    staged_dir = work_root / "staged" / cell.cell_id
    # materialize 前 staged_dir 必须不存在（create-without-overwrite 语义）。
    if staged_dir.exists():
        raise PrepareError(f"{cell.cell_id}: staged dir already exists: {staged_dir}")
    stripped_metadata_keys, stripped_csv_columns = mx.stage_cell_physical(
        spec, cell, source_dir, staged_dir
    )

    raw_input_digest_ = mx.raw_input_digest(source_dir)
    raw_trace_digest_, raw_trace_files = mx.raw_trace_digest(source_dir)
    raw_provenance = mx.raw_provenance_digest(
        spec_digest, raw_input_digest_, raw_trace_digest_
    )
    staged_semantic = mx.staged_semantic_digest(staged_dir)
    sup_digest = mx.supervision_state_digest()
    redaction_policy_digest = policy.policy_digest()

    projections = mx.build_trace_projections(source_dir, staged_semantic)
    source_framework_counts = mx.aggregate_source_framework_counts(projections)
    trajectory_path = staged_dir / "trajectory.csv"
    staged_files: dict[str, str] = {}
    for rel in mx.STAGED_FILE_NAMES:
        target = staged_dir / rel
        if target.exists():
            staged_files[rel] = c.sha256_hex(target.read_bytes())

    raw_input_files: dict[str, str] = {}
    for rel in mx.EXECUTABLE_SOURCE_FILES:
        raw_input_files[rel] = c.sha256_hex((source_dir / rel).read_bytes())

    sup_dir = source_dir / "supervision"
    supervision_state = {
        "present": sup_dir.exists(),
        "empty": sup_dir.exists() and not any(sup_dir.iterdir()),
    }

    return mx.MatrixCellManifest(
        cell_id=cell.cell_id,
        source_rel=cell.source_rel,
        subject_tuple={
            "scene": cell.scene,
            "agents": cell.agents,
            "seed": cell.seed,
            "repetition": cell.repetition,
        },
        metadata_fingerprint=dict(cell.expected_metadata_fingerprint),
        source_spec_digest=spec_digest,
        raw_input_digest=raw_input_digest_,
        raw_trace_digest=raw_trace_digest_,
        raw_provenance_digest=raw_provenance,
        redaction_policy_digest=redaction_policy_digest,
        staged_semantic_digest=staged_semantic,
        supervision_state_digest=sup_digest,
        raw_input_files=raw_input_files,
        raw_trace_files=raw_trace_files,
        staged_files=staged_files,
        supervision_state=supervision_state,
        trajectory_regular=trajectory_path.is_file()
        and not trajectory_path.is_symlink()
        and trajectory_path.stat().st_nlink == 1,
        staged_path=f"staged/{cell.cell_id}",
        projections=projections,
        source_framework_error_counts=source_framework_counts,
        expected_source_error_taxonomy=dict(cell.expected_source_error_taxonomy),
        expected_grader_skip_taxonomy=dict(cell.expected_grader_skip_taxonomy),
        stripped_metadata_keys=stripped_metadata_keys,
        stripped_csv_columns=stripped_csv_columns,
    )


def prepare(
    spec: mx.P6SourceSpec,
    results_root: str | Path,
    work_root: str | Path,
    *,
    manifest_out: str | Path | None = None,
) -> Path:
    """确定性 preparer：验证全部 cell 并 materialize，写 canonical manifest。

    返回 manifest 路径。manifest 不内嵌自身 hash。
    """
    results_root = Path(results_root).resolve()
    work_root = Path(work_root).resolve()
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(work_root, 0o700)

    if manifest_out is None:
        manifest_out = work_root / "MATRIX_MANIFEST.json"
    manifest_out = Path(manifest_out)
    if manifest_out.exists():
        raise PrepareError(
            f"manifest already exists (create-without-overwrite): {manifest_out}"
        )

    spec_digest = spec.digest()
    cells = [
        prepare_cell(spec, cell, results_root, work_root, spec_digest=spec_digest)
        for cell in spec.cells
    ]
    manifest = mx.MatrixManifest(
        cohort=spec.cohort,
        work_root=str(work_root),
        source_spec_digest=spec_digest,
        redaction_policy_digest=policy.policy_digest(),
        cells=cells,
    )
    _atomic_write(manifest_out, manifest.canonical_bytes())
    os.chmod(manifest_out, 0o600)
    return manifest_out


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="prepare_matrix")
    parser.add_argument("--source-spec", required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--manifest-out")
    args = parser.parse_args(argv)

    try:
        spec = mx.load_source_spec(args.source_spec)
        manifest_path = prepare(
            spec,
            results_root=args.results_root,
            work_root=args.work_root,
            manifest_out=args.manifest_out,
        )
    except (
        PrepareError,
        ValidationError,
        ValueError,
        policy.SemanticStagingError,
    ) as exc:
        print(f"prepare_matrix: FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"prepare_matrix: OK manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

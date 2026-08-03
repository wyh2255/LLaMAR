"""Real `BatchRunner`: hand a candidate skills tree to the validation chain.

This is the seam where a candidate stops being a text file and becomes a
measurement. It is also where this project's recurring defect would reappear: a
parameter accepted, written into metadata, and never actually in effect. That has
happened three times here (`temperature`, `skills_dir`, `prompt_dir`), so the
runner does not trust that `--skills-dir` worked -- it verifies, by comparing the
`prompt_hash` recorded in the run's own metadata against the hash of the candidate
tree it asked for.

If those disagree, the batch measured the champion while claiming to measure the
candidate. Every number from it is attributed to the wrong tree, so the runner
raises instead of returning results.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sar_orch.eval.aggregate import aggregate_all, scan_results


@dataclass
class ValidationBatchRunner:
    """Runs `scripts/run_validation.sh` against a candidate skills directory.

    Implements the `BatchRunner` protocol in `loop.py`.

    `repeats` must match the value the loop was configured with: fitness asserts
    it against `min_runs`, and a mismatch would validate one number while running
    another.
    """

    repo_root: Path
    cells_file: Path
    repeats: int = 3
    max_steps: int = 30
    prompt_dir: Path | None = None
    timeout_s: int = 60 * 60 * 6
    #: Set False only for tooling tests; a real generation must verify the tree.
    verify_prompt_hash: bool = True

    def __call__(self, skills_dir: Path, generation: int) -> tuple[dict, list[dict], str]:
        skills_dir = Path(skills_dir).resolve()
        tag = f"evolve_gen{generation:03d}"
        cmd = [
            "bash",
            "scripts/run_validation.sh",
            tag,
            "--repeats",
            str(self.repeats),
            "--cells",
            str(self.cells_file),
            "--max-steps",
            str(self.max_steps),
            "--skills-dir",
            str(skills_dir),
        ]
        if self.prompt_dir is not None:
            cmd += ["--prompt-dir", str(self.prompt_dir)]

        proc = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        batch_dir = _find_batch_dir(self.repo_root, tag)
        if batch_dir is None:
            raise RuntimeError(
                f"generation {generation}: no batch directory matching {tag!r} was "
                f"created.\nstdout tail:\n{proc.stdout[-1500:]}\n"
                f"stderr tail:\n{proc.stderr[-1500:]}"
            )

        reports, skipped = scan_results(batch_dir)
        if not reports:
            raise RuntimeError(
                f"generation {generation}: batch at {batch_dir} produced no usable "
                f"eval reports (skipped: {skipped}). Treating this as a failed "
                f"generation rather than a candidate rejection -- an empty batch "
                f"says nothing about the candidate."
            )

        if self.verify_prompt_hash:
            _assert_candidate_was_loaded(reports, skills_dir, generation)

        aggregate = aggregate_all(batch_dir)
        return aggregate, reports, str(batch_dir)


def _find_batch_dir(repo_root: Path, tag: str) -> Path | None:
    """Locate the batch the script created. Newest match wins."""
    results = Path(repo_root) / "sar_orch" / "results"
    if not results.is_dir():
        return None
    hits = sorted(p for p in results.glob(f"*_{tag}") if p.is_dir())
    return hits[-1] if hits else None


def _assert_candidate_was_loaded(
    reports: list[dict], skills_dir: Path, generation: int
) -> None:
    """Confirm the runs actually loaded the candidate tree.

    `prompt_hash` covers prompts *and* skills, so a candidate that differs from the
    champion by one skill file must produce a different hash. Every run in the batch
    must agree on it: a batch split across two trees is not a measurement of either.

    This is the direct regression test for the defect class where `--skills-dir` was
    accepted and then silently overridden by a derived path.
    """
    hashes = {
        (r.get("metadata", {}) or {}).get("prompt_hash") for r in reports
    }
    hashes.discard(None)
    if not hashes:
        raise RuntimeError(
            f"generation {generation}: no run recorded a prompt_hash, so there is no "
            f"evidence the candidate skills were loaded. Refusing to attribute these "
            f"results to the candidate."
        )
    if len(hashes) > 1:
        raise RuntimeError(
            f"generation {generation}: runs disagree on prompt_hash ({sorted(hashes)}); "
            f"the batch spans more than one skill/prompt tree and is not a measurement "
            f"of either."
        )

    # Recompute what the candidate tree *should* hash to, using the same function the
    # experiment uses, so the comparison cannot drift from the recorded value.
    from sar_orch.experiment import compute_prompt_hash

    recorded = hashes.pop()
    # The experiment hashes prompts + skills together; we only control skills here,
    # so compare against a recomputation over the same pair of roots.
    expected = compute_prompt_hash(_prompt_root_of(recorded), skills_dir)
    if expected != recorded:
        raise RuntimeError(
            f"generation {generation}: recorded prompt_hash {recorded!r} does not match "
            f"the candidate tree at {skills_dir} (expected {expected!r}). The batch "
            f"measured a different skill tree than the one requested -- most likely "
            f"--skills-dir was accepted but overridden downstream. Results discarded."
        )


def _prompt_root_of(_recorded_hash: str) -> Path:
    """Prompt root used for hash recomputation.

    Candidates only vary skills, so the prompt root is the live one. Kept as a
    function so a future prompt-varying candidate has one place to change.
    """
    return Path("sar_orch/prompts")


def load_baseline(batch_dir: Path | str) -> tuple[dict, list[dict]]:
    """Load an existing batch as the loop's baseline.

    Returns `(aggregate, per_run_reports)`. Both are needed: the aggregate for the
    gate, the per-run reports so fitness can check per-seed regression, which the
    aggregate has already pooled away.
    """
    batch_dir = Path(batch_dir)
    agg_path = batch_dir / "aggregate_report.json"
    if agg_path.exists():
        aggregate = json.loads(agg_path.read_text(encoding="utf-8"))
    else:
        aggregate = aggregate_all(batch_dir)
    reports, _ = scan_results(batch_dir)
    if not reports:
        raise RuntimeError(f"baseline batch at {batch_dir} has no eval reports")
    return aggregate, reports

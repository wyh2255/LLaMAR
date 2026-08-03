"""Generation orchestration: generate -> static gate -> experiment -> fitness -> promote.

Neither the LLM call nor the experiment runner lives here. Both are injected, so the
whole control flow -- dedup, termination, promotion, rollback -- is testable without
spending a token. That matters because the failure modes worth testing are the
expensive ones: promoting a regression, re-running an identical candidate, looping
forever on a stuck generator.

Four things this loop does that the previous one did not:

**Reads a verdict, not an exit code.** `gate` exits 0 both when a batch genuinely
passes and when an underpowered batch had every failure downgraded to a warning. Those
are opposite outcomes with the same exit status, so promotion goes through
`fitness.evaluate`, which reports the second as `inconclusive` and refuses to promote
on it. That is a third outcome, not a rejection: `rejections` carries findings about
the candidate and is fed to the next generator, whereas an unjudgeable batch says
nothing about the candidate and is recorded in `unjudged_reasons` instead.

**Deduplicates candidates by content.** A generator that re-proposes an identical
skill would otherwise cost a full batch (~870k tokens per run) to rediscover a known
result.

**Terminates on stagnation.** Consecutive rejections, and repeated duplicates, both
count -- a generator producing the same rejected text forever is not progressing even
though nothing "failed".

**Feeds rejections forward.** Each generation receives the prior verdicts. Without
that, a candidate rejected for leaking a coordinate comes back with the same
coordinate phrased differently, and the loop burns generations relearning the rule.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from sar_orch.evolve import fitness as fitness_mod
from sar_orch.evolve import promotion as promo
from sar_orch.evolve import static_gate
from sar_orch.evolve.leak_dictionary import LeakDictionary

HISTORY_FILE = "generations.jsonl"


@dataclass
class Candidate:
    """One proposed change: exactly one skill file, replaced wholesale.

    Whole-file content rather than a diff or a find-and-replace pair. An
    edit-by-match candidate fails in two ways that whole-file replacement cannot:
    the anchor may not exist (silent no-op that then gets measured as if the change
    applied), or it may match in several places. The cost is that the generator must
    reproduce the whole file, which for a <=600-word skill is not a real constraint.
    """

    path: str  # relative to the skills root, e.g. "worker/navigation/SKILL.md"
    content: str
    rationale: str = ""


@dataclass
class GenerationOutcome:
    """Auditable record of one generation."""

    generation: int
    #: static_reject | duplicate | fitness_reject | promoted | generator_exhausted
    #: | inconclusive
    outcome: str
    path: str = ""
    content_hash: str = ""
    rationale: str = ""
    #: Findings about the candidate's *content*. Fed forward to the next generation.
    rejections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    batch_dir: str = ""
    #: Why the batch could not be judged. Kept out of `rejections` on purpose: these
    #: describe the measurement, not the candidate, and must never be fed to the
    #: generator as "do not repeat this".
    unjudged_reasons: list[str] = field(default_factory=list)


@dataclass
class LoopConfig:
    max_generations: int = 5
    #: Stop after this many consecutive non-promotions (rejections or duplicates).
    max_consecutive_failures: int = 3
    #: Runs per cell. Must be >= gate's min_runs or fitness refuses (see fitness).
    repeats: int = 3
    word_tolerance: float = static_gate.DEFAULT_WORD_TOLERANCE
    absolute_max_words: int = static_gate.DEFAULT_ABSOLUTE_MAX_WORDS
    holdout_labels: set[str] = field(default_factory=set)


class BatchRunner(Protocol):
    """Runs a batch against a candidate skills tree and returns eval artefacts.

    Returns `(aggregate_report, per_run_reports, batch_dir)`. Returning the per-run
    reports as well as the aggregate is what lets fitness check per-seed regression;
    the aggregate alone has already pooled the seeds away.
    """

    def __call__(self, skills_dir: Path, generation: int) -> tuple[dict, list[dict], str]:
        ...


CandidateGenerator = Callable[[int, list[GenerationOutcome]], Candidate | None]


def content_hash(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def stage_candidate(
    *, champion_dir: Path | str, candidate: Candidate, dest: Path | str
) -> Path:
    """Copy the champion tree to `dest` and apply the candidate on top.

    Copy-then-overwrite, rather than writing only the changed file, so the candidate
    directory is a complete tree that `--skills-dir` can be pointed at directly. A
    partial tree would silently drop every other skill from the run.
    """
    champion_dir = Path(champion_dir)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(champion_dir, dest)
    target = dest / candidate.path
    if not target.parent.is_dir():
        raise ValueError(
            f"candidate path {candidate.path!r} does not exist in the champion tree; "
            "a candidate may modify an existing skill, not invent a new location"
        )
    target.write_text(candidate.content, encoding="utf-8")
    return dest


def append_history(state_dir: Path | str, outcome: GenerationOutcome) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / HISTORY_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")


def load_history(state_dir: Path | str) -> list[GenerationOutcome]:
    p = Path(state_dir) / HISTORY_FILE
    if not p.exists():
        return []
    out: list[GenerationOutcome] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(GenerationOutcome(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            # One unreadable line must not discard the rest of the history: the
            # remaining entries are still needed to avoid re-running candidates.
            continue
    return out


def format_rejection_feedback(history: list[GenerationOutcome], limit: int = 5) -> str:
    """Render prior rejections for the next generation's prompt.

    Assembled deterministically from the recorded verdicts rather than by asking an
    LLM to summarise them: a summarising pass costs tokens and can quietly drop the
    specific rule that was broken, which is the only part that matters here.
    """
    # Only outcomes that reflect a judgement on candidate content belong here.
    # `generator_exhausted` records that the loop ran out of candidates -- feeding
    # it back as "do not repeat this" would present an empty non-attempt as
    # guidance. `promoted` is likewise not something to avoid. `inconclusive` is the
    # same class of exclusion as `generator_exhausted`: the batch was too small to
    # judge, so nothing is known about the candidate. Listing it under "do not repeat
    # these" would tell the generator to avoid text that was never found wanting.
    rejected_kinds = {"static_reject", "fitness_reject", "duplicate"}
    recent = [h for h in history if h.outcome in rejected_kinds][-limit:]
    if not recent:
        return "No prior rejections."
    lines = ["Previously rejected attempts -- do not repeat these:"]
    for h in recent:
        lines.append(f"- generation {h.generation} ({h.outcome}) on {h.path}:")
        for r in h.rejections[:4]:
            lines.append(f"    * {r}")
        if not h.rejections and h.notes:
            lines.append(f"    * {h.notes[0]}")
    return "\n".join(lines)


def run_loop(
    *,
    champion_dir: Path | str,
    state_dir: Path | str,
    work_dir: Path | str,
    leaks: LeakDictionary,
    baseline_manifest: dict[str, int],
    baseline_aggregate: dict | None,
    baseline_reports: list[dict] | None,
    gate_config: dict,
    generate: CandidateGenerator,
    run_batch: BatchRunner,
    config: LoopConfig,
    timestamp_fn: Callable[[], str],
    archive_dir: Path | str | None = None,
) -> list[GenerationOutcome]:
    """Run generations until promotion budget, stagnation, or generator exhaustion.

    `timestamp_fn` is injected so runs are reproducible in tests.

    Returns every outcome, promoted or not. The rejected ones are the more useful
    record: they say what the generator kept getting wrong.
    """
    champion_dir = Path(champion_dir)
    state_dir = Path(state_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Fail before any generation rather than after the first batch: at repeats
    # below min_runs, every gate check downgrades to a warning and promotion would
    # be unconditional.
    fitness_mod.assert_adequate_repeats(config.repeats, gate_config)

    history = load_history(state_dir)
    seen_hashes = {h.content_hash for h in history if h.content_hash}
    outcomes: list[GenerationOutcome] = []
    consecutive_failures = 0
    start_gen = (max((h.generation for h in history), default=0)) + 1

    for gen in range(start_gen, start_gen + config.max_generations):
        if consecutive_failures >= config.max_consecutive_failures:
            break

        candidate = generate(gen, history + outcomes)
        if candidate is None:
            rec = GenerationOutcome(generation=gen, outcome="generator_exhausted")
            append_history(state_dir, rec)
            outcomes.append(rec)
            break

        chash = content_hash(candidate.content)
        base = GenerationOutcome(
            generation=gen,
            outcome="",
            path=candidate.path,
            content_hash=chash,
            rationale=candidate.rationale,
        )

        # Dedup before the static gate: identical content yields an identical
        # verdict, and re-running the batch would cost a full generation's budget to
        # rediscover a known result.
        if chash in seen_hashes:
            base.outcome = "duplicate"
            base.notes = [
                "identical content already evaluated in an earlier generation; "
                "counted toward stagnation because a generator repeating itself is "
                "not making progress"
            ]
            append_history(state_dir, base)
            outcomes.append(base)
            consecutive_failures += 1
            continue
        seen_hashes.add(chash)

        verdict = static_gate.evaluate(
            skill_paths=[candidate.path],
            content=candidate.content,
            baseline_words=baseline_manifest.get(candidate.path),
            leaks=leaks,
            tolerance=config.word_tolerance,
            absolute_max=config.absolute_max_words,
        )
        if not verdict.ok:
            base.outcome = "static_reject"
            base.rejections = verdict.rejections
            append_history(state_dir, base)
            outcomes.append(base)
            consecutive_failures += 1
            continue

        cand_dir = stage_candidate(
            champion_dir=champion_dir,
            candidate=candidate,
            dest=work_dir / f"gen{gen:03d}_skills",
        )
        aggregate, per_run, batch_dir = run_batch(cand_dir, gen)
        base.batch_dir = batch_dir

        fit = fitness_mod.evaluate(
            current=aggregate,
            baseline=baseline_aggregate,
            config=gate_config,
            repeats=config.repeats,
            current_reports=per_run,
            baseline_reports=baseline_reports,
            holdout_labels=config.holdout_labels or None,
        )
        base.metrics = fit.metrics
        base.notes = fit.notes

        # Checked before the blocker branch and before promotion. `fit.promote` is
        # already False here, but "rejected" would be the wrong record: an
        # inconclusive batch produced no finding about the candidate, and filing it
        # as a rejection would both slander the candidate and feed the generator a
        # nonexistent defect to avoid.
        if fit.inconclusive and not fit.blockers:
            base.outcome = "inconclusive"
            base.unjudged_reasons = fit.inconclusive_reasons
            append_history(state_dir, base)
            outcomes.append(base)
            # Stop, the way generator exhaustion stops the loop, rather than counting
            # toward stagnation. Both are faults in the machinery rather than in a
            # candidate, and neither improves by trying again: the cause here is runs
            # dying before they write a report, which the next generation inherits
            # unchanged. Continuing spends a full batch (~870k tokens per run) on
            # another unjudgeable result. Retrying with more repeats was the
            # alternative and is unsound from here -- the repeat count lives on the
            # injected `BatchRunner`, and `runner.py` requires it to match
            # `config.repeats` exactly, so the loop raising one of them silently
            # desyncs the number fitness asserts on from the number actually run.
            break

        if not fit.promote:
            base.outcome = "fitness_reject"
            base.rejections = fit.blockers
            append_history(state_dir, base)
            outcomes.append(base)
            consecutive_failures += 1
            continue

        promo.promote(
            candidate_dir=cand_dir,
            live_dir=champion_dir,
            state_dir=state_dir,
            generation=gen,
            metrics=fit.metrics,
            changed_path=candidate.path,
            note=candidate.rationale,
            timestamp=timestamp_fn(),
            archive_dir=archive_dir,
        )
        base.outcome = "promoted"
        append_history(state_dir, base)
        outcomes.append(base)
        consecutive_failures = 0

        # The promoted candidate is the new baseline for the generations that
        # follow. Comparing later candidates against the original baseline would
        # let an improvement be re-credited every generation.
        baseline_aggregate = aggregate
        baseline_reports = per_run

    return outcomes


def summarise(outcomes: list[GenerationOutcome]) -> str:
    """Plain-text run summary, assembled from the records themselves."""
    if not outcomes:
        return "no generations ran"
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    lines = [f"{len(outcomes)} generation(s): " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())
    )]
    for o in outcomes:
        head = f"  gen{o.generation:03d} {o.outcome:18s} {o.path or '-'}"
        lines.append(head)
        for r in o.rejections[:2]:
            lines.append(f"      {r[:100]}")
        for r in o.unjudged_reasons[:2]:
            lines.append(f"      unjudged: {r[:100]}")
    promoted = [o for o in outcomes if o.outcome == "promoted"]
    if not promoted:
        lines.append("  no candidate was promoted; the champion is unchanged")
    if any(o.outcome == "inconclusive" for o in outcomes):
        # Say it in the summary, not only in the JSONL: an inconclusive stop looks
        # like a clean stop from the outside, and the required action -- raise
        # repeats or find out why runs died, then re-run -- differs from every other
        # termination reason.
        lines.append(
            "  the loop stopped on an unjudgeable batch: too few runs survived for "
            "the gate to reach a verdict. The candidate was neither accepted nor "
            "found wanting. Raise --repeats (or fix whatever killed the runs) and "
            "re-run; do not read this as a rejection."
        )
    return "\n".join(lines)

"""Promotion decision: a thin wrapper over `eval/gate.py`, plus the checks gate
cannot make on its own.

Fitness is not a new scoring function. It is the existing regression gate, so a
candidate is judged by the same code that guards every other comparison in this
project. What this module adds is the three ways a gate pass can be hollow:

**1. Underpowered batches pass by default.** `gate` downgrades every failing check
to `warn` when a group has fewer runs than `min_runs`, and `GateResult.passed` only
looks at failures. So at `repeats=1` a batch cannot fail -- it reports PASSED with a
pile of warnings. Under budget pressure `--repeats 1` is exactly what someone
reaches for, and it silently disables rollback. Hence a hard assertion rather than a
warning.

Two independent defences, because the declared repeat count and the observed run
count diverge. `assert_adequate_repeats` reads the caller's `repeats` and fires
*before* a token is spent; `GateResult.inconclusive` reads what the batch actually
produced and fires *after*. A batch launched with an honest `repeats=3` still lands
at `n=1` when two runs crash, time out, or hit a gateway error and never write an
`eval_report.json` -- the assertion sees nothing wrong and every collapsed check is
already a warning, so `gate.passed` reads True. That is the case
`FitnessVerdict.inconclusive` covers. Neither check subsumes the other: the
converse gap is a `repeats=1` smoke batch whose metrics all sit inside tolerance,
where nothing is downgraded, `inconclusive` is False, and only the assertion stands
between that batch and unconditional promotion.

**2. Pooling hides single-seed collapse.** `group_by_key` keys on
`(scene, agents)`; seeds are pooled. A candidate can lift the group mean while
destroying one seed, and the gate sees only the mean. So regression is also checked
per seed.

**3. Holdout groups are structurally the weakest link.** Paired groups share a
`(scene, agents)` key across several seeds, so they naturally reach n=3N and are
immune to the underpower downgrade. A holdout group is by definition a single
`(scene, agents)` combination, so its n equals the repeat count exactly -- it is the
only singleton group in the scheme. Drop repeats and the group meant to catch
overfitting is the first check to stop working. So holdouts get an independent floor
and any warning on them blocks automatic promotion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sar_orch.eval import gate


@dataclass
class FitnessVerdict:
    """Whether this candidate may be promoted, and why.

    Three outcomes, not two. `promote` is False for both a rejection and an
    inconclusive batch, but they are not the same event and must not be reported as
    one: a rejection is a statement about the candidate, an inconclusive batch is a
    statement about the measurement. `inconclusive` separates them.
    """

    promote: bool
    #: Blocking problems. Non-empty means no promotion.
    #:
    #: Reserved for findings that say something about *this candidate* -- they are
    #: fed to the next generation as "do not repeat this". Reasons that describe the
    #: batch rather than the candidate belong in `inconclusive_reasons`.
    blockers: list[str] = field(default_factory=list)
    #: Non-blocking observations worth recording with the generation.
    notes: list[str] = field(default_factory=list)
    #: Metrics carried into the champion record for later attribution.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: True when the gate itself passed, before this module's extra checks.
    gate_passed: bool = False
    #: True when the gate could not reach a verdict: checks that would have failed
    #: were downgraded because the group's observed `n` fell below `min_runs`.
    #: Blocks promotion, but is *not* a rejection -- see `inconclusive_reasons`.
    inconclusive: bool = False
    #: Why the batch is unjudgeable. Deliberately separate from `blockers` so this
    #: never reaches candidate-generation feedback as a candidate defect.
    inconclusive_reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        # Blockers win the headline when both are present: a real finding about the
        # candidate is actionable, "the batch was too small to tell" is not, and the
        # actionable one should not be hidden behind the other.
        if self.blockers:
            head = "fitness: REJECT"
        elif self.inconclusive:
            head = "fitness: INCONCLUSIVE (no promotion; batch cannot be judged)"
        else:
            head = "fitness: PROMOTE"
        lines = [head]
        for r in self.inconclusive_reasons:
            lines.append(f"  UNJUDGED {r}")
        for b in self.blockers:
            lines.append(f"  BLOCK {b}")
        for n in self.notes:
            lines.append(f"  note  {n}")
        return "\n".join(lines)


def assert_adequate_repeats(repeats: int, config: dict[str, Any]) -> None:
    """Refuse to evaluate a batch that cannot fail.

    At `repeats < min_runs` the gate downgrades every failure to a warning and
    reports PASSED, so promotion becomes unconditional. That is worse than having no
    gate: it looks like a check. Raising here rejects the generation rather than
    quietly relaxing the standard.
    """
    min_runs = int(config.get("min_runs", 2) or 2)
    if repeats < min_runs:
        raise ValueError(
            f"repeats={repeats} is below min_runs={min_runs}. Every gate check would "
            f"downgrade to 'warn' and the batch would report PASSED regardless of "
            f"results, making promotion unconditional. Raise repeats or lower "
            f"min_runs deliberately -- do not run a gate that cannot fail."
        )


def _episodes_by_seed(reports: list[dict]) -> dict[tuple[Any, Any, Any], list[dict]]:
    """Group per-run reports by (scene, agents, seed) -- finer than the gate."""
    out: dict[tuple[Any, Any, Any], list[dict]] = {}
    for r in reports:
        m = r.get("metadata", {}) or {}
        key = (m.get("scene"), m.get("agents"), m.get("seed"))
        out.setdefault(key, []).append(r)
    return out


def check_per_seed_regression(
    current_reports: list[dict],
    baseline_reports: list[dict],
    *,
    metric: str = "coverage",
    max_drop: float = 0.25,
) -> list[str]:
    """Catch a candidate that lifts the group mean while wrecking one seed.

    The gate pools seeds inside a `(scene, agents)` group, so a mean can improve
    while an individual cell collapses. Uses a continuous metric rather than the
    binary outcome because per-seed n is tiny -- a single finished/not-finished flip
    is not evidence, whereas a large coverage drop is.

    Seeds absent from either side are skipped, not treated as regressions: a missing
    cell is a batch-composition problem and is reported separately by the gate's
    completeness check.
    """
    problems: list[str] = []
    cur = _episodes_by_seed(current_reports)
    base = _episodes_by_seed(baseline_reports)
    for key in sorted(set(cur) & set(base), key=lambda k: tuple(str(x) for x in k)):
        cur_vals = [
            r["episode"][metric]
            for r in cur[key]
            if (r.get("episode") or {}).get(metric) is not None
        ]
        base_vals = [
            r["episode"][metric]
            for r in base[key]
            if (r.get("episode") or {}).get(metric) is not None
        ]
        if not cur_vals or not base_vals:
            continue
        cur_mean = sum(cur_vals) / len(cur_vals)
        base_mean = sum(base_vals) / len(base_vals)
        drop = base_mean - cur_mean
        if drop > max_drop:
            scene, agents, seed = key
            problems.append(
                f"seed-level regression at scene={scene} agents={agents} seed={seed}: "
                f"{metric} {base_mean:.3f} -> {cur_mean:.3f} (drop {drop:.3f} > "
                f"{max_drop:.2f}). The pooled group mean can hide this."
            )
    return problems


def check_holdout_health(
    gate_result: gate.GateResult,
    holdout_labels: set[str],
    *,
    min_holdout_runs: int = 2,
) -> list[str]:
    """Holdout groups must be genuinely evaluated, not warned past.

    A holdout is a singleton `(scene, agents)` group, so its n equals the repeat
    count. If it slipped below `min_runs`, its checks were downgraded and the
    mechanism intended to detect overfitting produced nothing. Reading only the
    gate's exit code cannot distinguish that from a real pass.
    """
    problems: list[str] = []
    seen = {c.group for c in gate_result.checks}
    for label in sorted(holdout_labels):
        if label not in seen:
            problems.append(
                f"holdout group {label} is absent from the gate report -- it was "
                f"never evaluated, so overfitting could not be detected"
            )
            continue
        for c in gate_result.checks:
            if c.group != label:
                continue
            if c.status == "fail":
                problems.append(f"holdout {label} failed {c.metric}: {c.reason}")
            elif c.status == "warn":
                # A warning on a holdout is not tolerable the way it is elsewhere:
                # the usual cause is underpower, which means this group produced no
                # verdict at all.
                problems.append(
                    f"holdout {label} only warned on {c.metric} ({c.reason}); a "
                    f"holdout must produce a real verdict, so this blocks automatic "
                    f"promotion pending review"
                )
            if c.metric == "min_runs" and (c.actual or 0) < min_holdout_runs:
                problems.append(
                    f"holdout {label} ran {c.actual:.0f} time(s), below the holdout "
                    f"floor of {min_holdout_runs}; holdout repeats must not be cut "
                    f"to save budget -- that disables the anti-overfitting check"
                )
    return problems


def evaluate(
    *,
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    config: dict[str, Any],
    repeats: int,
    current_reports: list[dict] | None = None,
    baseline_reports: list[dict] | None = None,
    holdout_labels: set[str] | None = None,
) -> FitnessVerdict:
    """Full promotion decision.

    Raises ValueError when `repeats` is too low to permit a meaningful verdict --
    that is a setup error, not a candidate outcome, and must not be reported as a
    rejection (which would look like the candidate's fault).

    Returns an inconclusive verdict (`promote=False`, `inconclusive=True`, empty
    `blockers`) when the gate's own checks were downgraded for want of runs. Same
    reasoning as the raise above, one step later: nothing there is the candidate's
    fault either, so it does not go in `blockers`.
    """
    assert_adequate_repeats(repeats, config)

    result = gate.evaluate_gate(current=current, baseline=baseline, config=config)
    blockers: list[str] = []
    notes: list[str] = []

    for c in result.failures:
        blockers.append(f"gate fail {c.group}/{c.metric}: {c.reason}")

    # Config drift makes the batch unpoolable; its CI no longer describes any
    # single configuration, so a "pass" over it means nothing.
    for c in result.checks:
        if c.metric.startswith("config:") and c.status == "fail":
            blockers.append(f"config drift {c.group}/{c.metric}: {c.reason}")

    if holdout_labels:
        blockers += check_holdout_health(result, holdout_labels)

    if current_reports and baseline_reports:
        blockers += check_per_seed_regression(current_reports, baseline_reports)
    elif baseline is not None:
        notes.append(
            "per-seed regression check skipped (per-run reports not supplied); "
            "pooled group means can hide a single-seed collapse"
        )

    for c in result.warnings:
        notes.append(f"gate warn {c.group}/{c.metric}: {c.reason}")

    metrics: dict[str, Any] = {}
    for g in current.get("groups", []) or []:
        metrics[gate.group_label(g)] = gate.extract_metrics(g)

    # `gate.passed` stays True here -- every collapsed check is already a warning --
    # so promotion cannot be decided from it alone. `inconclusive` is the gate's own
    # signal that its verdict is not usable, and it is read rather than reconstructed
    # from the warning text: the reasons are display strings whose wording will move.
    inconclusive_reasons: list[str] = []
    if result.inconclusive:
        inconclusive_reasons.append(
            f"the gate reached no verdict: {len(result.downgraded)} check(s) that "
            f"would have failed were downgraded because the group ran fewer than "
            f"min_runs={config.get('min_runs')} times. This batch is not evidence "
            f"the candidate is good, nor that it is bad -- re-run with more repeats "
            f"before judging it."
        )
        for c in result.downgraded:
            inconclusive_reasons.append(
                f"would have failed: {c.group}/{c.metric} ({c.check}): {c.reason}"
            )

    return FitnessVerdict(
        # An inconclusive batch is refused promotion even with no blockers: promoting
        # on it would make the champion depend on a measurement that showed nothing.
        promote=not blockers and not result.inconclusive,
        blockers=blockers,
        notes=notes,
        metrics=metrics,
        gate_passed=result.passed,
        inconclusive=result.inconclusive,
        inconclusive_reasons=inconclusive_reasons,
    )

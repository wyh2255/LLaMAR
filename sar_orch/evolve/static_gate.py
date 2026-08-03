"""Mechanical checks a candidate must pass before any experiment is run.

Everything here is free -- no LLM calls, no experiments. That matters because the
alternative is spending ~870k tokens per run discovering that a candidate was
never admissible.

Four checks:

  1. **anti-cheating** -- scene ground truth must not appear in the text
     (`leak_dictionary`)
  2. **word budget** -- anchored to a *fixed* baseline, never to the current
     champion
  3. **schema** -- the file must still look like a SKILL.md
  4. **single file** -- one candidate changes exactly one skill

On (2): a "champion + 10%" rule compounds. Ten generations of +10% is 2.59x, and
four is 1.46x -- which is precisely the 43-64% growth four rounds of *human*
editing produced here. A sliding anchor does not restrain growth, it schedules it.
So the anchor is an absolute word count captured once.

On (4): the project rule is one variable per change. Without a mechanical check
that rule survives exactly as long as everyone remembers it -- and a candidate that
edits three skills at once produces a result that cannot be attributed to any of
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sar_orch.evolve.leak_dictionary import LeakDictionary, LeakHit, scan_text

#: Growth allowed over the fixed baseline word count.
DEFAULT_WORD_TOLERANCE = 0.10

#: Absolute ceiling regardless of baseline, so a short skill cannot balloon by
#: exploiting a generous percentage.
DEFAULT_ABSOLUTE_MAX_WORDS = 600

#: Headings a SKILL.md is expected to retain.
REQUIRED_HEADING_RE = re.compile(r"^#\s+\S", re.MULTILINE)


@dataclass
class GateVerdict:
    """Outcome of the static checks. `ok` means the candidate may be run."""

    ok: bool
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    leak_hits: list[LeakHit] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "static gate: PASS" + (
                f" ({len(self.warnings)} warning(s))" if self.warnings else ""
            )
        return "static gate: REJECT\n" + "\n".join(f"  - {r}" for r in self.rejections)


def check_single_file(paths: list[str]) -> list[str]:
    """One candidate must touch exactly one skill file."""
    unique = sorted(set(paths))
    if not unique:
        return ["candidate changes no files"]
    if len(unique) > 1:
        return [
            "candidate changes more than one skill file "
            f"({len(unique)}: {unique}); one variable per generation, otherwise a "
            "result cannot be attributed to any single change"
        ]
    return []


def check_word_budget(
    text: str,
    baseline_words: int | None,
    *,
    tolerance: float = DEFAULT_WORD_TOLERANCE,
    absolute_max: int = DEFAULT_ABSOLUTE_MAX_WORDS,
) -> list[str]:
    """Enforce the word budget against a fixed baseline plus a hard ceiling.

    `baseline_words is None` means no baseline was recorded for this path -- that
    is a setup error, not a pass. Treating it as a pass is how an unbounded
    candidate would slip through.
    """
    problems: list[str] = []
    count = len(text.split())
    if count > absolute_max:
        problems.append(
            f"{count} words exceeds the absolute ceiling of {absolute_max}"
        )
    if baseline_words is None:
        problems.append(
            "no baseline word count recorded for this skill path; cannot evaluate "
            "the budget (fix the baseline manifest rather than skipping the check)"
        )
        return problems
    limit = int(baseline_words * (1 + tolerance))
    if count > limit:
        problems.append(
            f"{count} words exceeds baseline {baseline_words} +{tolerance:.0%} "
            f"= {limit}. The anchor is deliberately fixed: a champion-relative "
            f"budget compounds ({(1 + tolerance) ** 10:.2f}x over ten generations)"
        )
    return problems


def check_schema(text: str) -> list[str]:
    """A candidate must still be a usable SKILL.md."""
    problems: list[str] = []
    if not text.strip():
        problems.append("candidate content is empty")
        return problems
    if not REQUIRED_HEADING_RE.search(text):
        problems.append("no top-level '# ' heading found; not a SKILL.md")
    if "\x00" in text:
        problems.append("content contains a NUL byte")
    return problems


def check_anti_cheating(
    text: str, leaks: LeakDictionary
) -> tuple[list[str], list[LeakHit]]:
    """Reject any candidate containing scene ground truth.

    An empty leak dictionary is a **setup failure**, not a clean result: it means
    the scene directory could not be read, so nothing was actually checked. Passing
    in that state would disable anti-cheating silently, which is worse than
    stopping.
    """
    if leaks.is_empty():
        return (
            [
                "leak dictionary is empty -- scene ground truth could not be read, "
                "so the anti-cheating check did not run. Refusing rather than "
                "passing unchecked"
            ],
            [],
        )
    hits = scan_text(text, leaks)
    if not hits:
        return [], []
    return (
        [
            "candidate contains scene ground truth: "
            + "; ".join(str(h) for h in hits[:6])
            + (" ..." if len(hits) > 6 else "")
        ],
        hits,
    )


def evaluate(
    *,
    skill_paths: list[str],
    content: str,
    baseline_words: int | None,
    leaks: LeakDictionary,
    tolerance: float = DEFAULT_WORD_TOLERANCE,
    absolute_max: int = DEFAULT_ABSOLUTE_MAX_WORDS,
) -> GateVerdict:
    """Run every static check. All of them, then report together.

    Deliberately not short-circuiting: a generator that gets one rejection at a
    time needs one generation per problem to converge. Reporting everything lets
    the next attempt fix all of it.
    """
    rejections: list[str] = []
    rejections += check_single_file(skill_paths)
    rejections += check_schema(content)
    rejections += check_word_budget(
        content, baseline_words, tolerance=tolerance, absolute_max=absolute_max
    )
    cheat_problems, hits = check_anti_cheating(content, leaks)
    rejections += cheat_problems
    return GateVerdict(ok=not rejections, rejections=rejections, leak_hits=hits)


# ---------------------------------------------------------------------------
# Baseline manifest
# ---------------------------------------------------------------------------


def build_baseline_manifest(skills_dir: Path | str) -> dict[str, int]:
    """Snapshot word counts per skill path, to be captured once and then frozen.

    Keys are paths relative to `skills_dir`, so the manifest stays valid when the
    tree is copied to a candidate directory.

    `wc -w`-equivalent counting, including code blocks and tables: it matches the
    measurement the growth audit used, which keeps the numbers comparable.
    """
    skills_dir = Path(skills_dir)
    manifest: dict[str, int] = {}
    for path in sorted(skills_dir.rglob("*.md")):
        rel = path.relative_to(skills_dir).as_posix()
        manifest[rel] = len(path.read_text(encoding="utf-8").split())
    return manifest

"""Extract scene ground truth so candidate skills can be checked against it.

The threat is not a malicious generator. It is a competent one: an agent asked to
improve a skill will naturally try to understand the problem, and reading
`SAR/Scenes/scene_*.py` is the most direct way to do that. Having read it, it can
write a rule that is genuinely useful for these scenes and useless as a general
strategy -- "prioritise the reservoir at the map's lower-left" is a transcription
of `position=(15,5)` wearing a paraphrase.

Prevention comes first (deny filesystem access to `SAR/`, see the sandbox policy in
`static_gate`). This module is the second layer, for detecting what slipped through.

Three tiers of signal, by strength:

  1. **Engine-internal parameter names** (`extra_load`, `find_probability`,
     `enclosing_grid`, `amt_light`, `tp=`). These are `Arg()` constructor keywords.
     An agent cannot learn them from any observation -- they never appear in the
     environment's output. Their presence is proof the scene file was read, and is
     a stronger signal than a name or a coordinate.
  2. **Entity names** (`ReservoirUtah`, `LostPersonTimmy`, `CaldorFire`). An agent
     *does* see these at runtime, so a skill mentioning one is scene-specific
     rather than necessarily leaked -- but either way it must not be baked into a
     general strategy.
  3. **Coordinates** (`(15,5)`, `position=(2,2)`). Matched with a tolerance,
     because writing an adjacent cell is the obvious way to evade an exact match.

Coverage note: scanning must include fenced code blocks and inline code spans. A
scanner that strips markdown code first -- the natural thing to do to reduce false
positives -- turns those blocks into the one reliable hiding place.

Additional evasions covered (found by review + follow-up probing, all verified
against the live `sar_orch/prompts/` and `sar_orch/skills/` corpus to add zero new
false positives before landing):

  - **Coordinates written as arithmetic or hex**: `(3*5,5)`, `(0xF,5)`. Evaluated
    with a small hand-rolled evaluator bound to a fixed grammar (one literal, or
    two literals joined by one `+`/`-`/`*`) -- never `eval()`, since the grammar is
    fixed by the regex before evaluation ever runs. Same ±1 tolerance as the
    original paren form, since it *is* the original form, just with a term that
    needs evaluating first.
  - **Coordinates in brackets or `x=.., y=..` form**: `[15,5]`, `x=15, y=5`.
    Matched *exactly*, not with tolerance -- unlike the paren form, these are new
    surface added by this patch, and testing found that bracket lists are common
    in ordinary skill prose (JSON examples, `[x, y, z]` triples) for reasons that
    have nothing to do with scene coordinates. Tolerance on top of that surface
    produced a false positive (`"position": [5, 4, 0]` in an example landed
    within 1 of a real `(5,5)`); exact-only removes it while keeping the format
    useful against literal transcription.
  - **Entity names split into words**: `Lost Person Timmy`, `Lost-Person-Timmy`,
    `Lost_Person_Timmy`. Detected by re-tokenizing into words (splitting on
    CamelCase boundaries *and* underscores) and looking for the same word
    sequence with any separator between them.
  - **Entity/engine names obfuscated letter-by-letter**: `L.o.s.t.P.e.r.s.o.n`,
    `e-x-t-r-a_load`. Detected with a per-character regex that tolerates a single
    stray `.`/`-`/`_` between consecutive letters.
  - **Full-width characters and zero-width joiners**: `（１５，５）`,
    `Lost​Person​Timmy`. Folded away by NFKC normalization plus
    stripping Unicode category Cf (format characters) before any of the above
    runs, so exact/fuzzy matching already covers them without new grammar.

Deliberately NOT covered (see `TestKnownLimitation` in the test suite for the
worked examples and the reasoning):

  - Coordinates split across sentences ("x is 15 ... y is 5") -- unbounded false
    positive surface, no numeric pair to anchor on.
  - Coordinates spelled out as number words ("fifteen, five") or written as a
    ratio ("15/5") -- the words grammar is large and error-prone, and slash pairs
    collide with ordinary step-count prose ("3/10 steps").
  - Semantic paraphrase ("the reservoir in the lower-left quadrant") -- this was
    already a known limitation and remains one; regex cannot approximate meaning.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

#: `Arg()` keyword names. Not obtainable through any observation, so a hit here is
#: the strongest available evidence that scene definitions were read.
ENGINE_SYMBOLS: tuple[str, ...] = (
    "extra_load",
    "find_probability",
    "enclosing_grid",
    "amt_light",
    "amt_regions",
    "grid_size",
    "task_timeout",
)

#: Scene-file identifiers that are not themselves secret but should never be
#: hardcoded into a general skill.
STRUCTURAL_SYMBOLS: tuple[str, ...] = (
    "SceneInitializer",
    "BaseSceneInitializer",
    "scene_initializer",
    "get_scene_init",
)

#: A coordinate term: a decimal or hex literal, optionally combined with one more
#: literal via a single +/-/* operator. Deliberately narrow -- one operator, no
#: nesting, no parentheses inside a term -- both to keep `_eval_term` a trivial,
#: auditable parser (never Python `eval`) and to avoid flagging ordinary arithmetic
#: prose ("3-10 steps"). The narrowness is a false-positive control, not laziness:
#: probing the live prompts/skills corpus found zero collisions at this width and
#: several at a naively wider one (e.g. matching bare "N/M" step-count ratios).
_NUM_RE = r"(?:0[xX][0-9a-fA-F]{1,2}|\d{1,3})"
_TERM_RE = rf"{_NUM_RE}(?:\s*[+\-*]\s*{_NUM_RE})?"

#: Coordinates written with parens: `(15,5)`, `(3*5,5)`. Matched with the caller's
#: tolerance, same as before this patch -- this is the original format, and the
#: ±1 tolerance on it is an established, tested tradeoff
#: (`test_off_by_one_coordinate_is_blocked`).
_COORD_RE = re.compile(
    rf"\(\s*({_TERM_RE})\s*,\s*({_TERM_RE})\s*(?:,\s*{_TERM_RE}\s*)?\)"
)

#: Coordinates written with brackets (`[15,5,0]`) or as `x=15, y=5`. These two new
#: formats are matched *exactly*, never with tolerance: skills legitimately use
#: bracketed lists for unrelated purposes (an example `"position": [5, 4, 0]` in a
#: report_observation() sample is not a leak), and applying the same ±1 tolerance
#: here turned that into a false positive during testing -- any bracketed triple
#: that merely lands next to a real scene coordinate lit up. Exact-only keeps the
#: new formats useful against the literal-transcription evasion they were added
#: for, without adopting the paren format's wider net.
_COORD_BRACKET_RE = re.compile(
    rf"\[\s*({_TERM_RE})\s*,\s*({_TERM_RE})\s*(?:,\s*{_TERM_RE}\s*)?\]"
)
_COORD_XY_RE = re.compile(
    rf"x\s*=\s*({_TERM_RE})\s*,\s*y\s*=\s*({_TERM_RE})", re.IGNORECASE
)


def _eval_term(term: str) -> int | None:
    """Evaluate a `_TERM_RE` match: one literal, or two joined by one operator.

    Not `ast.literal_eval` (rejects `3*5`, it isn't a literal) and not `eval`
    (arbitrary code execution) -- the grammar is already pinned by `_TERM_RE`
    before this ever runs, so this just re-parses that same fixed shape.
    """
    term = term.strip()
    for op in ("+", "-", "*"):
        if op in term:
            left, _, right = term.partition(op)
            a, b = _eval_literal(left), _eval_literal(right)
            if a is None or b is None:
                return None
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            return a * b
    return _eval_literal(term)


def _eval_literal(tok: str) -> int | None:
    tok = tok.strip()
    try:
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        return int(tok, 10)
    except ValueError:
        return None


@dataclass(frozen=True)
class LeakDictionary:
    """Ground-truth literals harvested from the scene definitions."""

    names: frozenset[str] = field(default_factory=frozenset)
    coordinates: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    scene_files: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.names and not self.coordinates


def _iter_scene_files(scenes_dir: Path) -> list[Path]:
    return sorted(p for p in scenes_dir.glob("scene_*.py") if p.is_file())


def build(scenes_dir: Path | str = "SAR/Scenes") -> LeakDictionary:
    """Parse scene files with `ast` and collect names + coordinates.

    `ast`, not regex or import: the scene modules import from `core`, which pulls
    in the whole engine, and this runs inside a gate that must not depend on the
    simulator being importable. Parsing also means we read the files without
    executing them.

    These files are read strictly to build a denylist. Nothing here modifies
    `SAR/`, which is protected: its physics and completion criteria define the
    benchmark, so changing them would invalidate every measurement.
    """
    scenes_dir = Path(scenes_dir)
    names: set[str] = set()
    coords: set[tuple[int, int]] = set()
    files: list[str] = []

    for path in _iter_scene_files(scenes_dir):
        files.append(path.name)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            # A scene we cannot parse yields no entries. Callers must treat an
            # empty dictionary as "unable to check", never as "nothing to find".
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        if isinstance(kw.value.value, str):
                            names.add(kw.value.value)
                    if kw.arg in ("position", "grid_size"):
                        pt = _as_point(kw.value)
                        if pt:
                            coords.add(pt)
            # Bare tuples elsewhere in the params dict (defensive: a scene may
            # list positions without the keyword).
            if isinstance(node, ast.Tuple):
                pt = _as_point(node)
                if pt:
                    coords.add(pt)

    return LeakDictionary(
        names=frozenset(names),
        coordinates=frozenset(coords),
        scene_files=tuple(files),
    )


def _as_point(node: ast.AST) -> tuple[int, int] | None:
    if not isinstance(node, ast.Tuple) or len(node.elts) < 2:
        return None
    vals = []
    for el in node.elts[:2]:
        if isinstance(el, ast.Constant) and isinstance(el.value, int):
            vals.append(el.value)
        else:
            return None
    return (vals[0], vals[1])


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeakHit:
    kind: str  # engine_symbol | structural_symbol | entity_name | coordinate
    value: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}" + (f" ({self.detail})" if self.detail else "")


def _normalize(text: str) -> str:
    """NFKC-fold full-width/exotic digit and punctuation variants to ASCII, then
    drop Unicode category Cf (zero-width joiners, BOM, etc).

    Both are common-enough obfuscations that it is cheaper to neutralize them once,
    up front, than to make every matcher below Unicode-aware. NFKC turns full-width
    `（１５，５）` into ascii `(15,5)`; stripping Cf turns `Lost​Person` into
    `LostPerson`. Applied before exact/near/coordinate matching, so those need no
    changes to benefit.
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def scan_text(
    text: str,
    leaks: LeakDictionary,
    *,
    coord_tolerance: int = 1,
    name_edit_distance: int = 1,
) -> list[LeakHit]:
    """Find scene ground truth in candidate text.

    The whole text is scanned, including fenced blocks and inline code. Tolerances
    exist because exact matching is trivially evaded: shift a coordinate by one
    cell, drop a letter from a name.

    Returns hits ordered by signal strength, engine symbols first.
    """
    hits: list[LeakHit] = []
    normalized = _normalize(text)
    lowered = normalized.lower()

    for sym in ENGINE_SYMBOLS:
        if sym in lowered:
            hits.append(
                LeakHit(
                    "engine_symbol",
                    sym,
                    "Arg() keyword -- not observable at runtime, so its presence "
                    "indicates scene definitions were read",
                )
            )
        elif _split_name_match(lowered, sym):
            hits.append(
                LeakHit(
                    "engine_symbol", sym, "keyword split into separate words"
                )
            )
        elif _letter_obfuscated(lowered, sym):
            hits.append(
                LeakHit(
                    "engine_symbol",
                    sym,
                    "letter-by-letter obfuscation of an Arg() keyword",
                )
            )
    for sym in STRUCTURAL_SYMBOLS:
        if sym.lower() in lowered:
            hits.append(LeakHit("structural_symbol", sym, "scene-file identifier"))

    for name in sorted(leaks.names):
        if name.lower() in lowered:
            hits.append(LeakHit("entity_name", name, "exact match"))
        elif name_edit_distance > 0 and (
            near := _near_token(lowered, name.lower(), name_edit_distance)
        ):
            hits.append(LeakHit("entity_name", name, f"near match {near!r}"))
        elif _split_name_match(lowered, name):
            hits.append(
                LeakHit("entity_name", name, "name split into separate words")
            )
        elif _letter_obfuscated(lowered, name):
            hits.append(
                LeakHit("entity_name", name, "letter-by-letter obfuscation")
            )

    if leaks.coordinates:
        for pt in _iter_terms(_COORD_RE, normalized):
            known = _within_tolerance(pt, leaks.coordinates, coord_tolerance)
            if known is not None:
                hits.append(
                    LeakHit(
                        "coordinate",
                        f"{pt}",
                        f"within {coord_tolerance} of scene literal {known}",
                    )
                )
        # Bracket and `x=,y=` forms are exact-only -- see `_COORD_BRACKET_RE`.
        for pt in _iter_terms(_COORD_BRACKET_RE, normalized):
            if pt in leaks.coordinates:
                hits.append(
                    LeakHit("coordinate", f"{pt}", "exact match, bracket form")
                )
        for pt in _iter_terms(_COORD_XY_RE, normalized):
            if pt in leaks.coordinates:
                hits.append(
                    LeakHit("coordinate", f"{pt}", "exact match, x=/y= form")
                )

    order = {
        "engine_symbol": 0,
        "structural_symbol": 1,
        "entity_name": 2,
        "coordinate": 3,
    }
    return sorted(hits, key=lambda h: (order.get(h.kind, 9), h.value))


def _iter_terms(pattern: re.Pattern[str], normalized_text: str):
    """Yield every `(x, y)` pair matched by `pattern`, evaluating each term through
    `_eval_term` so arithmetic (`3*5`) and hex (`0xF`) literals resolve to plain
    ints alongside decimal ones.
    """
    seen: set[tuple[int, int]] = set()
    for m in pattern.finditer(normalized_text):
        x, y = _eval_term(m.group(1)), _eval_term(m.group(2))
        if x is not None and y is not None and (x, y) not in seen:
            seen.add((x, y))
            yield (x, y)


def _within_tolerance(
    pt: tuple[int, int], known_coords: frozenset[tuple[int, int]], tolerance: int
) -> tuple[int, int] | None:
    for known in known_coords:
        if abs(pt[0] - known[0]) <= tolerance and abs(pt[1] - known[1]) <= tolerance:
            return known
    return None


#: Word tokens for split-name detection. Deliberately does *not* include `_` --
#: unlike `_TOKEN_RE` below, which tokenizes the identifier itself (where `_` is
#: part of an engine symbol's spelling), this tokenizes candidate *text*, where an
#: underscore is a separator standing in for a dropped space (`Lost_Person_Timmy`).
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: CamelCase / snake_case word boundary splitter, used to decompose an identifier
#: such as `LostPersonTimmy` or `extra_load` into `["lost", "person", "timmy"]` /
#: `["extra", "load"]` for the split-word check below.
_WORD_SPLIT_RE = re.compile(r"[A-Z][a-z0-9]*|[a-z0-9]+")


def _word_seq(identifier: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT_RE.findall(identifier)]


def _split_name_match(haystack_lower: str, identifier: str) -> bool:
    """True if `identifier`'s words appear as a consecutive sequence in the text,
    however they are separated (`Lost Person Timmy`, `Lost-Person-Timmy`,
    `Lost_Person_Timmy`).

    Single-word identifiers are skipped (nothing to split), and multi-word ones are
    checked as an ordered, consecutive sequence rather than "all words present
    somewhere" -- the latter would flag any text that happens to use "lost" and
    "person" in unrelated sentences. Checked against the live prompts/skills corpus
    with zero new false positives.
    """
    words = _word_seq(identifier)
    if len(words) < 2:
        return False
    tokens = _WORD_TOKEN_RE.findall(haystack_lower)
    n = len(words)
    return any(tokens[i : i + n] == words for i in range(len(tokens) - n + 1))


#: Between consecutive characters of an obfuscated identifier, tolerate at most one
#: stray `.`/`-`/`_` (e.g. `L.o.s.t`, `e-x-t-r-a_load`). Whitespace is deliberately
#: excluded: a space between every letter is what `_split_name_match` above is for,
#: and admitting it here would let this pattern bridge across unrelated words
#: ("...discovered fires..." contains "red" then "fire" one space apart) -- that
#: false positive was caught by probing before this shipped.
_LETTER_SEP = r"[.\-_]?"


def _letter_obfuscated(haystack_lower: str, identifier: str) -> bool:
    """True if `identifier` appears with a stray punctuation mark spliced between
    every letter (`extra_load` as `e.x.t.r.a._.l.o.a.d`).

    Only checked for identifiers of reasonable length, same rationale as
    `_near_token`'s length guard: short strings produce coincidental hits.
    """
    if len(identifier) < 5:
        return False
    pattern = _LETTER_SEP.join(re.escape(c) for c in identifier.lower())
    return re.search(pattern, haystack_lower) is not None


_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _near_token(haystack_lower: str, needle_lower: str, max_dist: int) -> str | None:
    """Find a token within `max_dist` edits of `needle_lower`.

    Guards against "change one letter of the person's name". Only tokens of
    similar length are compared, so this stays cheap.
    """
    if len(needle_lower) < 5:
        # Short names produce too many coincidental near-matches to be useful.
        return None
    for tok in set(_TOKEN_RE.findall(haystack_lower)):
        if tok == needle_lower:
            continue
        if abs(len(tok) - len(needle_lower)) > max_dist:
            continue
        if _edit_distance_within(tok, needle_lower, max_dist):
            return tok
    return None


def _edit_distance_within(a: str, b: str, limit: int) -> bool:
    """Levenshtein distance <= limit, with early exit."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            )
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit

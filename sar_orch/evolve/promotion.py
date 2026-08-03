"""Champion state and atomic promotion.

The previous loop rotated generations unconditionally: a candidate became the next
generation's base whether or not it was better, so a regression was inherited
rather than discarded. Promotion here happens only on an explicit gate pass, and
the write is atomic.

Why atomicity is not over-engineering: promotion copies a skill tree over the live
`sar_orch/skills/`. A crash midway leaves a mixture -- some files from the champion,
some from the candidate -- which is a configuration that was never evaluated and
belongs to no generation. Worse, it is silent: the next run picks it up and reports
a number attributed to a tree that never existed as a whole. So writes go to a
staging directory first and land with a single `os.replace`.

A failed candidate must leave `sar_orch/skills/` byte-identical. That is asserted,
not assumed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

CHAMPION_STATE_FILE = "champion.json"


@dataclass
class ChampionRecord:
    """What the current champion is, and what earned it that status."""

    generation: int
    skills_hash: str
    prompt_hash: str = ""
    promoted_at: str = ""
    #: Gate metrics at promotion time, so a later regression can be attributed.
    metrics: dict = field(default_factory=dict)
    #: Which skill file the winning candidate changed.
    changed_path: str = ""
    note: str = ""


def load_champion(state_dir: Path | str) -> ChampionRecord | None:
    p = Path(state_dir) / CHAMPION_STATE_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not read as "no champion yet" -- that would
        # silently restart evolution from the baseline and discard history.
        raise RuntimeError(
            f"champion state at {p} exists but could not be parsed; refusing to "
            "treat it as absent (that would silently restart from baseline)"
        )
    return ChampionRecord(**data)


def save_champion(state_dir: Path | str, record: ChampionRecord) -> Path:
    """Write champion state atomically."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / CHAMPION_STATE_FILE
    fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(record), fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def tree_hash(skills_dir: Path | str) -> str:
    """Content hash of a skill tree.

    Sorted by relative path, and the path is hashed alongside the bytes: the same
    content at a different path is a different configuration. Explicit sorting
    rather than `rglob` order, which is unspecified and differs across
    filesystems -- otherwise the same tree hashes differently per machine.
    """
    import hashlib

    skills_dir = Path(skills_dir)
    h = hashlib.sha256()
    for path in sorted(skills_dir.rglob("*.md"), key=lambda p: p.relative_to(skills_dir).as_posix()):
        rel = path.relative_to(skills_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def promote(
    *,
    candidate_dir: Path | str,
    live_dir: Path | str,
    state_dir: Path | str,
    generation: int,
    metrics: dict | None = None,
    changed_path: str = "",
    note: str = "",
    timestamp: str,
    archive_dir: Path | str | None = None,
) -> ChampionRecord:
    """Replace the live skill tree with the candidate, atomically.

    `timestamp` is passed in rather than read from the clock so the caller controls
    it and the operation stays reproducible in tests.

    Sequence: stage a copy next to the target, archive the outgoing champion, swap
    directories with `os.replace`, then record state. The swap is the only step that
    mutates what a running experiment would read, and it is atomic at the directory
    level.
    """
    candidate_dir = Path(candidate_dir)
    live_dir = Path(live_dir)
    if not candidate_dir.is_dir():
        raise FileNotFoundError(f"candidate dir not found: {candidate_dir}")

    parent = live_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=".promote_staging_"))
    staged_tree = staging / live_dir.name
    shutil.copytree(candidate_dir, staged_tree)

    if archive_dir is not None and live_dir.is_dir():
        archive_dir = Path(archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(live_dir, archive_dir / f"gen{generation - 1:03d}", dirs_exist_ok=True)

    old_holding = None
    try:
        if live_dir.is_dir():
            old_holding = staging / "_outgoing"
            os.replace(live_dir, old_holding)
        os.replace(staged_tree, live_dir)
    except BaseException:
        # Put the original back if the swap failed halfway.
        if old_holding is not None and old_holding.is_dir() and not live_dir.exists():
            os.replace(old_holding, live_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise

    shutil.rmtree(staging, ignore_errors=True)

    record = ChampionRecord(
        generation=generation,
        skills_hash=tree_hash(live_dir),
        promoted_at=timestamp,
        metrics=metrics or {},
        changed_path=changed_path,
        note=note,
    )
    save_champion(state_dir, record)
    return record


def git_commit_promotion(
    *,
    paths: list[str],
    message: str,
    enabled: bool = False,
    repo_root: Path | str = ".",
) -> str | None:
    """Optionally commit a promotion. Off by default.

    Default-off because an evolution loop that commits on its own accumulates
    history nobody reviewed, and `git add -A` in that loop would sweep up whatever
    else happened to be in the tree. So: explicit opt-in, and only the paths that
    were actually promoted -- never `-A`, never `.`.

    Returns the commit sha, or None when disabled or when there was nothing to
    commit.
    """
    if not enabled:
        return None
    repo_root = str(repo_root)
    add = subprocess.run(
        ["git", "add", "--"] + paths, cwd=repo_root, capture_output=True, text=True
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--"] + paths,
        cwd=repo_root,
        capture_output=True,
    )
    if staged.returncode == 0:
        return None  # nothing staged -> nothing to commit
    commit = subprocess.run(
        ["git", "commit", "-m", message, "--"] + paths,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return sha.stdout.strip() or None

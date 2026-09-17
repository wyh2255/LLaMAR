"""Pull a release's tree back into the LLaMAR checkout, one mapped file at a time.

The reverse of an episode's overlay: ``GET /reef/harness`` serves the head
release of a scenario (``--release-id`` any catalog release), and every file in
it is written back to the checkout path an episode would have read it from.
Only the entries the tree actually carries are touched - never a directory
sweep, never a ``.bak`` file, never a path outside ``sar_orch/prompts/``.

Mapping (the exact inverse of ``runner.apply_overlay``):

======================  ==========================================
rendered tree file      checkout file
======================  ==========================================
``overlay/prompts/coordinator/system.semantic.md``
                        ``sar_orch/prompts/coordinator/system.semantic.md``
``overlay/prompts/worker/system.md``
                        ``sar_orch/prompts/worker/system.md``
``overlay/rules.md``    ``sar_orch/prompts/coordinator/rules.md`` (a new file:
                        a person adopts or deletes it - the runner appends this
                        text to the coordinator prompt at episode time)
``overlay/sar_config.json``
                        none: reported only. Config reaches an episode through
                        the rendered tree, never through a checkout file.
======================  ==========================================

Defaults are safe: the script **only reports** (``--dry-run`` is the default),
and ``--apply`` is required to write anything. ``--apply`` ends by showing the
main repo's ``git diff`` for the files it touched, so the change lands as a diff
a person reviews and commits - git stays the baseline's source of truth.

Usage::

    python -m reef_sar_adapter.pull_release                        # dry run, head
    python -m reef_sar_adapter.pull_release --apply                # write + git diff
    python -m reef_sar_adapter.pull_release --scenario sar-smoke --release-id <id>
    python -m reef_sar_adapter.pull_release --repo /path/to/LLaMAR
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .runner import RULES_PLACEHOLDER

__all__ = [
    "DEFAULT_REPO",
    "DEFAULT_SCENARIO",
    "REPORT_ONLY",
    "STACK_FILE",
    "TARGETS",
    "Plan",
    "apply_plan",
    "build_plan",
    "fetch_manifest",
    "git_diff",
    "main",
    "render_plan",
]

#: The LLaMAR checkout the pulled files land in (the campaign's main repo).
DEFAULT_REPO = Path("/home/wyh/daily_work/LLaMAR")
#: The scenario the smoke campaign created; override with ``--scenario``.
DEFAULT_SCENARIO = "sar-smoke"
#: The deployment stack the service defaults (URL/token) are read from.
STACK_FILE = Path(__file__).resolve().with_name("stack.yaml")

#: Rendered tree path -> the checkout file it comes from, and the prefixes a
#: target may live under. A path outside them is refused, not guessed at.
TARGETS: Mapping[str, str] = {
    "overlay/prompts/coordinator/system.semantic.md": "sar_orch/prompts/coordinator/system.semantic.md",
    "overlay/prompts/worker/system.md": "sar_orch/prompts/worker/system.md",
    "overlay/rules.md": "sar_orch/prompts/coordinator/rules.md",
}
WRITABLE_PREFIX = "sar_orch/prompts/"
#: Tree files the pull reports but never writes: the config is rendered by reef
#: into each episode, so there is no checkout file for it.
REPORT_ONLY: Mapping[str, str] = {
    "overlay/sar_config.json": "reported only: the episode's config is rendered from the tree, not read from the checkout",
}

_MODES = ("dry-run", "apply")


@dataclass(frozen=True)
class Plan:
    """One tree entry's fate: written, unchanged, skipped, or reported only."""

    path: str
    target: Path | None
    action: str
    content: str
    current: str | None = None
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.target is not None and self.current != self.content


def fetch_manifest(base_url: str, token: str, scenario: str, release_id: str | None = None) -> dict[str, Any]:
    """``GET /reef/harness``: the served tree of ``scenario`` (head, or one release)."""
    url = f"{base_url.rstrip('/')}/reef/harness"
    if release_id:
        url += f"?release_id={release_id}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "x-reef-scenario": scenario})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"pull_release: {url} answered {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"pull_release: cannot reach {url}: {exc}") from exc


def build_plan(manifest: Mapping[str, Any], repo: Path) -> list[Plan]:
    """Every file the release carries, mapped to its checkout target."""
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise SystemExit("pull_release: the manifest carries no 'files' object")
    plan: list[Plan] = []
    for path in sorted(files):
        content = files[path]
        if not isinstance(content, str):
            raise SystemExit(f"pull_release: tree file {path!r} is not text")
        if path in REPORT_ONLY:
            plan.append(Plan(path, None, "report-only", content, note=REPORT_ONLY[path]))
            continue
        relative = TARGETS.get(path)
        if relative is None:
            plan.append(Plan(path, None, "skip", content, note="no checkout target; ignored"))
            continue
        if not relative.startswith(WRITABLE_PREFIX) or relative.endswith(".bak"):
            # Belt and braces: the table above is the only source of targets.
            raise SystemExit(f"pull_release: refusing to write {relative!r} (outside {WRITABLE_PREFIX})")
        target = repo / relative
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if path == "overlay/rules.md" and content.strip() == RULES_PLACEHOLDER:
            plan.append(
                Plan(path, target, "skip", content, current, note="baseline placeholder; nothing to adopt yet")
            )
        elif current == content:
            plan.append(Plan(path, target, "unchanged", content, current))
        else:
            plan.append(Plan(path, target, "write", content, current))
    return plan


def render_plan(plan: Sequence[Plan], manifest: Mapping[str, Any], repo: Path) -> str:
    """The human-readable report: the release, each entry's fate, and text diffs."""
    lines = [
        f"release_id: {manifest.get('release_id')}  parent: {manifest.get('parent_release_id')}",
        f"gate: {manifest.get('gate')}",
        f"repo: {repo}",
        "",
    ]
    for item in plan:
        marker = {
            "write": "WRITE ",
            "unchanged": "SAME  ",
            "skip": "SKIP  ",
            "report-only": "REPORT",
        }[item.action]
        location = item.target if item.target is not None else "-"
        lines.append(f"{marker} {item.path} -> {location}{f'  ({item.note})' if item.note else ''}")
    for item in plan:
        if item.action == "report-only":
            lines += ["", f"--- {item.path} (not written back) ---", item.content.rstrip("\n")]
        elif item.action == "write":
            before = "".join(f"{line}\n" for line in (item.current or "").split("\n")) if item.current else ""
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                item.content.splitlines(keepends=True),
                fromfile=f"a/{item.target}",
                tofile=f"b/{item.target}",
            )
            lines += ["", *("".join(diff).rstrip("\n").splitlines())]
    return "\n".join(lines)


def apply_plan(plan: Sequence[Plan]) -> list[str]:
    """Write the changed targets; returns the repo-relative paths written."""
    written: list[str] = []
    for item in plan:
        if item.action != "write" or item.target is None:
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_text(item.content, encoding="utf-8")
        written.append(str(item.target))
    return written


def git_diff(repo: Path, paths: Sequence[Path]) -> str:
    """The checkout's diff for ``paths``, tracked files first, new files after.

    Read-only: nothing is staged. An untracked new file shows through
    ``git diff --no-index /dev/null <file>`` because plain ``git diff`` does
    not know it yet.
    """
    relative = [str(path.relative_to(repo)) for path in paths]
    blocks: list[str] = []
    if not relative:
        return "(nothing written)"
    tracked = subprocess.run(
        ["git", "-C", str(repo), "diff", "--", *relative], capture_output=True, text=True, check=False
    )
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--short", "--", *relative], capture_output=True, text=True, check=False
    )
    blocks.append(status.stdout.rstrip("\n") or "(no status change)")
    blocks.append(tracked.stdout.rstrip("\n") or "(no tracked-file diff)")
    for path in paths:
        if path.exists() and path.name and not _is_tracked(repo, path):
            new_file = subprocess.run(
                ["git", "-C", str(repo), "diff", "--no-index", "--", "/dev/null", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            blocks.append(new_file.stdout.rstrip("\n") or f"(untracked, no diff shown: {path})")
    return "\n".join(blocks)


def _is_tracked(repo: Path, path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", str(path.relative_to(repo))],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _service_defaults() -> tuple[str, str]:
    """The base URL and token the deployment stack declares."""
    config = yaml.safe_load(STACK_FILE.read_text(encoding="utf-8")) or {}
    reef = config.get("reef") or {}
    host = str(reef.get("host") or "127.0.0.1")
    port = reef.get("port") or 8900
    token = str(reef.get("token") or "")
    return f"http://{host}:{port}", token


def main(argv: Sequence[str] | None = None) -> int:
    base_url, token = _service_defaults()
    parser = argparse.ArgumentParser(prog="python -m reef_sar_adapter.pull_release", description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="the LLaMAR checkout to write into")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, help="the reef scenario to pull from")
    parser.add_argument("--release-id", default=None, help="a catalog release instead of the serving head")
    parser.add_argument("--url", default=base_url, help="the reef service base URL")
    parser.add_argument("--token", default=token, help="the reef service bearer token")
    parser.add_argument("--apply", action="store_true", help="write the files (default: report only)")
    args = parser.parse_args(argv)

    repo = args.repo.expanduser().resolve()
    if not (repo / "sar_orch" / "prompts").is_dir():
        raise SystemExit(f"pull_release: {repo} carries no sar_orch/prompts; pass --repo")
    manifest = fetch_manifest(args.url, args.token, args.scenario, args.release_id)
    plan = build_plan(manifest, repo)
    print(render_plan(plan, manifest, repo))

    writes = [item for item in plan if item.action == "write"]
    print()
    print(f"{len(writes)} file(s) to write, {len(plan) - len(writes)} unchanged/skipped/reported")
    if not args.apply:
        print("dry run: nothing written (pass --apply to write)")
        return 0
    if not writes:
        print("nothing to write")
        return 0
    written = apply_plan(plan)
    for path in written:
        print(f"wrote {path}")
    print()
    print("--- git diff (review, then commit by hand) ---")
    print(git_diff(repo, [Path(path) for path in written]))
    return 0


if __name__ == "__main__":  # pragma: no cover - module invocation
    sys.exit(main())

"""Repo-root prompt loader (reef × sar_orch 外置化, workflow §B2).

Out-of-line prompt files (``sar_orch/prompts/**/system.md``) are consumed by
call sites in both ``sar_orch`` and ``src/a2a``; the helper lives in the
``a2a`` package so the ``src/a2a`` side never has to import ``sar_orch``.

Contract:

- paths are repository-relative (``sar_orch/prompts/<domain>/system.md``) and
  resolve against the repo root, found by walking up from this file until a
  directory containing ``pyproject.toml`` is reached;
- loading is **fail-closed**: a missing root, a missing/unreadable file, a
  file that is not utf-8, or an empty (whitespace-only) prompt raises
  :class:`PromptLoadError` — there is never a silent fallback to an inline
  copy;
- the file is returned verbatim (utf-8, no stripping) so an externalized
  prompt can be byte-compared against the inline string it replaced
  (workflow §B3 sha invariant).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PromptLoadError", "load_repo_prompt"]

#: Anchor file that identifies the repository root while walking upwards.
_ROOT_MARKER = "pyproject.toml"


class PromptLoadError(RuntimeError):
    """Raised when a repo prompt cannot be located or read (fail-closed)."""


def _find_repo_root(start: Path) -> Path:
    """Return the closest ancestor of ``start`` (inclusive) holding the root marker."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / _ROOT_MARKER).is_file():
            return candidate
    raise PromptLoadError(
        f"repository root not found: no {_ROOT_MARKER} in {current} or any parent"
    )


def _read_prompt_file(path: Path) -> str:
    """Read one prompt file, rejecting missing / unreadable / empty content."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PromptLoadError(f"prompt file unreadable: {path} ({exc})") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptLoadError(
            f"prompt file is not valid utf-8: {path} ({exc})"
        ) from exc
    if not text.strip():
        raise PromptLoadError(f"prompt file is empty: {path}")
    return text


def load_repo_prompt(rel_path: str) -> str:
    """Load ``rel_path`` verbatim from the repo root (fail-closed).

    ``rel_path`` is a repository-relative POSIX path such as
    ``"sar_orch/prompts/diagnosis/system.md"``.  Raises
    :class:`PromptLoadError` for an absolute path, a missing root marker, a
    missing/unreadable/empty file — callers must not fall back to an inline
    prompt.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise PromptLoadError(f"rel_path must be a non-empty str, got {rel_path!r}")
    relative = Path(rel_path)
    if relative.is_absolute():
        raise PromptLoadError(
            f"rel_path must be repository-relative, got absolute path: {rel_path}"
        )
    root = _find_repo_root(Path(__file__).resolve().parent)
    return _read_prompt_file(root / relative)

"""Runtime overlay for SAR tool descriptions (reef × sar_orch §5.2).

``sar_orch/tools/descriptions.json`` carries the tool LLM-facing text
(``description`` + the ``parameters`` JSON schema) for every tool class under
``sar_orch/tools/{worker,coordinator}/``.  It is an *overlay*, not a source of
truth: the code keeps its inline values and the JSON only replaces the fields
it actually provides.

Contract (deliberately the opposite of the fail-closed prompt loader):

- **fail-soft / backward compatible** — a missing file, an unreadable file, a
  JSON syntax error, a non-object document, or a missing key silently falls
  back to the inline code values.  Default behaviour is byte-for-byte the
  historical behaviour when the file is absent; tool descriptions are an
  evolution target, never a reason to stop a run;
- **partial application** — an entry may carry only ``description`` or only
  ``parameters``; fields that are absent (or invalid: non-string / blank
  description, non-dict parameters) keep the inline value;
- **key resolution** — ``<tool_name>`` when the name is unique across the two
  SAR tool scopes, and ``<scope>/<tool_name>`` when both scopes define the
  same name (today: ``finish_task``).  Lookup tries the scope-qualified key
  first, then the bare name; see ``DESCRIPTIONS_OVERLAY.md``;
- the file is located through the shared repo-root helper
  (:func:`a2a.utils.prompt_loader.load_repo_prompt`), so a reef episode that
  renders the mutated file into its materialised repo picks it up unchanged.

Application sites: the SAR tool *packages* apply the overlay to their classes at
import time (``sar_orch/tools/worker/__init__.py`` right after ``SAR_WORKER_TOOLS``
is built, ``sar_orch/tools/coordinator/__init__.py`` for its eight classes) — so
every import path (the registered tool lists, the diagnosis loop's direct
submodule imports, tests) sees the overlaid values, and a failure to load simply
leaves the code defaults in place.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable
from typing import Any

from a2a.utils.prompt_loader import PromptLoadError, load_repo_prompt

__all__ = [
    "DESCRIPTIONS_REL_PATH",
    "TOOL_SCOPES",
    "apply_description_overlay",
    "apply_description_overlays",
    "load_descriptions_overlay",
    "reset_descriptions_cache",
    "tool_scope",
]

logger = logging.getLogger(__name__)

#: Repository-relative path of the overlay file.
DESCRIPTIONS_REL_PATH = "sar_orch/tools/descriptions.json"

#: SAR tool sub-packages; also the scope prefixes accepted in overlay keys.
TOOL_SCOPES = ("worker", "coordinator")

# path -> parsed overlay (empty dict when unavailable).  The overlay file is a
# static per-run artefact, so one read per process is enough.
_CACHE: dict[str, dict[str, Any]] = {}


def reset_descriptions_cache() -> None:
    """Drop the per-process cache (tests / long-lived processes)."""
    _CACHE.clear()


def tool_scope(tool_cls: type) -> str | None:
    """Return ``"worker"`` / ``"coordinator"`` for a SAR tool class, else None."""
    module = getattr(tool_cls, "__module__", "") or ""
    parts = module.split(".")
    if (
        len(parts) >= 3
        and parts[0] == "sar_orch"
        and parts[1] == "tools"
        and parts[2] in TOOL_SCOPES
    ):
        return parts[2]
    return None


def load_descriptions_overlay(rel_path: str = DESCRIPTIONS_REL_PATH) -> dict[str, Any]:
    """Return the parsed overlay document, or ``{}`` when it is unusable.

    Never raises: any problem (missing file, bad UTF-8, JSON error, non-object
    root) degrades to the empty overlay, i.e. "keep the inline code values".
    """
    if rel_path in _CACHE:
        return _CACHE[rel_path]

    overlay: dict[str, Any] = {}
    try:
        raw = load_repo_prompt(rel_path)
    except PromptLoadError as exc:
        logger.debug("tool descriptions overlay not loaded (%s): %s", rel_path, exc)
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("tool descriptions overlay is not valid JSON (%s): %s", rel_path, exc)
        else:
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, dict):
                        overlay[key] = value
                    else:
                        logger.warning(
                            "tool descriptions overlay entry %r is not an object, ignored (%s)",
                            key,
                            rel_path,
                        )
            else:
                logger.warning(
                    "tool descriptions overlay root is not an object, ignored (%s)", rel_path
                )

    _CACHE[rel_path] = overlay
    return overlay


def _entry_for(tool_cls: type, overlay: dict[str, Any], scope: str | None) -> dict[str, Any] | None:
    """Resolve the overlay entry for ``tool_cls`` (scope-qualified first)."""
    name = getattr(tool_cls, "name", None)
    if not isinstance(name, str) or not name:
        return None
    if scope:
        entry = overlay.get(f"{scope}/{name}")
        if isinstance(entry, dict):
            return entry
    entry = overlay.get(name)
    return entry if isinstance(entry, dict) else None


def entry_name(tool_cls: type, scope: str | None = None) -> str:
    """Human-readable identifier of a tool class for log messages."""
    name = getattr(tool_cls, "name", None)
    name = name if isinstance(name, str) and name else getattr(tool_cls, "__name__", "?")
    if scope is None:
        scope = tool_scope(tool_cls)
    return f"{scope}/{name}" if scope else str(name)


def apply_description_overlay(
    tool_cls: type,
    *,
    scope: str | None = None,
    overlay: dict[str, Any] | None = None,
    rel_path: str = DESCRIPTIONS_REL_PATH,
) -> bool:
    """Apply the overlay entry of ``tool_cls`` in place; return True if patched.

    ``scope`` defaults to the scope derived from the class module (see
    :func:`tool_scope`); ``overlay`` overrides the loaded document (tests).
    Only present, well-formed fields are applied — anything else keeps the
    inline class attribute.
    """
    if overlay is None:
        overlay = load_descriptions_overlay(rel_path)
    if not overlay:
        return False
    if scope is None:
        scope = tool_scope(tool_cls)
    entry = _entry_for(tool_cls, overlay, scope)
    if entry is None:
        return False

    applied = False
    if "description" in entry:
        description = entry["description"]
        if isinstance(description, str) and description.strip():
            tool_cls.description = description  # type: ignore[attr-defined]
            applied = True
        else:
            logger.warning(
                "tool descriptions overlay: %r.description is not a non-blank string, ignored",
                entry_name(tool_cls, scope),
            )
    if "parameters" in entry:
        parameters = entry["parameters"]
        if isinstance(parameters, dict):
            tool_cls.parameters = copy.deepcopy(parameters)  # type: ignore[attr-defined]
            applied = True
        else:
            logger.warning(
                "tool descriptions overlay: %r.parameters is not an object, ignored",
                entry_name(tool_cls, scope),
            )
    return applied


def apply_description_overlays(
    tool_classes: Iterable[type],
    *,
    overlay: dict[str, Any] | None = None,
    rel_path: str = DESCRIPTIONS_REL_PATH,
) -> int:
    """Apply the overlay to several tool classes; return how many were patched."""
    if overlay is None:
        overlay = load_descriptions_overlay(rel_path)
    applied = 0
    for tool_cls in tool_classes:
        if apply_description_overlay(tool_cls, overlay=overlay, rel_path=rel_path):
            applied += 1
    return applied

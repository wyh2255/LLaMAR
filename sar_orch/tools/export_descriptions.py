"""Offline exporter / verifier for the SAR tool-descriptions overlay (reef §5.2).

The overlay file ``sar_orch/tools/descriptions.json`` is the second reef
``config`` target: it lets the evolution loop mutate *what the LLM sees* about
each SAR tool (``description`` + JSON-schema ``parameters``) without touching
code.  This module is the offline counterpart that produced the first version
of that file and keeps it checkable later.

Design:

- values are extracted from **source** (``ast``), never by importing the tool
  classes — so the export is immune to whatever state the runtime overlay
  happens to be in when this script runs, and the script stays import-light;
- keys are the tool ``name`` when that name is unique across the two SAR tool
  scopes (``worker`` / ``coordinator``), and ``<scope>/<name>`` when the same
  name is defined by classes in both scopes (today: ``finish_task``, whose
  worker and coordinator variants differ).  See
  ``sar_orch/tools/DESCRIPTIONS_OVERLAY.md``;
- ``--verify`` re-derives the expected overlay from source and byte-compares it
  against the file on disk — this is the "overlay == inline code values"
  invariant check used at externalization time (and re-runnable by reviewers).

Usage (offline, any cwd)::

    python sar_orch/tools/export_descriptions.py --check
    python sar_orch/tools/export_descriptions.py --write
    python sar_orch/tools/export_descriptions.py --verify
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DESCRIPTIONS_PATH",
    "PACKAGE_ROOT",
    "TOOL_SCOPES",
    "ToolSpec",
    "build_overlay",
    "iter_tool_specs",
    "overlay_key",
    "render_overlay",
]

#: Directory holding this module (``sar_orch/tools``) — the overlay's home.
PACKAGE_ROOT = Path(__file__).resolve().parent

#: Sub-packages that define SAR tools, in scan order.
TOOL_SCOPES = ("worker", "coordinator")

#: Overlay file, next to this module.
DESCRIPTIONS_PATH = PACKAGE_ROOT / "descriptions.json"


@dataclass(frozen=True)
class ToolSpec:
    """One tool class as written in source."""

    scope: str
    module: str
    class_name: str
    name: str
    description: str
    parameters: dict[str, Any]

    @property
    def key(self) -> str:
        return overlay_key(self.name, scope=self.scope)


def overlay_key(name: str, *, scope: str, ambiguous: bool = False) -> str:
    """Return the overlay key for a tool name in a scope.

    ``ambiguous`` marks names defined by more than one scope (see module
    docstring): those are addressed as ``<scope>/<name>``.
    """
    return f"{scope}/{name}" if ambiguous else name


def _is_tool_base(base: ast.expr) -> bool:
    """True when a class base refers to ``Tool`` (bare or dotted)."""
    if isinstance(base, ast.Name):
        return base.id == "Tool"
    if isinstance(base, ast.Attribute):
        return base.attr == "Tool"
    return False


def _assignment_value(cls: ast.ClassDef, field: str) -> Any | None:
    """Return the literal value assigned to ``field`` in the class body."""
    for node in cls.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if len(targets) == 1 and targets[0].id == field:
                return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == field:
                if node.value is None:
                    return None
                return ast.literal_eval(node.value)
    return None


def _iter_module_specs(path: Path, scope: str) -> list[ToolSpec]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = f"sar_orch.tools.{scope}.{path.stem}"
    specs: list[ToolSpec] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_is_tool_base(base) for base in node.bases):
            continue
        name = _assignment_value(node, "name")
        description = _assignment_value(node, "description")
        parameters = _assignment_value(node, "parameters")
        if name is None or description is None or parameters is None:
            raise ValueError(
                f"{module}:{node.lineno} class {node.name} must define literal "
                f"'name' / 'description' / 'parameters' class attributes "
                f"(got name={name!r}, description={'<set>' if description is not None else None}, "
                f"parameters={'<set>' if parameters is not None else None})"
            )
        if not isinstance(name, str) or not isinstance(description, str):
            raise TypeError(f"{module}:{node.lineno} class {node.name}: non-str name/description")
        if not isinstance(parameters, dict):
            raise TypeError(f"{module}:{node.lineno} class {node.name}: parameters is not a dict")
        specs.append(
            ToolSpec(
                scope=scope,
                module=module,
                class_name=node.name,
                name=name,
                description=description,
                parameters=parameters,
            )
        )
    return specs


def iter_tool_specs(package_root: Path | None = None) -> list[ToolSpec]:
    """Scan the SAR tool packages and return every tool class, sorted."""
    root = PACKAGE_ROOT if package_root is None else Path(package_root)
    specs: list[ToolSpec] = []
    for scope in TOOL_SCOPES:
        scope_dir = root / scope
        if not scope_dir.is_dir():
            raise FileNotFoundError(f"tool scope directory missing: {scope_dir}")
        for path in sorted(scope_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            specs.extend(_iter_module_specs(path, scope))
    if not specs:
        raise ValueError(f"no tool classes found under {root}")
    return specs


def build_overlay(specs: list[ToolSpec]) -> dict[str, dict[str, Any]]:
    """Build the overlay mapping from specs (keys sorted for stable diffs)."""
    name_counts = Counter(spec.name for spec in specs)
    overlay: dict[str, dict[str, Any]] = {}
    for spec in specs:
        key = overlay_key(spec.name, scope=spec.scope, ambiguous=name_counts[spec.name] > 1)
        if key in overlay:
            raise ValueError(
                f"overlay key collision: {key!r} claimed by more than one tool "
                f"({spec.module}.{spec.class_name})"
            )
        overlay[key] = {
            "description": spec.description,
            "parameters": spec.parameters,
        }
    return {key: overlay[key] for key in sorted(overlay)}


def render_overlay(overlay: dict[str, dict[str, Any]]) -> str:
    """Serialise the overlay exactly as it is stored in the repo."""
    return json.dumps(overlay, indent=2, ensure_ascii=False) + "\n"


def _print_discovery(specs: list[ToolSpec]) -> None:
    name_counts = Counter(spec.name for spec in specs)
    print(f"discovered {len(specs)} tool classes in {len(TOOL_SCOPES)} scopes:")
    for spec in specs:
        key = overlay_key(spec.name, scope=spec.scope, ambiguous=name_counts[spec.name] > 1)
        marker = " (scope-qualified: name shared across scopes)" if key != spec.name else ""
        print(f"  {spec.scope:11s} {spec.module.split('.')[-1]:26s} "
              f"{spec.class_name:26s} -> {key}{marker}")
    print(f"total tool classes: {len(specs)}; distinct names: {len(name_counts)}; "
          f"overlay keys: {len(build_overlay(specs))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export / verify sar_orch/tools/descriptions.json against source inline values."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="扫描并打印工具清单/计数")
    mode.add_argument("--write", action="store_true", help="由源码内联值生成 descriptions.json")
    mode.add_argument("--verify", action="store_true", help="核对文件与源码内联值逐字段相等")
    args = parser.parse_args(argv)

    specs = iter_tool_specs()
    expected = build_overlay(specs)

    if args.check:
        _print_discovery(specs)
        return 0

    if args.write:
        _print_discovery(specs)
        DESCRIPTIONS_PATH.write_text(render_overlay(expected), encoding="utf-8")
        print(f"wrote {DESCRIPTIONS_PATH} ({len(expected)} entries)")
        return 0

    # --verify
    if not DESCRIPTIONS_PATH.is_file():
        print(f"MISSING: {DESCRIPTIONS_PATH}", file=sys.stderr)
        return 1
    actual = json.loads(DESCRIPTIONS_PATH.read_text(encoding="utf-8"))
    if actual == expected:
        print(f"OK: {DESCRIPTIONS_PATH} matches source inline values "
              f"({len(actual)} entries)")
        return 0
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key for key in set(expected) & set(actual) if expected[key] != actual[key]
    )
    print("MISMATCH between descriptions.json and source inline values:", file=sys.stderr)
    for key in missing:
        print(f"  missing key: {key}", file=sys.stderr)
    for key in extra:
        print(f"  unexpected key: {key}", file=sys.stderr)
    for key in changed:
        for field in ("description", "parameters"):
            if expected[key].get(field) != actual[key].get(field):
                print(f"  {key}.{field} differs", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

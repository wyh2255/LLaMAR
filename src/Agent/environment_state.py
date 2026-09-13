"""Generic Environment State query/view contracts shared by all agents.

This module is intentionally backend-independent: it must not import ``a2a``
or ``sar_orch``.  It defines the canonical ``Freshness`` enum (Phase 0) and the
minimal read-only query/view DTOs used by the EnvironmentStateProvider path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Freshness(str, Enum):
    """Freshness of an Environment State projection.

    Only three values are allowed.  A projection that cannot be built safely is
    never silently reused: it must be marked ``STALE`` (with a reason and source
    revision) or ``UNAVAILABLE`` (no safe projection / read error).
    """

    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


@runtime_checkable
class EnvironmentStateProvider(Protocol):
    """Generic read-path provider protocol (design §6).

    Context depends on this protocol (and the query/view DTOs), never on a
    concrete SAR backend.  Implementations combine a ``MemoryReadPort`` and a
    ``ControlPlaneReadPort`` and apply viewer ACL inside the provider.
    """

    scope_id: str
    viewer_role: str
    viewer_id: str
    current_dispatch_id: str | None

    def query_environment_state(
        self, query: EnvironmentStateQuery
    ) -> EnvironmentStateView: ...


def build_environment_state_query(
    *,
    scope_id: str,
    viewer_role: str = "coordinator",
    viewer_id: str = "",
    current_dispatch_id: str | None = None,
    temporal_cursor: int = 0,
    token_budget: int = 0,
    required_sections: tuple[str, ...] = (),
) -> EnvironmentStateQuery:
    """Construct a validated ``EnvironmentStateQuery``.

    ``viewer_role`` / ``viewer_id`` must come from an authenticated principal,
    never from an LLM or HTTP request claim.
    """
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        current_dispatch_id=current_dispatch_id,
        temporal_cursor=temporal_cursor,
        token_budget=token_budget,
        required_sections=required_sections,
    )


@dataclass(frozen=True)
class EnvironmentStateQuery:
    """Read-only inputs for constructing an Environment State view.

    ``viewer_role`` / ``viewer_id`` are derived from an authenticated principal;
    they are never trusted from an LLM or HTTP request.
    """

    scope_id: str
    viewer_role: str = "coordinator"
    viewer_id: str = ""
    current_dispatch_id: str | None = None
    temporal_cursor: int = 0
    token_budget: int = 0
    required_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentStateView:
    """Immutable Environment State view (a rebuildable read, not truth)."""

    freshness: Freshness
    reason: str = ""
    source_revision: int | None = None
    sections: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def fresh(cls, source_revision: int | None = None) -> EnvironmentStateView:
        return cls(Freshness.FRESH, source_revision=source_revision)

    @classmethod
    def stale(
        cls, reason: str, source_revision: int | None = None
    ) -> EnvironmentStateView:
        return cls(Freshness.STALE, reason=reason, source_revision=source_revision)

    @classmethod
    def unavailable(cls, reason: str) -> EnvironmentStateView:
        return cls(Freshness.UNAVAILABLE, reason=reason)


#: Reserved metadata keys carried in ``EnvironmentStateView.sections``.
NEXT_CURSOR_KEY = "next_cursor"
TRUNCATED_KEY = "truncated"

FRESHNESS_SECTION = "freshness"

#: Section headings the pure renderer emits.  This is rendering only — ACL
#: filtering happens in the provider, never here (design §6).
_SECTION_HEADINGS = {
    "spatial_state": "### Spatial State",
    "embodied_state": "### Embodied State",
    # Phase 4 (P4): coordinator-only system-health section (agentic
    # diagnosis, §3.3).  Registered here so the pure renderer can format
    # it; ACL + injection gating is decided by the provider, never by the
    # renderer.
    "system_health": "### System Health",
    # Phase 5 #1/#2: coordinator-only long-term memory section (published
    # summary).  Registered here so the pure renderer can format it; ACL
    # (who may see it) is decided by the provider, never by the renderer.
    "long_term_memory": "### Long-term Memory",
    "relevant_events": "### Relevant Recent Events",
    "task_execution_state": "### Task Execution State",
    FRESHNESS_SECTION: "### Freshness / Conflicts / Evidence",
}


def _format_field_rows(entry: dict[str, Any]) -> str:
    fields = entry.get("fields", {})
    if not fields:
        return ""
    lines: list[str] = []
    for name, row in sorted(fields.items()):
        value = row.get("value")
        if value is None:
            continue
        detail = f"{name}: {value}"
        outcome = row.get("outcome")
        if outcome and outcome != "material":
            detail += f" [{outcome}]"
        if row.get("conflict_candidates"):
            detail += " (conflicted)"
        lines.append(f"  - {detail}")
    return "\n".join(lines)


def _format_entity_dict(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for entity_id, entry in sorted(payload.items()):
        entity_type = entry.get("entity_type", "entity")
        lines.append(f"- {entity_id} ({entity_type})")
        fields = _format_field_rows(entry)
        if fields:
            lines.append(fields)
    return "\n".join(lines)


def _format_event_or_task_list(name: str, payload: list[dict[str, Any]]) -> str:
    if name == "task_execution_state":
        lines = []
        for t in payload:
            lines.append(
                f"- {t['dispatch_id']} -> {t['worker_id']} [{t['state']}] "
                f"worker_task={t['worker_task_id']} control_rev={t['control_revision']}"
            )
        return "\n".join(lines)
    lines = []
    for evt in payload:
        lines.append(
            f"- seq={evt.get('sequence')} {evt.get('event_type', '')} "
            f"actor={evt.get('actor_id', '')} dispatch={evt.get('dispatch_id') or ''}"
        )
    return "\n".join(lines)


def _format_long_term_memory(payload: dict[str, Any]) -> str:
    """Format published long-term memory entries (Phase 5 #1/#2).

    Section shape is ``{memory_key: {kind, statement, confidence,
    created_at, ...}}``.  A dedicated format is used (not the
    relevant_events sequence formatter): one stable line per memory_key,
    sorted by key.  Entries without a statement are skipped.
    """
    lines: list[str] = []
    for memory_key in sorted(payload):
        entry = payload[memory_key]
        if not isinstance(entry, dict):
            continue
        statement = entry.get("statement")
        if not statement:
            continue
        detail = f"- {memory_key} [{entry.get('kind', '')}]"
        confidence = entry.get("confidence")
        if confidence is not None:
            detail += f" (confidence={confidence})"
        detail += f": {statement}"
        lines.append(detail)
    return "\n".join(lines)


def _format_system_health(payload: dict[str, Any]) -> str:
    """Format system-health diagnoses (P4, main plan §3.3).

    Section shape is ``{target: {finding, suggestion, confidence}}`` —
    one line per diagnosis: ``target: finding → suggestion
    (confidence=N)``, sorted by target for a stable render.  Entries
    without a finding or suggestion are skipped.  A dedicated formatter is
    used (not the long-term / entity formatters): the line IS the
    diagnosis, no extra wrapping.
    """
    lines: list[str] = []
    for target in sorted(payload):
        entry = payload[target]
        if not isinstance(entry, dict):
            continue
        finding = entry.get("finding")
        suggestion = entry.get("suggestion")
        if not finding or not suggestion:
            continue
        detail = f"{target}: {finding} → {suggestion}"
        confidence = entry.get("confidence")
        if confidence is not None:
            detail += f" (confidence={confidence})"
        lines.append(detail)
    return "\n".join(lines)


def render_environment_state_view(
    view: EnvironmentStateView, long_term_mode: str = "read"
) -> str:
    """Pure rendering of an ``EnvironmentStateView`` into a user block.

    The renderer never performs ACL filtering; it only formats the sections the
    provider already authorized.  STALE / UNAVAILABLE views render their
    freshness + reason so pre-LLM never silently reuses stale state.

    ``long_term_mode`` (Phase 5 #5): only ``"shadow"`` suppresses the
    long-term memory section — shadow compare must not be polluted by it.
    The default ``"read"`` renders the section whenever the provider
    authorized it (even when the published store is empty, so the section's
    presence stays explicit).  ``"off"`` behaves like ``"read"`` here: an
    off-mode provider never injects the section in the first place.
    """
    lines = ["---", "## Environment State", "---"]
    sections = view.sections
    for name, heading in _SECTION_HEADINGS.items():
        if name == FRESHNESS_SECTION:
            continue
        if name == "long_term_memory":
            # Phase 5 #5: shadow mode never renders the long-term section
            # (avoid polluting the shadow compare).
            if long_term_mode == "shadow":
                continue
            payload = sections.get(name)
            if payload is None:
                continue
            text = (
                _format_long_term_memory(payload)
                if isinstance(payload, dict)
                else ""
            )
            lines.append(heading)
            lines.append(text if text else "- (none published yet)")
            lines.append("---")
            continue
        if name == "system_health":
            # Phase 4 (P4): dedicated one-line-per-diagnosis formatter
            # (§3.3).  The provider injects the key only when diagnoses
            # exist, so the empty note is defensive only.
            payload = sections.get(name)
            if payload is None:
                continue
            text = _format_system_health(payload) if isinstance(payload, dict) else ""
            lines.append(heading)
            lines.append(text if text else "- (no diagnoses)")
            lines.append("---")
            continue
        payload = sections.get(name)
        text = ""
        if isinstance(payload, dict):
            text = _format_entity_dict(payload)
        elif isinstance(payload, list):
            text = _format_event_or_task_list(name, payload)
        if text:
            lines.append(heading)
            lines.append(text)
            lines.append("---")

    freshness = sections.get(FRESHNESS_SECTION, {})
    lines.append(_SECTION_HEADINGS[FRESHNESS_SECTION])
    lines.append(f"- scope_id: {freshness.get('scope_id', '')}")
    lines.append(
        f"- memory_revision: {freshness.get('memory_revision', 0)} / "
        f"view_revision: {freshness.get('view_revision', 0)}"
    )
    lines.append(
        f"- freshness: {view.freshness.value}"
        + (f" ({view.reason})" if view.reason else "")
    )
    conflicts = freshness.get("conflicts", [])
    if conflicts:
        lines.append(f"- conflicts: {len(conflicts)}")
    if view.evidence:
        lines.append(f"- evidence_refs: {len(view.evidence)}")
    if sections.get(TRUNCATED_KEY):
        lines.append("- (state truncated: token budget exhausted)")
    return "\n".join(lines)


__all__ = [
    "FRESHNESS_SECTION",
    "NEXT_CURSOR_KEY",
    "TRUNCATED_KEY",
    "EnvironmentStateProvider",
    "EnvironmentStateQuery",
    "EnvironmentStateView",
    "Freshness",
    "build_environment_state_query",
    "render_environment_state_view",
]

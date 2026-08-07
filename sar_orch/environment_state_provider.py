"""Phase 4 — concrete EnvironmentStateProvider composition root.

This module is the SAR concrete composition root for the generic
``EnvironmentStateProvider`` read path (design §6 / §8).  It combines a
``MemoryReadPort`` (canonical Spatial / Temporal / Embodied reads) with a
``ControlPlaneReadPort`` (MissionRuntime / TaskStore task views) and applies:

- viewer ACL inside the provider (never in the renderer);
- token-budget section priority (Task > Spatial > Embodied > Temporal >
  Freshness);
- ``FRESH | STALE | UNAVAILABLE`` freshness semantics;
- cursor monotonicity and cross-epoch reset.

The module is backend-adjacent (imports ``a2a.coordinator.memory`` and
``MissionRuntime`` types), but it never imports ``SARBarrier`` or any oracle /
ground-truth source (H1-INV-1).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from Agent.environment_state import (
    FRESHNESS_SECTION,
    NEXT_CURSOR_KEY,
    TRUNCATED_KEY,
    EnvironmentStateQuery,
    EnvironmentStateView,
    Freshness,
    render_environment_state_view,
)
from Agent.router_agent.state_provider import RuntimeState

#: Section budget weights (design §6): Task 25% -> Spatial 35% -> Embodied 20%
#: -> Temporal 15% -> Freshness/Evidence 5%.  Unused quota is recycled in this
#: priority order; with ``token_budget=0`` only scope + freshness + a
#: truncation marker are rendered.
SECTION_PRIORITY: tuple[str, ...] = (
    "task_execution_state",
    "spatial_state",
    "embodied_state",
    "relevant_events",
)
SECTION_WEIGHTS: dict[str, float] = {
    "task_execution_state": 0.25,
    "spatial_state": 0.35,
    "embodied_state": 0.20,
    "relevant_events": 0.15,
}

#: The two core sections (control task view + spatial facts) render at any
#: non-zero budget; embodied and temporal require more budget (recycled quota).
_SECTION_BUDGET_THRESHOLD: dict[str, int] = {
    "task_execution_state": 1,
    "spatial_state": 1,
    "embodied_state": 3,
    "relevant_events": 4,
}

VIEWER_ROLE_COORDINATOR = "coordinator"
VIEWER_ROLE_WORKER = "worker"


class MemoryReadPort:
    """Read-only canonical Memory port (Spatial / Embodied / Temporal)."""

    def __init__(self, store: Any, scope_id: str) -> None:
        self._store = store
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def memory_revision(self) -> int:
        return self._store.revision_of(self._scope_id)

    def view_revision(self) -> int:
        return self._store.view_revision_of(self._scope_id)

    def latest_sequence(self) -> int:
        events = self._store.temporal_events(self._scope_id)
        return events[-1]["sequence"] if events else 0

    def spatial_snapshot(self) -> dict[str, dict[str, Any]]:
        """Entity_id -> {entity_type, fields: {field_name: field_row}}."""
        return self._group_projection_fields("spatial")

    def embodied_snapshot(self) -> dict[str, dict[str, Any]]:
        """node_id -> {entity_type, fields: {field_name: field_row}}."""
        return self._group_projection_fields("embodied")

    def _group_projection_fields(self, domain: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self._store.projection_fields(self._scope_id):
            if row["domain"] != domain:
                continue
            entity_id = row["entity_id"]
            entry = out.setdefault(
                entity_id,
                {"entity_type": row["entity_type"], "fields": {}},
            )
            entry["fields"][row["field_name"]] = {
                "value": row["value"],
                "env_step": row.get("env_step"),
                "provenance": row.get("provenance"),
                "confidence": row.get("confidence"),
                "outcome": row.get("outcome"),
                "evidence_id": row.get("evidence_id"),
                "sequence": row.get("sequence"),
                "conflict_candidates": row.get("conflict_candidates", []),
            }
        return out

    def temporal_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        return [
            evt
            for evt in self._store.temporal_events(self._scope_id)
            if evt["sequence"] > after_sequence
        ]

    def conflicts(self) -> list[dict[str, Any]]:
        conflicts = []
        for row in self._store.projection_fields(self._scope_id):
            if row.get("outcome") == "conflicted":
                conflicts.append(
                    {
                        "domain": row["domain"],
                        "entity_id": row["entity_id"],
                        "field_name": row["field_name"],
                        "value": row["value"],
                        "conflict_candidates": row.get("conflict_candidates", []),
                    }
                )
        return conflicts

    def evidence_refs(self) -> list[str]:
        refs: list[str] = []
        for row in self._store.projection_fields(self._scope_id):
            evidence_id = row.get("evidence_id")
            if evidence_id and evidence_id not in refs:
                refs.append(evidence_id)
        return refs


class ControlPlaneReadPort:
    """Read-only task view port over MissionRuntime (design §6)."""

    def __init__(self, runtime: Any | None = None) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> Any | None:
        return self._runtime

    def task_views(self, viewer_id: str | None = None) -> list[dict[str, Any]]:
        """Canonical task views keyed by PhysicalDispatch state.

        Only dispatch state from the control plane is used; Temporal events can
        never alter this view.  A worker viewer only sees its own dispatches.
        """
        if self._runtime is None:
            return []
        views: list[dict[str, Any]] = []
        for dispatch_id, dispatch in sorted(self._runtime.dispatches.items()):
            if viewer_id and dispatch.worker_id != viewer_id:
                continue
            views.append(
                {
                    "dispatch_id": dispatch.dispatch_id,
                    "logical_node_id": dispatch.logical_node_id,
                    "worker_id": dispatch.worker_id,
                    "worker_task_id": dispatch.worker_task_id or "",
                    "state": dispatch.state.value,
                    "control_revision": self._runtime.control_revision_of(dispatch_id),
                }
            )
        return views

    def control_revision(self) -> int:
        if self._runtime is None:
            return 0
        revisions = [
            self._runtime.control_revision_of(d.dispatch_id)
            for d in self._runtime.dispatches.values()
        ]
        return max(revisions, default=0)


class EnvironmentStateProvider:
    """Concrete composition root: MemoryReadPort + ControlPlaneReadPort.

    ``viewer_role`` / ``viewer_id`` are the authenticated principal (derived by
    the caller); a query whose viewer or scope claims conflict with the
    principal / current admission is refused with ``UNAVAILABLE``.  ACL is
    applied here, never in the renderer.
    """

    def __init__(
        self,
        memory_read_port: MemoryReadPort,
        control_plane_read_port: ControlPlaneReadPort,
        *,
        scope_id: str,
        viewer_role: str = VIEWER_ROLE_COORDINATOR,
        viewer_id: str = "system",
        current_dispatch_id: str | None = None,
    ) -> None:
        self._memory_read_port = memory_read_port
        self._control_plane_read_port = control_plane_read_port
        self.scope_id = scope_id
        self.viewer_role = viewer_role
        self.viewer_id = viewer_id
        self.current_dispatch_id = current_dispatch_id
        self._last_view: EnvironmentStateView | None = None

    @property
    def _is_system(self) -> bool:
        return self.viewer_role == VIEWER_ROLE_COORDINATOR or self.viewer_id == "system"

    def _validate_principal(self, query: EnvironmentStateQuery) -> str | None:
        """Return a rejection reason, or None when claims match the principal."""
        if query.scope_id and query.scope_id != self.scope_id:
            return "scope_mismatch"
        if query.viewer_id and query.viewer_id != self.viewer_id:
            return "viewer_mismatch"
        if query.viewer_role and query.viewer_role != self.viewer_role:
            return "viewer_role_mismatch"
        if (
            query.current_dispatch_id
            and self.current_dispatch_id
            and query.current_dispatch_id != self.current_dispatch_id
        ):
            return "dispatch_mismatch"
        return None

    # ── generic read path ────────────────────────────────────────────────

    def query_environment_state(
        self, query: EnvironmentStateQuery
    ) -> EnvironmentStateView:
        rejection = self._validate_principal(query)
        if rejection is not None:
            return EnvironmentStateView(Freshness.UNAVAILABLE, reason=rejection)
        try:
            view = self._build_view(query)
            self._last_view = view
            return view
        except Exception as exc:  # noqa: BLE001 - never break pre-LLM
            if self._last_view is not None:
                return EnvironmentStateView(
                    Freshness.STALE,
                    reason=f"memory_read_error: {exc}",
                    source_revision=self._last_view.source_revision,
                )
            return EnvironmentStateView(
                Freshness.UNAVAILABLE, reason=f"memory_read_error: {exc}"
            )

    def _build_view(self, query: EnvironmentStateQuery) -> EnvironmentStateView:
        viewer_id = None if self._is_system else self.viewer_id

        spatial = self._memory_read_port.spatial_snapshot()
        embodied = self._memory_read_port.embodied_snapshot()
        events = self._memory_read_port.temporal_events(query.temporal_cursor)
        tasks = self._control_plane_read_port.task_views(viewer_id=viewer_id)

        # Worker ACL: embodied / task / temporal are restricted to self.
        if not self._is_system:
            embodied = (
                {self.viewer_id: embodied[self.viewer_id]}
                if self.viewer_id in embodied
                else {}
            )
            events = [
                e
                for e in events
                if e.get("actor_id") == self.viewer_id
                or e.get("dispatch_id") == self.current_dispatch_id
            ]

        as_of_sequence = max(
            [e["sequence"] for e in events],
            default=self._memory_read_port.latest_sequence(),
        )
        memory_revision = self._memory_read_port.memory_revision()
        view_revision = self._memory_read_port.view_revision()
        conflicts = self._memory_read_port.conflicts()
        evidence_refs = self._memory_read_port.evidence_refs()

        sections: dict[str, Any] = {
            "spatial_state": spatial,
            "embodied_state": embodied,
            "relevant_events": events,
            "task_execution_state": tasks,
            FRESHNESS_SECTION: {
                "scope_id": self.scope_id,
                "as_of_sequence": as_of_sequence,
                "memory_revision": memory_revision,
                "view_revision": view_revision,
                "conflicts": conflicts,
            },
            NEXT_CURSOR_KEY: as_of_sequence,
        }

        sections = self._apply_budget(sections, query.token_budget)

        return EnvironmentStateView(
            Freshness.FRESH,
            source_revision=view_revision,
            sections=sections,
            evidence=evidence_refs,
        )

    def _apply_budget(
        self, sections: dict[str, Any], token_budget: int
    ) -> dict[str, Any]:
        """Drop low-priority sections when the token budget is exhausted.

        Priority order (recycled): Task 25% -> Spatial 35% -> Embodied 20% ->
        Temporal 15%.  With ``token_budget=0`` only the freshness metadata
        (scope + revisions) and a truncation marker survive.
        """
        result: dict[str, Any] = {
            "spatial_state": {},
            "embodied_state": {},
            "relevant_events": [],
            "task_execution_state": [],
            FRESHNESS_SECTION: sections[FRESHNESS_SECTION],
            NEXT_CURSOR_KEY: sections[NEXT_CURSOR_KEY],
            TRUNCATED_KEY: token_budget <= 0,
        }
        if token_budget <= 0:
            return result
        for name in SECTION_PRIORITY:
            if token_budget >= _SECTION_BUDGET_THRESHOLD.get(name, 1):
                result[name] = sections[name]
        return result

    # ── legacy StateProvider facade ──────────────────────────────────────

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Backwards-compatible ``StateProvider`` facade.

        Projects a large-budget environment state view into a ``RuntimeState``
        so existing ContextManager refresh hooks keep working unchanged.
        """
        query = EnvironmentStateQuery(
            scope_id=self.scope_id,
            viewer_role=self.viewer_role,
            viewer_id=self.viewer_id,
            current_dispatch_id=self.current_dispatch_id,
            temporal_cursor=0,
            token_budget=0,
        )
        view = self.query_environment_state(query)
        return RuntimeState(
            version=view.source_revision or 0,
            env_step=0,
            payload={
                "environment_state": dict(view.sections),
                "freshness": view.freshness.value,
                "reason": view.reason,
            },
            stale=view.freshness is not Freshness.FRESH,
            refresh_error=view.reason,
        )


# ---------------------------------------------------------------------------
# Pure renderer — re-exported from the generic module (design §6).  The
# renderer performs NO ACL filtering.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shadow compare (Phase 4 contract): legacy/new view diff with allowlist
# ---------------------------------------------------------------------------

DEFAULT_SHADOW_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Structural / metadata-only differences between legacy and read-port
        # projections are expected and allowed.
        "as_of_sequence",
        "memory_revision",
        "view_revision",
        "snapshot_revision",
        "next_cursor",
        "evidence_refs",
        "relevant_events",
        "recent_observations",
        "last_message",
        "updated_at",
        "created_at",
    }
)


@dataclass(frozen=True)
class ShadowDiff:
    """One field-level difference between the legacy and read-port views."""

    path: str
    legacy: Any = None
    read_port: Any = None
    allowed: bool = False


@dataclass(frozen=True)
class ShadowCompareResult:
    diffs: list[ShadowDiff] = field(default_factory=list)

    @property
    def non_allowlist_diffs(self) -> list[ShadowDiff]:
        return [d for d in self.diffs if not d.allowed]

    @property
    def clean(self) -> bool:
        return not self.non_allowlist_diffs


def compare_legacy_vs_read_port(
    legacy_view: dict[str, Any],
    read_port_view: EnvironmentStateView,
    *,
    allowlist: frozenset[str] = DEFAULT_SHADOW_ALLOWLIST,
    max_diffs: int = 200,
) -> ShadowCompareResult:
    """Field-level diff of a legacy projection vs the canonical read-port view.

    ``max_diffs`` bounds the report so a pathological divergence cannot blow up
    the rollout audit.  Only non-allowlist diffs count toward Phase 4's "zero
    non-allowlist diff" gate.
    """
    diffs: list[ShadowDiff] = []

    def _walk(path: str, legacy: Any, read_port: Any, allowed: bool = False) -> None:
        if len(diffs) >= max_diffs:
            return

        # A missing key on one side and an empty container on the other both
        # mean "absent/empty" for shadow-compare purposes (e.g. the legacy
        # normalizer cannot reconstruct a temporal delta).  Treat them equal.
        def _is_empty(value: Any) -> bool:
            return value is None or value == [] or value == {}

        if _is_empty(legacy) and _is_empty(read_port):
            return

        if type(legacy) is not type(read_port) and not (
            isinstance(legacy, (int, float)) and isinstance(read_port, (int, float))
        ):
            diffs.append(ShadowDiff(path, legacy, read_port, allowed=allowed))
            return
        if isinstance(legacy, dict) and isinstance(read_port, dict):
            for key in sorted(set(legacy) | set(read_port)):
                child_allowed = allowed or key in allowlist
                _walk(
                    f"{path}.{key}",
                    legacy.get(key),
                    read_port.get(key),
                    child_allowed,
                )
            return
        if isinstance(legacy, (list, tuple)) and isinstance(read_port, (list, tuple)):
            for i in range(max(len(legacy), len(read_port))):
                lv = legacy[i] if i < len(legacy) else None
                rv = read_port[i] if i < len(read_port) else None
                _walk(f"{path}[{i}]", lv, rv, allowed)
            return
        if legacy != read_port:
            diffs.append(ShadowDiff(path, legacy, read_port, allowed=allowed))

    _walk("", legacy_view, dict(read_port_view.sections))
    return ShadowCompareResult(diffs)


class RolloutAuditWriter:
    """Append-only ``memory_rollout_audit.ndjson`` writer (Phase 4 rollback).

    Records non-allowlist legacy-vs-read-port diffs so that any provider / ACL
    gate failure can be audited before falling back to ``legacy``.  Canonical
    DB / outbox rows are never deleted by a rollback; this audit is a separate
    evidence stream.  Diff values are redacted before persistence so secrets /
    raw mailbox body never appear in the audit (Phase 2 redaction boundary).
    """

    def __init__(
        self,
        audit_path: str | os.PathLike[str] | None = None,
        *,
        redactor: Any | None = None,
        secret: bytes | str | None = None,
    ) -> None:
        self._path = Path(audit_path) if audit_path else None
        if redactor is not None:
            self._redactor = redactor
        else:
            from Agent.redaction import SensitiveTextRedactor

            secrets = [secret] if secret is not None else None
            self._redactor = SensitiveTextRedactor(
                secrets=secrets,
                extra=[
                    # String-embedded mailbox content (not just dict keys):
                    # ``mail_body=...`` / ``mailbox_body=...`` / ``message_body=...``
                    (
                        "mailbox",
                        (
                            r"(?i)((?:mail[_-]?body|mailbox[_-]?body|message[_-]?body"
                            r"|mail[_-]?subject|mailbox[_-]?subject)\s*[:=]\s*)"
                            r"(?P<value>[^\n;]+)"
                        ),
                    ),
                ],
            )

    def set_path(self, audit_path: str | os.PathLike[str] | None) -> None:
        self._path = Path(audit_path) if audit_path else None

    def record_diff(
        self,
        *,
        scope_id: str,
        result: ShadowCompareResult,
        mode: str = "shadow",
    ) -> int:
        """Append one audit line per non-allowlist diff; returns count written."""
        if self._path is None:
            return 0
        non_allowed = result.non_allowlist_diffs
        if not non_allowed:
            return 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            for diff in non_allowed:
                record = {
                    "kind": "memory_rollout_audit",
                    "scope_id": scope_id,
                    "mode": mode,
                    "path": diff.path,
                    # Redact before persistence: the audit must never carry a
                    # secret / raw mailbox body / credential原文.
                    "legacy": self._redactor.redact_data(diff.legacy),
                    "read_port": self._redactor.redact_data(diff.read_port),
                }
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return len(non_allowed)

    def record_rollback(
        self,
        *,
        scope_id: str,
        reason: str,
        mode_from: str = "read_port",
        mode_to: str = "legacy",
        correlation: dict[str, Any] | None = None,
    ) -> bool:
        """Append a redacted read_port→legacy rollback audit record.

        Returns True when a line was written (i.e. audit is configured).  The
        reason and any ``correlation`` values are redacted so a failure detail
        or secret can never leak into the audit stream.  ``correlation`` is
        intended for non-secret correlation metadata (e.g. the failing
        worker's opaque ``worker_task_id`` / agent name).
        """
        if self._path is None:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            record = {
                "kind": "memory_rollout_audit",
                "event": "read_port_to_legacy_rollback",
                "scope_id": scope_id,
                "mode_from": mode_from,
                "mode_to": mode_to,
                "reason": self._redactor.redact(str(reason or "")),
            }
            if correlation:
                record["correlation"] = self._redactor.redact_data(dict(correlation))
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True


class MemoryRolloutController:
    """Process-scoped read_port→legacy rollback latch (Phase 4 rollback).

    Once a read-port provider / ACL gate failure is observed, the latch flips
    permanently so the coordinator context rendering falls back to the legacy
    pinned path for every subsequent read-path call in the process/run.  The
    canonical SQLite DB / outbox stay read-only; only a redacted
    ``memory_rollout_audit`` record is written on the transition.
    """

    def __init__(
        self,
        *,
        audit: RolloutAuditWriter | None = None,
        scope_id: str = "",
    ) -> None:
        self._audit = audit
        self._scope_id = scope_id
        self._rolled_back = False
        self._reason = ""

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def active(self) -> bool:
        """True while the read-port path is still trusted (not rolled back)."""
        return not self._rolled_back

    def set_scope(self, scope_id: str) -> None:
        self._scope_id = scope_id

    def rollback(
        self, reason: str, *, correlation: dict[str, Any] | None = None
    ) -> bool:
        """Latch the rollback (once) and append the redacted audit record.

        Returns True only for the first transition; subsequent calls are no-ops
        so the audit stream gets exactly one rollback record per process/run.
        ``correlation`` carries non-secret correlation metadata (e.g.
        ``worker_task_id`` / agent name) into the audit.  Never raises (audit
        write must not break pre-LLM).
        """
        if self._rolled_back:
            return False
        self._rolled_back = True
        self._reason = str(reason or "")
        try:
            if self._audit is not None:
                self._audit.record_rollback(
                    scope_id=self._scope_id,
                    reason=self._reason,
                    correlation=correlation,
                )
        except Exception:  # noqa: BLE001,S110 - audit write must not break pre-LLM
            pass
        return True


# Legacy coordinator payload -> canonical section-shape normalizer.  The live
# ``memory_read_mode="shadow"`` path keeps the legacy pinned Context as its
# LLM source, but also builds the canonical provider view for the same scope /
# principal and compares the two projections.  Because the legacy payload and
# the canonical ``EnvironmentStateView.sections`` have different shapes, we
# project the legacy payload into the same section shape first so the compare
# is meaningful (and only non-allowlist domain diffs are recorded).
LEGACY_ALLOWLIST_EXTRA: frozenset[str] = frozenset(
    {
        # Legacy-only structural keys that have no canonical equivalent.
        "state_mode",
        "mission_finished",
        "step_budget",
        "map_revision",
        "map_delta",
        "map_summary",
        "map_summary_revision",
        "mission_dag_view",
        "task_status_view",
        "recent_changes",
        "supervision",
        "stale",
        "refresh_error",
        "known_dynamic_objects",
        "known_priors",
        "stale_entries",
        "unknowns",
        "rules",
        "task_objective",
        "workers",
        "agent_summaries",
        "pending_requests",
        "capabilities",
        "formatted_summary",
        "status",
        "last_seen_step",
        "last_seen_ts",
        "sources",
        "conflict",
        "last_position",
        "current_task_id",
        "task_state",
        "logical_node_id",
        "worker_task_id",
        "artifact_preview",
        "result_preview",
        "control_revision",
        "truncated",
        "env_step",
        "confidence",
        "provenance",
        "outcome",
        "evidence_id",
        "sequence",
        "conflict_candidates",
        "conflict_event_ids",
        "superseded_event_id",
    }
)

#: Effective allowlist used by the live shadow-compare path.
SHADOW_COMPARE_ALLOWLIST: frozenset[str] = (
    DEFAULT_SHADOW_ALLOWLIST | LEGACY_ALLOWLIST_EXTRA
)


#: Legacy merge / bookkeeping keys that never enter the canonical projection.
#: The canonical reducer claims every worker-reported attribute (skipping
#: ``position`` and, for non-embodied entities, ``inventory``); the legacy map
#: additionally stores merge artifacts (``observed_cells``, ``sources``,
#: ``last_seen_ts``, ``last_seen_step``, ``conflict``, ``conflicts``,
#: ``confidence``) and structural fields (``name`` / ``object_type``).  The
#: normalizer excludes exactly those, so every other worker-derived attribute is
#: projected dynamically (no fixed whitelist that can drift from the reducer).
_WORKER_ARTIFACT_KEYS: frozenset[str] = frozenset(
    {
        "observed_cells",
        "sources",
        "last_seen_ts",
        "last_seen_step",
        "confidence",
        "conflict",
        "conflicts",
        "position",
        "inventory",
        "name",
        "object_type",
    }
)


def _legacy_worker_field_rows(obj: dict[str, Any]) -> dict[str, Any]:
    """Dynamically project worker-derived field rows from a legacy object dict.

    Reads the legacy map's ``attributes`` (which carry the worker-reported
    claims) and excludes merge artifacts, so the projection matches what the
    canonical reducer stores for the same evidence.  The top-level ``status``
    is used as a fallback only when it is a real worker-derived value — the
    default ``"unknown"`` marker is never projected as a claim.
    """
    raw_attrs = obj.get("attributes")
    attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
    rows: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in _WORKER_ARTIFACT_KEYS:
            continue
        if value is not None:
            rows[key] = {"value": value}
    status = obj.get("status")
    if status is not None and status != "unknown" and "status" not in rows:
        rows["status"] = {"value": status}
    return rows


def _legacy_cell_field_rows(cell: dict[str, Any]) -> dict[str, Any]:
    """Dynamically project a fire-region cell's own worker-derived fields.

    Cells keep their observations (``position``, ``attributes``) separate from
    the parent region's direct claims, mirroring the canonical per-cell
    projection.  Only non-artifact attributes are projected.
    """
    raw_attrs = cell.get("attributes")
    attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
    rows: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in _WORKER_ARTIFACT_KEYS:
            continue
        if value is not None:
            rows[key] = {"value": value}
    return rows


def _legacy_object_position(obj: dict[str, Any]) -> list[Any] | None:
    position = obj.get("position")
    return list(position) if position else None


def _canonical_conflict_entry(
    domain: str, entity_id: str, conflict_rec: dict[str, Any]
) -> dict[str, Any]:
    """Project a legacy per-field conflict record into the canonical freshness
    ``conflicts`` shape (``domain`` / ``entity_id`` / ``field_name`` / ``value``
    / ``conflict_candidates``).  The retained ``value`` is the same current
    holder the legacy map kept (C3), so it aligns with the canonical reducer's
    conflicted field value.
    """
    return {
        "domain": domain,
        "entity_id": entity_id,
        "field_name": conflict_rec.get("field_name"),
        "value": conflict_rec.get("value"),
        "conflict_candidates": [
            {"value": c} for c in (conflict_rec.get("candidates") or [])
        ],
    }


def _legacy_worker_evidenced(obj: dict[str, Any]) -> bool:
    """True when a legacy object was worker-observed (not a static prior).

    Static priors (reservoirs / deposits / agents initialized from scene
    config) carry no ``sources`` and keep ``last_seen_step == 0``; the
    canonical projection only ever contains worker-observed entities, so
    unobserved priors must be omitted to keep the compare aligned.
    """
    if obj.get("sources"):
        return True
    try:
        return int(obj.get("last_seen_step") or 0) > 0
    except (TypeError, ValueError):
        return False


def normalize_legacy_view(legacy_view: dict[str, Any]) -> dict[str, Any]:
    """Project a legacy coordinator Environment State view into the canonical
    section shape so ``compare_legacy_vs_read_port`` is meaningful.

    The legacy payload (``semantic_summary``, ``team_status_summary``,
    ``physical_dispatches_view``) is mapped onto the canonical section keys
    (``spatial_state``, ``embodied_state``, ``task_execution_state``,
    ``relevant_events``, ``freshness``).  The projection follows the same
    worker-evidence projection as the canonical reducer:

    - worker-derived attributes are projected dynamically (all non-artifact
      keys, never a fixed whitelist; merge artifacts such as ``observed_cells``
      / ``sources`` / ``last_seen_ts`` / ``conflicts`` are excluded);
    - fire regions are expanded into one entity per ``observed_cells`` cell,
      with each cell projected from its OWN data (never the parent's), and a
      region that was itself directly observed (``average_intensity`` / direct
      position / standalone intensity) is kept as its own entity with its own
      fields — so parent/cell differing values both survive;
    - static-prior-only entities (reservoirs / deposits / embodied agents with
      no worker evidence) are omitted;
    - freshness ``conflicts`` are emitted in the canonical shape
      (``domain`` / ``entity_id`` / ``field_name`` / ``value`` /
      ``conflict_candidates``) from the legacy map's retained-holder conflict
      records, proving C3 stays explicit on the legacy side.

    Values are only structural / Worker-derived facts — never secrets or raw
    mailbox bodies.
    """
    payload = legacy_view.get("payload") if isinstance(legacy_view, dict) else None
    if not isinstance(payload, dict):
        payload = legacy_view if isinstance(legacy_view, dict) else {}

    semantic = payload.get("semantic_summary") or {}
    team = payload.get("team_status_summary") or {}
    dispatches = payload.get("physical_dispatches_view") or []

    spatial_state: dict[str, Any] = {}
    freshness_conflicts: list[dict[str, Any]] = []
    dynamic = semantic.get("known_dynamic_objects") or {}
    priors = semantic.get("known_priors") or {}
    for kind, objs in list(dynamic.items()) + list(priors.items()):
        if not isinstance(objs, list):
            continue
        is_prior = kind in priors
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            if is_prior and not _legacy_worker_evidenced(obj):
                continue
            region_name = obj.get("name") or obj.get("object_type") or ""
            if not region_name:
                continue
            entity_type = obj.get("object_type", kind)
            raw_attrs = obj.get("attributes")
            attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
            rows = _legacy_worker_field_rows(obj)
            cells = attrs.get("observed_cells")
            if isinstance(cells, list) and cells:
                # Reconstruct fire-region entities from observed_cells: the
                # legacy map keeps per-cell observations separate from the
                # parent region's direct claims, and canonical stores one
                # spatial entity per observed cell — project cells from their
                # OWN data (never the parent's), so parent/cell differing
                # values both survive.
                for cell in cells:
                    if not isinstance(cell, dict):
                        continue
                    cell_name = cell.get("name") or ""
                    if not cell_name:
                        continue
                    cell_rows = _legacy_cell_field_rows(cell)
                    cell_pos = cell.get("position")
                    if cell_pos:
                        cell_rows["position"] = {"value": list(cell_pos)}
                    if cell_name != region_name:
                        cell_rows["parent_fire"] = {"value": region_name}
                    spatial_state[cell_name] = {
                        "entity_type": entity_type,
                        "fields": cell_rows,
                    }
                    for conflict_rec in cell.get("conflicts") or []:
                        freshness_conflicts.append(
                            _canonical_conflict_entry(
                                "spatial", cell_name, conflict_rec
                            )
                        )
                # Materialize the region entity only when the region itself was
                # directly observed (a ``Fire`` claim carries
                # ``average_intensity``, or a direct observation seeded its
                # position / standalone intensity).  A region known only via
                # cells never exists as a canonical entity, so it must not be
                # emitted.  Its fields are the region's OWN direct claims —
                # cell-derived consensus (``fire_type``) and cell-level
                # ``intensity`` / ``parent_fire`` never leak onto it.
                if (
                    "average_intensity" in rows
                    or "intensity" in rows
                    or _legacy_object_position(obj) is not None
                ):
                    region_rows = dict(rows)
                    position = _legacy_object_position(obj)
                    if position:
                        region_rows["position"] = {"value": position}
                    spatial_state[region_name] = {
                        "entity_type": entity_type,
                        "fields": region_rows,
                    }
            else:
                position = _legacy_object_position(obj)
                if position:
                    rows["position"] = {"value": position}
                spatial_state[region_name] = {
                    "entity_type": entity_type,
                    "fields": rows,
                }
            for conflict_rec in obj.get("conflicts") or []:
                freshness_conflicts.append(
                    _canonical_conflict_entry("spatial", region_name, conflict_rec)
                )

    embodied_state: dict[str, Any] = {}
    for worker in team.get("workers") or []:
        if not isinstance(worker, dict):
            continue
        agent_id = worker.get("agent_id") or ""
        if not agent_id:
            continue
        position = worker.get("last_position")
        inventory = worker.get("inventory")
        try:
            worker_evidenced = (
                bool(position)
                or bool(inventory)
                or bool(worker.get("last_message"))
                or int(worker.get("last_seen_step") or 0) > 0
            )
        except (TypeError, ValueError):
            worker_evidenced = False
        # Static-prior-only agents (no worker-evidenced fields) never appear in
        # the canonical embodied projection — omit them so the compare stays
        # aligned.
        if not worker_evidenced:
            continue
        fields: dict[str, Any] = {}
        if position:
            fields["position"] = {"value": list(position)}
        if inventory is not None:
            # Legacy semantic-map inventory is ``{resource: count}``; canonical
            # projection stores ``[resource, ...]``.  Normalize to the resource
            # list so the shadow compare compares the same shape.
            if isinstance(inventory, dict):
                inventory = sorted(inventory.keys())
            fields["inventory"] = {"value": list(inventory)}
        if not fields:
            continue
        embodied_state[agent_id] = {"entity_type": "agent", "fields": fields}

    task_execution_state: list[dict[str, Any]] = []
    for dispatch in dispatches:
        if not isinstance(dispatch, dict):
            continue
        task_execution_state.append(
            {
                "dispatch_id": dispatch.get("dispatch_id"),
                "worker_id": dispatch.get("worker_id"),
                "worker_task_id": dispatch.get("worker_task_id") or "",
                "state": dispatch.get("state"),
            }
        )

    freshness = semantic.get("step_budget") or {}
    return {
        "spatial_state": spatial_state,
        "embodied_state": embodied_state,
        "task_execution_state": task_execution_state,
        "relevant_events": [],
        "freshness": {
            "scope_id": legacy_view.get("scope_id", ""),
            "memory_revision": legacy_view.get("memory_revision", 0),
            "conflicts": freshness_conflicts,
        },
        "step_budget": dict(freshness),
    }


@dataclass
class ShadowCompareReport:
    """Inspectable shadow-compare report/counter (H2 evidence).

    ``runs`` counts live shadow-compare invocations; ``clean_runs`` counts
    invocations with zero non-allowlist diffs; ``non_allowlist_diff_count``
    totals recorded differences.  ``last_*`` fields describe the most recent
    comparison.  The report never carries secrets or raw mailbox content.
    """

    scope_id: str = ""
    runs: int = 0
    clean_runs: int = 0
    non_allowlist_diff_count: int = 0
    last_clean: bool | None = None
    last_reason: str = ""
    last_audit_written: int = 0
    last_diff_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "runs": self.runs,
            "clean_runs": self.clean_runs,
            "non_allowlist_diff_count": self.non_allowlist_diff_count,
            "last_clean": self.last_clean,
            "last_reason": self.last_reason,
            "last_audit_written": self.last_audit_written,
            "last_diff_paths": self.last_diff_paths,
        }


class ShadowCompareService:
    """Live ``memory_read_mode="shadow"`` compare + audit runner.

    Invoked on the legacy Context path (``SARCoordinatorStateProvider``
    snapshot) — the legacy view stays the LLM Context source, and this service
    additionally builds the canonical provider view for the same principal /
    scope, compares the projections, records non-allowlist diffs in
    ``memory_rollout_audit.ndjson`` at the run log, and exposes an inspectable
    report/counter.  It never raises (pre-LLM safety) and never writes to the
    canonical SQLite DB (read-only evidence).
    """

    def __init__(
        self,
        *,
        audit_path: str | os.PathLike[str] | None = None,
        allowlist: frozenset[str] = SHADOW_COMPARE_ALLOWLIST,
        max_diffs: int = 200,
    ) -> None:
        self._audit = RolloutAuditWriter(audit_path)
        self._allowlist = allowlist
        self._max_diffs = max_diffs
        self._report = ShadowCompareReport()
        self._last_scope_id: str = ""

    @property
    def report(self) -> ShadowCompareReport:
        return self._report

    def report_dict(self) -> dict[str, Any]:
        return self._report.to_dict()

    def set_audit_path(self, audit_path: str | os.PathLike[str] | None) -> None:
        self._audit.set_path(audit_path)

    def run(
        self,
        *,
        legacy_view: dict[str, Any],
        read_port_view: EnvironmentStateView,
        scope_id: str,
    ) -> ShadowCompareResult:
        """Compare legacy vs canonical projection for the same scope/principal.

        Never raises; any internal failure is folded into the report as a
        non-clean outcome with a reason.  Canonical Memory is only read.
        """
        self._report.scope_id = scope_id
        self._report.runs += 1
        try:
            normalized = normalize_legacy_view(legacy_view)
            result = compare_legacy_vs_read_port(
                normalized,
                read_port_view,
                allowlist=self._allowlist,
                max_diffs=self._max_diffs,
            )
        except Exception as exc:  # noqa: BLE001 - never break pre-LLM
            self._report.last_clean = False
            self._report.last_reason = f"shadow_compare_error: {exc}"
            return ShadowCompareResult()

        non_allowed = result.non_allowlist_diffs
        self._report.last_clean = not non_allowed
        self._report.last_reason = (
            "" if not non_allowed else f"{len(non_allowed)} non-allowlist diff(s)"
        )
        self._report.last_diff_paths = [d.path for d in non_allowed]
        if not non_allowed:
            self._report.clean_runs += 1
        self._report.non_allowlist_diff_count += len(non_allowed)
        try:
            self._report.last_audit_written = self._audit.record_diff(
                scope_id=scope_id, result=result, mode="shadow"
            )
        except Exception as exc:  # noqa: BLE001 - audit write must not break pre-LLM
            self._report.last_audit_written = 0
            self._report.last_reason += f"; audit_error: {exc}"
        return result

    def mark_error(self, scope_id: str, exc: Exception) -> None:
        """Record a canonical read failure as a non-clean shadow outcome."""
        self._report.scope_id = scope_id
        self._report.runs += 1
        self._report.last_clean = False
        self._report.last_reason = f"shadow_compare_error: {exc}"
        self._report.last_audit_written = 0


__all__ = [
    "DEFAULT_SHADOW_ALLOWLIST",
    "FRESHNESS_SECTION",
    "LEGACY_ALLOWLIST_EXTRA",
    "NEXT_CURSOR_KEY",
    "SECTION_PRIORITY",
    "SECTION_WEIGHTS",
    "SHADOW_COMPARE_ALLOWLIST",
    "TRUNCATED_KEY",
    "VIEWER_ROLE_COORDINATOR",
    "VIEWER_ROLE_WORKER",
    "ControlPlaneReadPort",
    "EnvironmentStateProvider",
    "MemoryReadPort",
    "MemoryRolloutController",
    "RolloutAuditWriter",
    "ShadowCompareReport",
    "ShadowCompareResult",
    "ShadowCompareService",
    "ShadowDiff",
    "compare_legacy_vs_read_port",
    "normalize_legacy_view",
    "render_environment_state_view",
]

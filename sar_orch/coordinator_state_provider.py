from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from Agent.environment_state import Freshness
from Agent.router_agent.state_provider import (
    AsyncStatePreparer,
    RuntimeState,
)

if TYPE_CHECKING:
    from sar_orch.barrier import SARBarrier
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.summarizer import MapSummarizer
    from a2a.coordinator.supervision_state_store import SupervisionStateStore
    from a2a.coordinator.event_store import EventStore
    from a2a.coordinator.task_store import TaskStore


class SARCoordinatorStateProvider(AsyncStatePreparer):
    """Read-only runtime state provider for the SAR Coordinator.

    Bridges SAR-specific backends (barrier, semantic map, event store, task
    store, supervision store) into the generic RuntimeState DTO consumed by
    CoordinatorContextManager. Implementations are lightweight snapshot reads;
    no I/O or sensor acquisition is triggered here.

    Implements ``AsyncStatePreparer`` so that ``ContextManager`` can invoke
    async LLM-bound preparation (map diff / summary) before the synchronous
    ``snapshot()`` call.
    """

    def __init__(
        self,
        barrier: "SARBarrier | None" = None,
        semantic_map: "SemanticMapStore | None" = None,
        event_store: "EventStore | None" = None,
        state_mode: str = "semantic",
        supervision_state_store: "SupervisionStateStore | None" = None,
        agent_registry: "Any | None" = None,
        map_summarizer: "MapSummarizer | None" = None,
        log_dir: "str | None" = None,
        memory_read_mode: str = "legacy",
    ) -> None:
        self._barrier = barrier
        self._semantic_map = semantic_map
        self._event_store = event_store
        self._state_mode = state_mode
        self._supervision_state_store = supervision_state_store
        self._agent_registry = agent_registry
        self._map_summarizer = map_summarizer
        self._log_dir = log_dir
        self._memory_read_mode = memory_read_mode
        self._task_store: "TaskStore | None" = None
        self._runtime = None
        self._last_version: int = -1
        self._semantic_cached: dict[str, Any] | None = None
        self._last_snapshot: RuntimeState | None = None
        self._env_state_provider: Any | None = None
        self._memory_ingestor: Any | None = None

        # Phase 4: live shadow-compare runner (memory_read_mode="shadow").
        self._shadow_compare: Any | None = None
        self._shadow_compare_runs_per_step: set[int] = set()
        if self._memory_read_mode == "shadow":
            from pathlib import Path

            from sar_orch.environment_state_provider import ShadowCompareService

            audit_path = None
            if self._log_dir:
                audit_path = str(Path(self._log_dir) / "memory_rollout_audit.ndjson")
            self._shadow_compare = ShadowCompareService(audit_path=audit_path)

        # Phase 4 rollback: read_port -> legacy latch + redacted audit.
        self._memory_rollout: Any | None = None
        if self._memory_read_mode == "read_port":
            from pathlib import Path

            from sar_orch.environment_state_provider import (
                MemoryRolloutController,
                RolloutAuditWriter,
            )

            audit_path = None
            if self._log_dir:
                audit_path = str(Path(self._log_dir) / "memory_rollout_audit.ndjson")
            self._memory_rollout = MemoryRolloutController(
                audit=RolloutAuditWriter(audit_path)
            )

        # Phase 5 — continuity tracking between prepare_for_llm() calls
        self._semantic_revision: int | None = None
        self._previous_map_snapshot: dict[str, Any] | None = None
        self._previous_map_revision: int | None = None
        self._last_map_delta: dict[str, Any] | None = None
        self._last_summary: str = ""
        self._last_summary_revision: int = 0
        self._runtime_version: int = 0

    # ── Phase 4 rollback surface (read_port -> legacy) ──────────────────

    def rollout_rolled_back(self) -> bool:
        """True once a read-port failure latched the legacy fallback."""
        return bool(getattr(self._memory_rollout, "rolled_back", False))

    def rollout_active(self) -> bool:
        """True while read-port rendering is still trusted (not rolled back)."""
        return getattr(self._memory_rollout, "active", True)

    def rollback_environment_state(self, reason: str) -> bool:
        """Trigger the read_port -> legacy rollback (once) and write audit.

        Returns True on the first transition; subsequent calls are no-ops so
        exactly one audit record is written per process/run.  Canonical DB /
        outbox are never touched.
        """
        controller = getattr(self, "_memory_rollout", None)
        if controller is None:
            return False
        scope_id = self.scope_id or ""
        controller.set_scope(scope_id)
        return controller.rollback(reason)

    def set_task_store(self, task_store: "TaskStore | None") -> None:
        """Attach the per-request TaskStore once it is created."""
        self._task_store = task_store
        if task_store is not None and self._log_dir is not None:
            from pathlib import Path

            from a2a.coordinator.mission_graph import MissionGraphJsonlLogger

            barrier = self._barrier

            def _env_step() -> int:
                return (
                    getattr(barrier, "_step_counter", 0) if barrier is not None else 0
                )

            task_store.set_mission_graph_history_sink(
                MissionGraphJsonlLogger(
                    Path(self._log_dir) / "mission_graph.jsonl",
                    step_getter=_env_step,
                )
            )

    def set_runtime(self, runtime) -> None:
        """Attach the active context-bound MissionRuntime.

        When a canonical MemoryIngestor is attached and ``memory_read_mode`` is
        ``read_port``, the concrete EnvironmentStateProvider is (re)built for
        the admitted scope so the ContextManager read-port path resolves against
        canonical Memory + the control plane.
        """
        self._runtime = runtime
        ingestor = getattr(self, "_memory_ingestor", None)
        if ingestor is not None and runtime is not None:
            try:
                self._attach_read_port_provider(ingestor, runtime)
            except Exception as exc:  # noqa: BLE001 - never break pre-LLM
                # Never break pre-LLM; the adapter renders UNAVAILABLE instead.
                import logging

                logging.getLogger(__name__).debug(
                    "read-port provider build skipped: %s", exc
                )

    def set_memory_ingestor(self, ingestor) -> None:
        """Attach the canonical MemoryIngestor for read-port provider builds."""
        self._memory_ingestor = ingestor

    def _attach_read_port_provider(self, ingestor, runtime) -> None:
        """Build the concrete EnvironmentStateProvider for the active scope."""
        from sar_orch.environment_state_provider import (
            ControlPlaneReadPort,
            EnvironmentStateProvider,
            MemoryReadPort,
        )

        runtime_epoch = runtime._manager.epoch
        scope_id = ingestor.scope_id_for(runtime.context_id, runtime_epoch)
        provider = EnvironmentStateProvider(
            MemoryReadPort(ingestor.store, scope_id),
            ControlPlaneReadPort(runtime),
            scope_id=scope_id,
            viewer_role="coordinator",
            viewer_id="system",
        )
        self.set_environment_state_provider(provider)

    # ── Phase 4: read-port delegation (EnvironmentStateProvider adapter) ──

    def set_environment_state_provider(self, provider) -> None:
        """Attach the concrete EnvironmentStateProvider (coordinator/system).

        When ``memory_read_mode == "read_port"`` the CoordinatorContextManager
        calls ``query_environment_state`` on this adapter; the provider applies
        ACL / budget / freshness and never reads SARBarrier (H1-INV-1).
        """
        self._env_state_provider = provider

    @property
    def scope_id(self) -> str:
        return getattr(self._env_state_provider, "scope_id", "") or ""

    @property
    def viewer_role(self) -> str:
        return getattr(self._env_state_provider, "viewer_role", "coordinator")

    @property
    def viewer_id(self) -> str:
        return getattr(self._env_state_provider, "viewer_id", "system")

    @property
    def current_dispatch_id(self) -> str | None:
        return getattr(self._env_state_provider, "current_dispatch_id", None)

    def query_environment_state(self, query):
        """Delegate to the attached concrete provider.

        Falls back to a UNAVAILABLE view when no read-port provider is wired so
        the ContextManager never silently reuses stale state.
        """
        provider = getattr(self, "_env_state_provider", None)
        if provider is None or not hasattr(provider, "query_environment_state"):
            from Agent.environment_state import EnvironmentStateView, Freshness

            return EnvironmentStateView(
                Freshness.UNAVAILABLE,
                reason="environment_state_provider_not_configured",
            )
        return provider.query_environment_state(query)

    # ── Environment State adapter view ───────────────────────────────────

    def build_environment_state_view(
        self, context_id: str | None = None
    ) -> dict[str, Any]:
        """Project the current runtime snapshot as an Environment State view.

        Environment State is a rebuildable view, not a persistence truth source.
        The view carries the projection metadata required by the Environment State
        contract: ``scope_id``, ``as_of_sequence``, ``memory_revision``,
        ``freshness``, ``conflicts`` and ``evidence_refs``.  This provider is the
        Phase 1 transition owner that adapts SAR backends into that view; the
        renderer (``ContextManager``) stays a pure projection consumer.
        """
        state = self.snapshot(context_id)

        semantic = state.get("semantic_summary") or {}
        conflicts: list[Any] = []
        if isinstance(semantic, dict):
            raw_conflicts = semantic.get("conflicts", [])
            if isinstance(raw_conflicts, list):
                conflicts = list(raw_conflicts)
        evidence_refs: list[Any] = []
        if self._event_store is not None:
            evidence_refs = [
                str(obs) for obs in self._event_store.get_recent_observations(limit=5)
            ]
        elif isinstance(semantic, dict):
            recent = semantic.get("recent_observations", [])
            if isinstance(recent, list):
                evidence_refs = [str(obs) for obs in recent[:5]]

        if state.stale:
            freshness = (
                Freshness.STALE.value if state.payload else Freshness.UNAVAILABLE.value
            )
        else:
            freshness = Freshness.FRESH.value

        return {
            "scope_id": context_id or "",
            "as_of_sequence": state.env_step,
            "memory_revision": state.version,
            "freshness": freshness,
            "conflicts": conflicts,
            "evidence_refs": evidence_refs,
            "stale": state.stale,
            "refresh_error": state.refresh_error,
            "payload": dict(state.payload),
        }

    # ── Phase 4: live shadow-compare (memory_read_mode="shadow") ──────────

    def shadow_compare_report(self) -> dict[str, Any]:
        """Inspectable shadow-compare report/counter (H2 evidence).

        Returns an empty report when shadow mode is not active or the compare
        has not run yet.  The legacy pinned Context remains the LLM source; the
        canonical provider view is only compared for evidence.
        """
        if self._shadow_compare is None:
            return {
                "scope_id": "",
                "runs": 0,
                "clean_runs": 0,
                "non_allowlist_diff_count": 0,
                "last_clean": None,
                "last_reason": "shadow_compare_not_configured",
                "last_audit_written": 0,
                "last_diff_paths": [],
                "last_horizon": None,
            }
        return self._shadow_compare.report_dict()

    def _run_shadow_compare(self, env_step: int, payload: dict[str, Any]) -> None:
        """Run the live shadow compare once per env step.

        Builds the legacy Environment State view (the actual Context source)
        and the canonical provider view for the same scope / viewer, compares
        them at a settled common env-step horizon, and records non-allowlist
        diffs.  The horizon is the current barrier step: evidence at the
        current (still in-flight) env step is excluded from BOTH projections so
        callback timing cannot produce false non-allowlist diffs, while genuine
        divergence at settled steps stays detectable.  Never raises (pre-LLM
        safety) and never writes to the canonical SQLite DB — evidence only.
        """
        if self._shadow_compare is None:
            return
        if env_step in self._shadow_compare_runs_per_step:
            return
        provider = getattr(self, "_env_state_provider", None)
        query_fn = getattr(provider, "query_environment_state", None)
        if query_fn is None:
            return
        scope_id = getattr(provider, "scope_id", "") or ""
        if not scope_id:
            return
        self._shadow_compare_runs_per_step.add(env_step)

        from Agent.environment_state import EnvironmentStateQuery

        # Settled common env-step horizon: only evidence strictly before the
        # current (in-flight) barrier step is compared.  Without a barrier
        # (unit fixtures / non-SAR) there is no settled horizon to fence at.
        horizon = env_step if self._barrier is not None else None

        legacy_view = {
            "scope_id": scope_id,
            "as_of_sequence": env_step,
            "memory_revision": env_step,
            "freshness": Freshness.FRESH.value,
            "payload": dict(payload),
        }
        try:
            # Canonical view for the SAME authenticated principal / scope.
            canonical = query_fn(
                EnvironmentStateQuery(
                    scope_id=scope_id,
                    viewer_role=getattr(provider, "viewer_role", "coordinator"),
                    viewer_id=getattr(provider, "viewer_id", "system"),
                    current_dispatch_id=getattr(provider, "current_dispatch_id", None),
                    temporal_cursor=0,
                    token_budget=1000,
                )
            )
        except Exception as exc:  # noqa: BLE001 - never break pre-LLM
            # Record the failure as a non-clean outcome so H2 evidence shows
            # the shadow path degraded instead of silently skipping.
            self._shadow_compare.mark_error(scope_id, exc)
            return
        self._shadow_compare.run(
            legacy_view=legacy_view,
            read_port_view=canonical,
            scope_id=scope_id,
            horizon=horizon,
        )

    # ── Phase 5: Continuity preparation ───────────────────────────────────

    def _try_snapshot_with_revision(self) -> tuple[int, dict[str, Any]]:
        """Atomically read (revision, snapshot) when the store supports it.

        Falls back to ``(0, snapshot())`` for mock or legacy stores without
        ``snapshot_with_revision()``.
        """
        if self._semantic_map is not None and hasattr(
            self._semantic_map, "snapshot_with_revision"
        ):
            return self._semantic_map.snapshot_with_revision()
        snap = self._semantic_map.snapshot() if self._semantic_map is not None else {}
        return 0, snap

    async def prepare_for_llm(self, llm_client: Any) -> None:
        """Async preparation before the next ``snapshot()`` call.

        In semantic mode:
        1. Atomically reads ``(revision, snapshot)`` from the semantic map.
        2. On first call (no baseline) establishes cache and returns.
        3. On actual revision change computes ``map_delta`` and may call
           ``map_summarizer.maybe_summarize()`` once.
        4. Same revision is a no-op.
        5. Failures are caught — the last successful summary is preserved.
        """
        if self._state_mode != "semantic" or self._semantic_map is None:
            return

        env_step = (
            getattr(self._barrier, "_step_counter", 0)
            if self._barrier is not None
            else 0
        )
        revision, snapshot = self._try_snapshot_with_revision()

        # Environment progress is independently meaningful runtime state.  Do
        # not let a stable map revision freeze the ContextManager version.
        self._runtime_version = max(self._runtime_version, env_step)

        # Same revision as last prepare — nothing to do
        if (
            self._previous_map_snapshot is not None
            and revision == self._semantic_revision
        ):
            self._last_version = env_step
            return

        # First observation: establish baseline (no diff, no summarizer)
        if self._previous_map_snapshot is None:
            self._previous_map_snapshot = snapshot
            self._previous_map_revision = revision
            self._semantic_revision = revision
            self._semantic_cached = snapshot
            self._last_map_delta = None
            if self._runtime_version == 0:
                self._runtime_version = env_step
            self._last_version = env_step
            return

        # Revision change: compute delta, bump runtime version
        from sar_orch.map.diff import MapDiffCalculator

        delta = MapDiffCalculator.diff(
            self._previous_map_snapshot,
            snapshot,
            base_revision=self._previous_map_revision or 0,
            revision=revision,
        )

        self._previous_map_snapshot = snapshot
        self._previous_map_revision = revision
        self._semantic_revision = revision
        self._semantic_cached = snapshot
        self._last_map_delta = delta
        self._runtime_version += 1
        self._last_version = env_step

        # Call summarizer once per revision (only when delta is not None)
        if delta is not None and self._map_summarizer is not None:
            try:
                summary = await self._map_summarizer.maybe_summarize(
                    llm_client=llm_client,
                    env_step=env_step,
                    map_revision=revision,
                    snapshot=snapshot,
                    map_delta=delta,
                )
                self._last_summary = summary
                self._last_summary_revision = revision
            except Exception:
                # Never propagate — keep the last successful summary
                pass

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Team status, task views, recent changes, and supervision are rebuilt
        on EVERY call (they are cheap). Only the semantic map snapshot is
        cached across calls within the same env step to avoid redundant
        serialization after the async prepare baseline is established. On
        refresh failure, returns the most recent snapshot
        with stale=True and refresh_error set; if no prior snapshot exists,
        returns an empty stale snapshot.

        When ``prepare_for_llm()`` has been called beforehand, the semantic
        cache and continuity fields (``map_revision``, ``map_delta``,
        ``map_summary``) reflect the latest prepared state.  For direct callers
        that never invoke ``prepare_for_llm``, the fields are populated with
        safe snapshot values (the current revision when available,
        ``delta=None``, and an empty summary).
        """
        try:
            env_step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )

            # Semantic map is expensive — cache across calls within same step
            if (
                env_step != self._last_version
                or self._semantic_cached is None
                # Snapshot-only callers have no prepare baseline.  Refresh the
                # atomic pair so a same-step observation is still visible.
                or (
                    self._state_mode == "semantic"
                    and self._previous_map_snapshot is None
                )
            ):
                if self._semantic_map is not None:
                    revision, snap = self._try_snapshot_with_revision()
                    self._semantic_cached = snap
                    self._semantic_revision = revision
                else:
                    self._semantic_cached = {}
                    self._semantic_revision = 0

            payload: dict[str, Any] = {
                "state_mode": self._state_mode,
                "mission_finished": (
                    self._barrier.is_finished() if self._barrier is not None else False
                ),
            }

            # Step budget
            if self._semantic_map is not None:
                payload["step_budget"] = self._semantic_map.get_step_budget()
            elif self._barrier is not None:
                max_steps = getattr(self._barrier.env, "task_timeout", 50)
                payload["step_budget"] = {
                    "current_step": env_step,
                    "max_steps": max_steps,
                    "remaining": max(0, max_steps - env_step),
                }
            else:
                payload["step_budget"] = {
                    "current_step": 0,
                    "max_steps": 0,
                    "remaining": 0,
                }

            if self._state_mode == "semantic":
                payload["semantic_summary"] = self._semantic_cached
                payload["team_status_summary"] = self._build_team_status()
                # Phase 5: continuity fields in semantic mode
                payload["map_revision"] = self._semantic_revision or 0
                payload["map_delta"] = self._last_map_delta
                payload["map_summary"] = self._last_summary
                payload["map_summary_revision"] = self._last_summary_revision
            elif self._state_mode == "oracle":
                payload["global_snapshot"] = (
                    self._barrier.get_env_snapshot()
                    if self._barrier is not None
                    else {}
                )
                # Oracle mode must NOT expose semantic continuity fields

            # Phase 2: Mission DAG + Physical Dispatches structured views
            payload["mission_dag_view"] = self._build_mission_dag_view()
            payload["physical_dispatches_view"] = self._build_physical_dispatches_view()

            # These are cheap — rebuild every call
            payload["task_status_view"] = self._build_task_status_view()
            payload["recent_changes"] = self._build_recent_changes()
            payload["supervision"] = self._build_supervision_view()

            # Runtime version: use _runtime_version if it has been advanced
            # by prepare_for_llm, otherwise env_step for backward compat.
            self._runtime_version = max(self._runtime_version, env_step)
            version = self._runtime_version

            snapshot = RuntimeState(
                version=version,
                env_step=env_step,
                observed_at=time.monotonic(),
                payload=payload,
                stale=False,
                refresh_error="",
            )
            self._last_version = env_step
            self._last_snapshot = snapshot
            self._run_shadow_compare(env_step, payload)
            return snapshot
        except Exception as exc:  # pragma: no cover - defensive fallback
            if self._last_snapshot is not None:
                return replace(
                    self._last_snapshot,
                    stale=True,
                    refresh_error=str(exc),
                )
            return RuntimeState(
                version=0,
                env_step=0,
                observed_at=time.monotonic(),
                payload={},
                stale=True,
                refresh_error=str(exc),
            )

    def _build_team_status(self) -> dict[str, Any]:
        """Build a team-level summary from the semantic map.

        Enriches each agent entry with position, inventory, task state, and
        capabilities from AgentRegistry (if available).  Positions and
        inventory are the Worker-derived semantic-map values only — the Barrier
        / simulator is never read in the online semantic path (H1-INV-1).
        """
        if self._semantic_map is None:
            return {
                "workers": [],
                "agent_summaries": [],
                "pending_requests": [],
                "recent_observations": [],
                "stale_entries": [],
                "conflicts": [],
            }
        snap = self._semantic_map.snapshot()
        agents = snap.get("agents", [])
        workers: list[dict[str, Any]] = []
        agent_summaries: list[str] = []

        for agent in agents:
            aid = agent.get("agent_id", "unknown")
            pos = agent.get("last_position")
            inv = agent.get("inventory")

            task_id = agent.get("current_task_id", "")
            task_state = agent.get("task_state", "UNKNOWN")

            summary = (
                f"{aid}: at {pos} | inventory={inv} | task={task_id} ({task_state})"
            )

            enriched = dict(agent)

            # Inject static capabilities from AgentRegistry (H1: AgentRegistry
            # is the static capability owner; it never provides battery /
            # position / inventory dynamic scene fields).
            capabilities: list[str] = []
            if self._agent_registry is not None:
                try:
                    info = self._agent_registry.get(aid)
                    capabilities = list(info.capabilities)
                except Exception:
                    pass
            enriched["capabilities"] = capabilities
            if capabilities:
                summary += f" | capabilities={', '.join(capabilities)}"

            enriched["formatted_summary"] = summary
            workers.append(enriched)
            agent_summaries.append(summary)

        return {
            "workers": workers,
            "agent_summaries": agent_summaries,
            "pending_requests": [],
            "recent_observations": snap.get("recent_observations", []),
            "stale_entries": snap.get("stale_entries", []),
            "conflicts": snap.get("conflicts", []),
        }

    def _build_task_status_view(self) -> list[dict[str, Any]]:
        """Build a structured view of active/running tasks.

        If no TaskStore is attached (e.g., before agentic execution begins or
        during route planning), returns an empty list.
        """
        if self._task_store is None:
            return []

        views: list[dict[str, Any]] = []
        runtime = self._runtime or getattr(self._task_store, "_runtime", None)
        if runtime is not None:
            entries = [
                (
                    dispatch.dispatch_id,
                    self._task_store.get_node(dispatch.logical_node_id),
                    dispatch.worker_task_id or "",
                    dispatch.worker_id,
                )
                for dispatch in runtime.dispatches.values()
            ]
        else:
            entries = [
                (
                    node.task_id,
                    node,
                    self._task_store._dispatch_to_worker.get(node.task_id, ""),
                    node.worker_id or "",
                )
                for node in self._task_store.get_plan()
            ]

        for dispatch_id, node, worker_task_id, worker_id in entries:
            worker_id = worker_id or ""

            state_record = (
                self._event_store.get_task_state(dispatch_id, worker_task_id or None)
                if self._event_store is not None
                else {}
            )
            # Physical runtime state is canonical. EventStore remains a
            # diagnostic/result evidence source and must not override it.
            state = (
                runtime.get_dispatch(dispatch_id).state.value
                if runtime is not None
                else state_record.get("state", "UNKNOWN")
            )
            latest_result = (
                (node.result if node is not None else None)
                or (
                    runtime.get_dispatch(dispatch_id).result
                    if runtime is not None
                    and runtime.get_dispatch(dispatch_id) is not None
                    else None
                )
                or state_record.get("text", "")
            )
            help_request = ""
            if state == "INPUT_REQUIRED":
                help_request = state_record.get("text", "")

            view: dict[str, Any] = {
                "dispatch_id": dispatch_id,
                "worker_task_id": worker_task_id,
                "worker_id": worker_id,
                "state": state,
                "latest_result": latest_result,
                "help_request": help_request,
                "updated_at": state_record.get("updated_at", ""),
                "acknowledged_by_coordinator": (
                    node is not None and node.state != "pending"
                ),
            }

            # Merge supervision fields from SupervisionStateStore (Phase 3)
            if self._supervision_state_store is not None:
                sup = self._supervision_state_store.get(dispatch_id)
                if sup is not None:
                    view["supervision_state"] = sup.supervision_state
                    view["last_contact_at"] = sup.last_contact_at
                    view["last_progress_at"] = sup.last_progress_at
                    view["last_state_change_at"] = sup.last_state_change_at
                    view["active_alerts"] = dict(sup.active_alerts)
                    view["terminal"] = sup.terminal

            views.append(view)
        return views

    def _build_supervision_view(self) -> dict[str, Any]:
        """Build a supervision summary from SupervisionStateStore.

        Includes unacknowledged actionable events, active alerts, and watchdog
        health. Empty if no supervision store is attached.
        """
        if self._supervision_state_store is None:
            return {"alerts": [], "unacknowledged_events": []}

        alerts: list[dict[str, Any]] = []
        unacknowledged: list[dict[str, Any]] = []
        for state in self._supervision_state_store.list_states():
            if state.active_alerts:
                alerts.append(
                    {
                        "dispatch_id": state.dispatch_id,
                        "worker_id": state.worker_id,
                        "supervision_state": state.supervision_state,
                        "active_alerts": dict(state.active_alerts),
                        "last_contact_at": state.last_contact_at,
                        "last_progress_at": state.last_progress_at,
                        "terminal": state.terminal,
                    }
                )
            unacknowledged.extend(state.unacknowledged_events)

        return {
            "alerts": alerts,
            "unacknowledged_events": unacknowledged,
        }

    def _build_mission_dag_view(self) -> list[dict[str, Any]]:
        """Build structured Mission DAG view from MissionGraph (if attached).

        Each entry contains: logical_id, state, participant_ids, depends_on,
        objective, assignments, dispatch_count, team_id, failure_reason.
        Empty list if no TaskStore or no MissionGraph nodes.
        """
        if self._task_store is None:
            return []
        dag: list[dict[str, Any]] = []
        # Iterate logical IDs in stable sort order.
        for logical_id in sorted(self._task_store.mission_node_ids):
            node = self._task_store.get_mission_node(logical_id)
            if node is None:
                continue
            dag.append(
                {
                    "logical_id": node.logical_id,
                    "state": node.state,
                    "participant_ids": list(node.participant_ids),
                    "depends_on": list(node.depends_on),
                    "objective": node.objective,
                    "assignments": dict(node.assignments),
                    "status": node.status,
                    "dispatch_count": len(node.dispatch_ids),
                    "team_id": node.team_id or "",
                    "failure_reason": node.failure_reason or "",
                    "terminal_workers": sorted(node.terminal_workers),
                }
            )
        return dag

    def _build_physical_dispatches_view(self) -> list[dict[str, Any]]:
        """Build structured view of physical dispatch records.

        Each entry contains: dispatch_id, logical_node_id, worker_id,
        worker_task_id, state, artifact_preview, result_preview.
        Empty list if no runtime or no dispatches.
        """
        if self._runtime is None:
            return []
        views: list[dict[str, Any]] = []
        for dispatch_id, dispatch in sorted(
            self._runtime.dispatches.items(), key=lambda kv: kv[0]
        ):
            views.append(
                {
                    "dispatch_id": dispatch.dispatch_id,
                    "logical_node_id": dispatch.logical_node_id,
                    "worker_id": dispatch.worker_id,
                    "worker_task_id": dispatch.worker_task_id or "",
                    "state": dispatch.state.value,
                    "artifact_preview": (
                        (dispatch.artifact[:80] + "...")
                        if dispatch.artifact and len(dispatch.artifact) > 80
                        else dispatch.artifact or ""
                    ),
                    "result_preview": (
                        str(dispatch.result)[:80] if dispatch.result is not None else ""
                    ),
                }
            )
        return views

    def _build_recent_changes(self) -> list[str]:
        """Summarize recent observations as human-readable change lines.

        Merges observations from both EventStore (push-callback events) and
        SemanticMapStore (all ingested observations), deduplicating by string
        content.
        """
        all_obs: list[dict[str, Any]] = []
        if self._event_store is not None:
            all_obs.extend(self._event_store.get_recent_observations(limit=5))
        if self._semantic_map is not None:
            all_obs.extend(self._semantic_map.get_recent_observations(limit=5))

        lines: list[str] = []
        seen: set[str] = set()
        for obs in all_obs:
            reporter = obs.get("reporter", "unknown")
            obj_type = obs.get("object_type", "object")
            name = obs.get("name", "unknown")
            step = obs.get("step", 0)
            note = obs.get("note", "")
            if note:
                line = f"{reporter} observed {obj_type} {name} at step {step}: {note}"
            else:
                line = f"{reporter} observed {obj_type} {name} at step {step}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        return lines[-5:]

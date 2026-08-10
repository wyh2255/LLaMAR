from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

from Agent.router_agent.state_provider import RuntimeState
from a2a.coordinator.team_status_auth import TeamStatusProof

if TYPE_CHECKING:
    from sar_orch.barrier import SARBarrier
    from a2a.worker.mailbox_store import WorkerMailboxStore
    from a2a.worker.team_state import WorkerTeamState

#: Typed /environment-state admission reasons the coordinator can return.
_ENVIRONMENT_STATE_REASON_TOKENS = (
    "environment_state_unauthorized",
    "environment_state_unknown_worker_task",
    "environment_state_unknown_worker",
    "environment_state_no_active_dispatch",
    "environment_state_scope_mismatch",
    "environment_state_worker_mismatch",
    "environment_state_dispatch_mismatch",
    "environment_state_unavailable",
)

#: The single typed reason that is an inherent startup-ordering transient: the
#: coordinator has not yet bound this worker's server-issued task id when the
#: worker's first /environment-state fetch races the post-acceptance
#: ``register_worker_task_id``.  The binding lands milliseconds later, so this
#: condition resolves by waiting.  Every other failure (bad/expired/replayed
#: proof, scope/worker/dispatch mismatch, unknown worker, terminal dispatch,
#: unparseable) stays fail-closed and latches the read_port→legacy rollback.
_TRANSIENT_ADMISSION_REASON = "environment_state_unknown_worker_task"

#: UNAVAILABLE reason produced by ``query_environment_state`` while a transient
#: admission deferral is pending (no cached view, rollback NOT latched).  The
#: worker context renderer treats this reason as non-latching so a transient
#: startup ordering never falls back to legacy permanently.
PENDING_ADMISSION_REASON = "environment_state_pending_admission"


def _extract_environment_state_reason(exc: Exception) -> str:
    """Return the coordinator's typed /environment-state reason token, or "".

    ``httpx.HTTPStatusError`` carries the JSON body whose ``detail`` is the
    typed reason (FastAPI ``{"detail": ...}``).  When the body cannot be parsed
    as JSON (proxy / non-FastAPI 403, empty body, streaming truncation), the
    raw response BODY text and finally the exception message are scanned for a
    known reason token, so a startup binding-ordering 403 that could not be
    JSON-decoded still defers instead of latching.  An unknown reason yields ""
    so callers keep the fail-closed default.
    """
    response = getattr(exc, "response", None)
    candidates: list[str] = []
    if response is not None:
        try:
            detail = response.json().get("detail", "")
            if detail:
                candidates.append(str(detail))
        except Exception:  # noqa: BLE001,S110 - best-effort body parse
            pass
        try:
            body = response.text or ""
            if body and body.strip():
                candidates.append(body)
        except Exception:  # noqa: BLE001,S110 - best-effort body read
            pass
    candidates.append(str(exc))
    for candidate in candidates:
        for token in _ENVIRONMENT_STATE_REASON_TOKENS:
            if token in candidate:
                return token
    return ""


class SARWorkerStateProvider:
    """Read-only runtime state provider for SAR Workers.

    Reads position, inventory, step, and local observations directly from
    SARBarrier (no step consumption). Optionally fetches global semantic map
    summary via HTTP for known_fires / known_persons enrichment.

    Supports optional mailbox and team_state dependencies for Phase 3 peer
    messaging. When attached, the version tuple includes mailbox version and
    team generation so that context refreshes when mail arrives or the team
    changes, even within the same env step.

    Phase 5: authenticated /team-status via HMAC proof bound to worker ID.
    Cache key includes team_generation so same-step team transitions trigger
    a refresh.
    """

    def __init__(
        self,
        barrier: "SARBarrier | None" = None,
        agent_idx: int = 0,
        semantic_map_url: str | None = None,
        mailbox: "WorkerMailboxStore | None" = None,
        team_state: "WorkerTeamState | None" = None,
        coordinator_id: str = "Coordinator",
        coordinator_secret: bytes | None = None,
        # Phase 4: authenticated read-port provider (no global-map direct read).
        environment_state_url: str | None = None,
        memory_read_mode: str = "read_port",
        # Phase 4 (H2): read_port -> legacy rollback audit + request budget.
        # ``token_limit`` mirrors the worker ContextManager token budget so the
        # /environment-state HTTP request carries the same nonzero budget as the
        # local read-port query (design §6).  ``log_dir`` hosts the redacted
        # ``memory_rollout_audit.ndjson`` exactly like the coordinator.
        log_dir: str | None = None,
        token_limit: int = 80000,
        # Phase 4 (H2): admission-aware retry for the startup dispatch-binding
        # race.  The worker's first /environment-state fetch can race the
        # coordinator's post-acceptance ``register_worker_task_id`` and receive
        # a typed ``environment_state_unknown_worker_task`` 403 that resolves
        # milliseconds later.  The fetch retries this bounded number of times
        # (backing off ``admission_retry_delay`` per attempt) before it defers
        # without latching the read_port→legacy rollback.
        admission_retry_limit: int = 5,
        admission_retry_delay: float = 0.05,
    ) -> None:
        self._barrier = barrier
        self._agent_idx = agent_idx
        self._semantic_map_url = (
            semantic_map_url.rstrip("/") if semantic_map_url else None
        )
        self._mailbox = mailbox
        self._team_state = team_state
        self._coordinator_id = coordinator_id
        self._coordinator_secret = coordinator_secret
        self._memory_read_mode = memory_read_mode
        self._log_dir = Path(log_dir) if log_dir else None
        self._token_limit = token_limit
        self._admission_retry_limit = max(0, int(admission_retry_limit))
        self._admission_retry_delay = max(0.0, float(admission_retry_delay))
        self._last_version: int | tuple = -1
        self._last_snapshot: RuntimeState | None = None

        self._agent_name: str = ""
        self._team_status_url: str | None = None
        self._cached_teammates: list[dict] = []
        self._last_team_status_cache_key: tuple = ()
        self._cached_team_status_revision: int = -1

        # Phase 4: authenticated /environment-state client cache.
        self._environment_state_url = (
            environment_state_url.rstrip("/") if environment_state_url else None
        )
        self._cached_environment_state_view: dict | None = None
        self._env_state_client: Any | None = None
        self._scope_id: str = ""
        self._viewer_role: str = "worker"
        self._viewer_id: str = ""
        self._current_dispatch_id: str | None = None
        # Phase 4 (H2): server-issued, opaque, task-bound A2A task id that
        # binds this worker's identity on /environment-state.  Set by the agent
        # adapter when the worker starts executing its dispatched task.
        self._worker_task_id: str = ""

        # Phase 4 (H2): transient admission deferral.  Set when the very first
        # /environment-state fetch races the coordinator's dispatch-binding and
        # receives a typed ``environment_state_unknown_worker_task`` 403 that
        # resolves milliseconds later.  While set (and no cached view exists),
        # ``query_environment_state`` reports ``environment_state_pending_admission``
        # — a distinct UNAVAILABLE reason the context renderer treats as
        # non-latching, so startup ordering never trips the permanent
        # read_port→legacy rollback.  Cleared on the first successful fetch.
        self._transient_deferred = False

        # Phase 4 (H2): id of the worker task whose /environment-state binding
        # was last confirmed by a successful fetch.  While empty / different
        # from ``_worker_task_id``, the provider is inside the startup
        # dispatch-binding window for the current task, so a stale-id fetch can
        # resolve the PREVIOUS task's now-terminal dispatch
        # (``environment_state_no_active_dispatch``).  That condition defers
        # (never latches) until the current task's binding is confirmed once.
        self._last_successful_fetch_task_id: str = ""

        # Phase 4 (H2): read_port -> legacy rollback latch + redacted audit,
        # mirroring the coordinator's MemoryRolloutController so a worker
        # provider/HTTP/ACL failure latches legacy exactly once per run.  The
        # audit redactor is bound to the coordinator secret so a failure detail
        # that ever embeds the shared proof / secret is redacted before it is
        # persisted.
        self._memory_rollout: Any | None = None
        if self._memory_read_mode == "read_port":
            from sar_orch.environment_state_provider import (
                MemoryRolloutController,
                RolloutAuditWriter,
            )

            audit_path = None
            if self._log_dir:
                audit_path = str(self._log_dir / "memory_rollout_audit.ndjson")
            self._memory_rollout = MemoryRolloutController(
                audit=RolloutAuditWriter(audit_path, secret=self._coordinator_secret)
            )

        if self._semantic_map_url:
            self._team_status_url = self._semantic_map_url

    def set_worker_task_id(self, worker_task_id: str) -> None:
        """Bind this provider to the server-issued A2A task id (read_port).

        The coordinator resolves ``worker_task_id → dispatch → worker_id``
        server-side, so a shared-secret proof alone can never impersonate
        another worker.
        """
        self._worker_task_id = worker_task_id or ""

    def set_environment_state_client(self, client) -> None:
        """Inject an authenticated /environment-state HTTP client (Phase 4).

        The client must be a callable ``async (query) -> EnvironmentStateView``
        or expose ``fetch(query)``.  Used in read_port mode so the worker never
        constructs a global-map direct-read view.
        """
        self._env_state_client = client

    # ── Phase 4 (H2) rollback surface (read_port -> legacy) ───────────────

    def rollout_rolled_back(self) -> bool:
        """True once a read-port failure latched the legacy fallback."""
        return bool(getattr(self._memory_rollout, "rolled_back", False))

    def rollout_active(self) -> bool:
        """True while read-port rendering is still trusted (not rolled back)."""
        return getattr(self._memory_rollout, "active", True)

    def rollback_environment_state(self, reason: str) -> bool:
        """Trigger the read_port -> legacy rollback latch (once) + redacted audit.

        Returns True on the first transition; subsequent calls are no-ops so
        exactly one audit record is written per process/run.  The audit carries
        non-secret correlation metadata (opaque ``worker_task_id`` / agent
        name) so the first failure can be traced to the failing worker without
        leaking sensitive values.  Canonical DB / outbox are never touched.
        """
        controller = getattr(self, "_memory_rollout", None)
        if controller is None:
            return False
        scope_id = self.scope_id or ""
        controller.set_scope(scope_id)
        return controller.rollback(
            reason,
            correlation={
                "actor_id": self._agent_name,
                "worker_task_id": self._worker_task_id,
            },
        )

    def _trigger_read_port_rollback(self, reason: str) -> None:
        """Latch the read_port→legacy rollback on this provider (once)."""
        self.rollback_environment_state(reason)

    def _read_port_token_budget(self) -> int:
        """Token budget for the read-port state block (design §6).

        Mirrors ``ContextManager._read_port_token_budget`` (available =
        token_limit - reserved_completion floor 1024) so the HTTP request
        carries the same nonzero budget the worker ContextManager would pass
        to a local read-port query.  Kept provider-side to avoid a circular
        context<->provider import while staying budget-consistent.
        """
        return max(0, self._token_limit - 1024)

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def viewer_role(self) -> str:
        return self._viewer_role

    @property
    def viewer_id(self) -> str:
        return self._viewer_id

    @property
    def current_dispatch_id(self) -> str | None:
        return self._current_dispatch_id

    async def fetch_environment_state_async(self) -> None:
        """Fetch the authenticated /environment-state view from the coordinator.

        In read_port mode this replaces the global-map direct-read view.  The
        request carries a proof bound to this worker's **server-issued opaque
        ``worker_task_id``** — never a caller-selected worker id — so the
        coordinator can resolve identity server-side via the task binding and
        apply the provider ACL.

        The first fetch can race the coordinator's post-acceptance
        ``register_worker_task_id`` and receive a typed
        ``environment_state_unknown_worker_task`` 403 that resolves
        milliseconds later.  That single transient admission condition is
        retried (bounded) and, if it persists, deferred WITHOUT latching the
        read_port→legacy rollback.  Genuine authorization failures still latch
        fail-closed exactly as before.
        """
        if not self._agent_name:
            return
        if self._env_state_client is not None:
            await self._fetch_via_injected_client(self._env_state_client)
            return
        if not self._environment_state_url:
            return
        if not self._worker_task_id:
            # No dispatched task → no task-bound identity → cannot authenticate.
            return
        import httpx

        from a2a.coordinator.team_status_auth import TeamStatusProof

        # A fresh proof is minted per attempt (random nonce + timestamp) so a
        # bounded retry of a transient admission 403 is never mistaken for a
        # nonce replay by the coordinator's UsedNonceStore.
        async def _post() -> Any:
            proof = (
                TeamStatusProof.generate(self._coordinator_secret, self._worker_task_id)
                if self._coordinator_secret
                else ""
            )
            payload = {
                "worker_task_id": self._worker_task_id,
                "proof": proof,
                "temporal_cursor": 0,
                "token_budget": self._read_port_token_budget(),
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._environment_state_url}/environment-state",
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()

        attempt = 0
        while True:
            try:
                view = await _post()
                self._cache_environment_state_view(view)
                self._clear_transient_deferral()
                self._record_successful_fetch()
                return
            except Exception as exc:  # noqa: BLE001 - network boundary, never break pre-LLM
                if self._is_transient_admission_failure(exc):
                    if attempt < self._admission_retry_limit:
                        attempt += 1
                        await asyncio.sleep(self._admission_retry_delay * attempt)
                        continue
                    # Bounded retry exhausted while the condition is still the
                    # transient binding-ordering one: defer, never latch legacy.
                    self._defer_transient_admission(exc)
                    return
                # Provider / HTTP / ACL failure: latch the read_port→legacy
                # rollback (once) and write the redacted audit so the worker
                # never renders canonical state it could not authenticate.
                self._trigger_read_port_rollback(f"http_error: {exc}")
                import logging

                logging.getLogger(__name__).debug(
                    "environment-state fetch failed for %s: %s",
                    self._agent_name,
                    exc,
                )
                return

    async def _fetch_via_injected_client(self, client: Any) -> None:
        """Fetch through an injected /environment-state client (test seam).

        Applies the same admission classification as the production HTTP path:
        a transient binding-ordering failure defers (never latches legacy);
        every other failure latches fail-closed.
        """
        try:
            view = await client.fetch(query_viewer=self._agent_name)
        except Exception as exc:  # noqa: BLE001 - never break pre-LLM
            if self._is_transient_admission_failure(exc):
                self._defer_transient_admission(exc)
            else:
                self._trigger_read_port_rollback(f"injected_client_error: {exc}")
                import logging

                logging.getLogger(__name__).debug(
                    "environment-state client fetch failed for %s: %s",
                    self._agent_name,
                    exc,
                )
            return
        if view is None:
            # No authenticated projection → fail closed, never reuse stale.
            self._trigger_read_port_rollback("environment_state_not_fetched")
            return
        self._cache_environment_state_view(view)
        self._clear_transient_deferral()
        self._record_successful_fetch()

    def _is_transient_admission_failure(self, exc: Exception) -> bool:
        """True only for startup binding-ordering conditions that resolve by
        waiting — never a genuine authorization / identity failure.

        The coordinator returns ``environment_state_unknown_worker_task`` when
        it has not yet bound this worker's server-issued task id — the exact
        race where the worker's first fetch beats the coordinator's
        post-acceptance ``register_worker_task_id``.  The binding lands
        milliseconds later, so only this reason is retried / deferred.

        A fetch can also resolve the PREVIOUS task's now-terminal dispatch
        (``environment_state_no_active_dispatch``) while the current task's
        re-dispatch binding has not landed yet — the same startup ordering
        window across a task boundary.  Until the current task's binding is
        confirmed by one successful fetch, that condition defers too.  Every
        other failure (bad/expired/replayed proof, scope/worker/dispatch
        mismatch, unknown worker, unparseable) is treated as genuine and
        latches fail-closed.
        """
        reason = _extract_environment_state_reason(exc)
        if reason == _TRANSIENT_ADMISSION_REASON:
            return True
        if reason == "environment_state_no_active_dispatch":
            return self._last_successful_fetch_task_id != self._worker_task_id
        return False

    def _record_successful_fetch(self) -> None:
        """Mark the current worker task's binding as confirmed by a fetch."""
        self._last_successful_fetch_task_id = self._worker_task_id

    def _defer_transient_admission(self, exc: Exception) -> None:
        """Record a pending-admission deferral WITHOUT latching the rollback.

        The coordinator simply has not bound the task yet; a later pre_llm
        fetch will succeed.  ``query_environment_state`` reports the distinct
        ``environment_state_pending_admission`` reason so the worker context
        renderer falls back to legacy for this request without tripping the
        permanent rollback latch.
        """
        self._transient_deferred = True
        import logging

        logging.getLogger(__name__).debug(
            "environment-state admission pending for %s: %s",
            self._agent_name,
            exc,
        )

    def _clear_transient_deferral(self) -> None:
        self._transient_deferred = False

    def _cache_environment_state_view(self, view) -> bool:
        """Validate freshness and cache the authenticated projection.

        Only ``FRESH`` views are cached.  An UNAVAILABLE / STALE response or a
        non-fresh injected view latches the read_port→legacy rollback (once)
        and writes the redacted audit — never mixing canonical and legacy
        truth in one request.  Returns True when a fresh view was cached.
        """
        from Agent.environment_state import FRESHNESS_SECTION, Freshness

        if isinstance(view, dict):
            sections = dict(view.get("sections") or {})
            freshness = view.get("freshness")
            reason = view.get("reason", "")
        else:
            sections = dict(getattr(view, "sections", None) or {})
            freshness = getattr(view, "freshness", None)
            reason = getattr(view, "reason", "")

        # Fail closed on a missing/null freshness: a projection that does not
        # claim to be FRESH is never cached as fresh truth — it latches the
        # read_port→legacy rollback and writes the redacted audit.
        if freshness is None or freshness != Freshness.FRESH:
            state = "missing" if freshness is None else str(freshness)
            self._trigger_read_port_rollback(
                f"environment_state_not_fresh: {state}: {reason}"
            )
            return False

        self._scope_id = str(sections.get(FRESHNESS_SECTION, {}).get("scope_id", ""))
        self._viewer_id = self._agent_name
        self._cached_environment_state_view = sections
        return True

    def query_environment_state(self, query):
        """Provider-ACL query used by the worker ContextManager in read_port.

        Uses the last authenticated /environment-state projection; a missing
        view or an already-latched rollback is never silently reused — it
        renders UNAVAILABLE.
        """
        from Agent.environment_state import EnvironmentStateView, Freshness

        if self.rollout_rolled_back():
            return EnvironmentStateView(
                Freshness.UNAVAILABLE, reason="read_port_rolled_back_to_legacy"
            )
        if self._cached_environment_state_view is None:
            if self._transient_deferred:
                # Startup binding-ordering deferral: the coordinator has not
                # yet bound this worker's task id.  Distinct from a genuine
                # not-fetched failure so the renderer can skip latching legacy.
                return EnvironmentStateView(
                    Freshness.UNAVAILABLE, reason=PENDING_ADMISSION_REASON
                )
            return EnvironmentStateView(
                Freshness.UNAVAILABLE, reason="environment_state_not_fetched"
            )
        return EnvironmentStateView(
            Freshness.FRESH,
            source_revision=0,
            sections=dict(self._cached_environment_state_view),
            evidence=[],
        )

    async def fetch_team_status_async(self) -> None:
        """Fetch team status from coordinator.

        Cache key is ``(env_step, team_generation, known_server_revision)``.
        A change in any component forces a re-fetch.  The server's returned
        ``team_partition_revision`` is stored and becomes part of the cache
        key on the next call, so a known-stale cache is always refreshed.
        """
        if not self._team_status_url or not self._agent_name:
            return
        env_step = (
            getattr(self._barrier, "_step_counter", 0)
            if self._barrier is not None
            else 0
        )
        team_gen = (
            self._team_state.generation
            if self._team_state is not None
            else ("", -1, False)
        )
        known_revision = self._cached_team_status_revision
        cache_key = (env_step, team_gen, known_revision)
        if cache_key == self._last_team_status_cache_key:
            return
        try:
            import httpx

            proof = (
                TeamStatusProof.generate(
                    self._coordinator_secret,
                    self._agent_name,
                )
                if self._coordinator_secret
                else ""
            )

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._team_status_url}/team-status",
                    params={
                        "agent_id": self._agent_name,
                        "proof": proof,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if "teammates" in data:
                    self._cached_teammates = data.get("teammates", [])
                elif "team_id" in data:
                    self._cached_teammates = []
                server_revision = data.get("team_partition_revision", -1)
                self._cached_team_status_revision = server_revision
                self._last_team_status_cache_key = cache_key
        except Exception:
            pass

    def force_refresh_team_status(self) -> None:
        """Force the next ``fetch_team_status_async`` call to re-fetch.

        Call when external knowledge (e.g. a TEAM_UPDATE envelope with a
        known newer partition revision) indicates the cached peers are
        stale but the generation tuple hasn't changed yet.
        """
        self._last_team_status_cache_key = ()

    def snapshot(self, context_id: str | None = None) -> RuntimeState:
        """Return a fresh runtime state snapshot.

        Version is a tuple ``(env_step, mailbox_version, team_generation)``
        so that context refreshes when any of the three sources changes.
        If no mailbox/team_state is attached, the corresponding component
        defaults to 0 / ``("", -1, False)`` (the null team state).

        Exceptions from mailbox or team_state propagate to the outer
        catch, resulting in a stale snapshot with ``refresh_error`` set.
        """
        try:
            env_step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
            mailbox_ver = self._mailbox.version if self._mailbox is not None else 0
            team_gen = (
                self._team_state.generation
                if self._team_state is not None
                else ("", -1, False)
            )
            version = (env_step, mailbox_ver, team_gen)
            if version == self._last_version and self._last_snapshot is not None:
                return self._last_snapshot

            observed_at = time.monotonic()
            age_ms = 0.0
            if self._last_snapshot is not None:
                age_ms = (observed_at - self._last_snapshot.observed_at) * 1000.0

            payload: dict[str, Any] = {}
            payload["age_ms"] = age_ms

            if self._barrier is not None:
                try:
                    agent = self._barrier.env.controller.get("agents", self._agent_idx)
                    pos = agent.get_position()
                    payload["position"] = (pos[0], pos[1], pos[2])

                    inventory = self._barrier.env.controller.get_inventory(
                        self._agent_idx
                    )
                    payload["inventory"] = inventory

                    obs = self._barrier.get_current_obs(self._agent_idx)
                    payload["known_fires"] = self._extract_fires_from_obs(obs)
                    payload["known_persons"] = self._extract_persons_from_obs(obs)
                except Exception:
                    pass

            payload["step"] = env_step
            payload["mission_status"] = (
                ("complete" if self._barrier.is_finished() else "in_progress")
                if self._barrier is not None
                else "in_progress"
            )

            if self._mailbox is not None:
                payload["mailbox_summary"] = self._mailbox.summary(
                    coordinator_id=self._coordinator_id
                )

            if self._team_state is not None:
                ts = self._team_state.current()
                if ts is not None:
                    payload["team_summary"] = {
                        "team_id": ts.team_id,
                        "epoch": ts.epoch,
                        "members": list(ts.members),
                        "coordinator_id": ts.coordinator_id,
                    }

            payload["team_coordination"] = {
                "teammates": self._cached_teammates,
                "teammates_count": len(self._cached_teammates),
            }

            snapshot = RuntimeState(
                version=version,
                env_step=env_step,
                observed_at=observed_at,
                payload=payload,
                stale=False,
                refresh_error="",
            )
            self._last_version = version
            self._last_snapshot = snapshot
            return snapshot
        except Exception as exc:
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

    def _extract_fires_from_obs(self, obs: str) -> list[dict[str, Any]]:
        fires: list[dict[str, Any]] = []
        if not obs:
            return fires
        for line in obs.split("\n"):
            line = line.strip()
            if "fire" in line.lower() or "region" in line.lower():
                fires.append({"description": line[:200]})
        return fires

    def _extract_persons_from_obs(self, obs: str) -> list[dict[str, Any]]:
        persons: list[dict[str, Any]] = []
        if not obs:
            return persons
        for line in obs.split("\n"):
            line = line.strip()
            if "person" in line.lower():
                persons.append({"description": line[:200]})
        return persons

"""Coordinator 主服务 - FastAPI + WebSocket，集成 A2A Server。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import FileResponse
from pydantic import BaseModel

from a2a.coordinator.a2a_server import create_coordinator_a2a_server
from a2a.coordinator.agent_registry import AgentRegistry, AgentInfo, AgentStatus
from a2a.coordinator.router import RouterAgent
from a2a.coordinator.worker_registry import WorkerRegistry
from a2a.coordinator.task_logger import TaskLogger
from a2a.coordinator.task_queue import (
    TaskQueue,
    TaskNotFoundError,
    InvalidStatusTransitionError,
)
from a2a.coordinator.event_store import event_store
from a2a.coordinator.mesh_guide import MeshGuide, AgentNotFoundError
from a2a.coordinator.routes import health, workers
from a2a.coordinator.supervision_state_store import SupervisionStateStore
from sar_orch.map_agent import mount_to_fastapi as mount_map_agent_mcp
from a2a.coordinator.task_watchdog import TaskWatchdog, WatchdogConfig
from a2a.coordinator.mission_runtime import MissionRuntimeManager
from a2a.coordinator.team_partition_service import TeamPartitionService
from a2a.coordinator.team_status_auth import TeamStatusProof, UsedNonceStore
from a2a.shared.server_lifecycle import (
    shutdown_a2a_active_tasks,
    shutdown_uvicorn_server,
)
from a2a.shared.types import (
    DistributedTask,
    TaskStatus,
    WS_REGISTER,
    WS_HEARTBEAT,
    WS_TASK_PROGRESS,
    WS_CANCEL_TASK,
    WS_RELAY_A2A,
)


logger = logging.getLogger(__name__)

# Push notification artifact text cache
# Worker may push multiple times (artifact + status), need to aggregate
_push_artifact_cache: dict[str, list[str]] = {}


def _extract_worker_data_blocks(text: str) -> list[dict[str, Any]]:
    marker = "[DATA]"
    if marker not in text:
        return []
    blocks = []
    for chunk in text.split(marker)[1:]:
        raw = chunk.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_observation_from_status_text(text: str) -> dict[str, Any] | None:
    """Legacy: extract single observation from a report_observation tool result."""
    for block in _extract_worker_data_blocks(text):
        if (
            block.get("ev") != "tool_result"
            or block.get("tool_name") != "report_observation"
            or not block.get("success")
        ):
            continue
        content = block.get("content") or ""
        try:
            observation = json.loads(content)
        except json.JSONDecodeError:
            return None
        return observation if isinstance(observation, dict) else None
    return None


def _extract_observations_with_provenance(
    text: str,
) -> list[tuple[dict[str, Any], str]]:
    """Extract (observation, source class) pairs from ``[DATA]`` tool blocks.

    ``worker_sensor_tool`` tags structured tool-result observations;
    ``worker_observation`` tags legacy ``report_observation`` blocks.  Both are
    allowlisted online sources (H1 card §3.1) and feed the canonical projection
    reducer from the authenticated callback producer path.
    """
    results: list[tuple[dict[str, Any], str]] = []
    seen_keys: set[str] = set()

    for block in _extract_worker_data_blocks(text):
        if block.get("ev") != "tool_result" or not block.get("success"):
            continue

        # New format: structured_data with observations list
        structured = block.get("structured_data")
        if isinstance(structured, dict):
            obs_list = structured.get("observations", [])
            if isinstance(obs_list, list):
                for obs in obs_list:
                    if isinstance(obs, dict):
                        dedup_key = f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
                        if dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            results.append((obs, "worker_sensor_tool"))

        # Legacy format: report_observation with JSON content
        if block.get("tool_name") == "report_observation":
            content = block.get("content") or ""
            try:
                obs = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obs, dict):
                dedup_key = (
                    f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
                )
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    results.append((obs, "worker_observation"))

    return results


def _extract_auto_observations(text: str) -> list[dict[str, Any]]:
    """Extract auto-reported observations from structured_data in tool result blocks.

    Handles both:
    - New format: any tool_result with structured_data.observations list
    - Legacy format: report_observation tool_result with JSON content
    """
    return [obs for obs, _ in _extract_observations_with_provenance(text)]


def _environment_state_view_to_json(view) -> dict[str, Any]:
    """Serialize an ``EnvironmentStateView`` for the ``/environment-state`` API.

    The provider has already applied ACL filtering; this is pure serialization
    (renderer never performs ACL).
    """
    from Agent.environment_state import FRESHNESS_SECTION

    return {
        "freshness": view.freshness.value,
        "reason": view.reason,
        "source_revision": view.source_revision,
        "sections": dict(view.sections),
        "evidence": list(view.evidence),
        "freshness_section_key": FRESHNESS_SECTION,
    }


class CreateTaskRequest(BaseModel):
    task_id: str | None = None
    prompt: str
    task_type: str = "agent"


class CoordinatorServer:
    """Coordinator 主服务。"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        config_path: str | None = None,
        # --- RouterAgent 注入参数 ---
        prompts_dir: str | None = None,
        tools_dir: str | None = None,
        extra_tools: list | None = None,
        skills_dir: str | None = None,
        log_dir: str | None = None,
        router_model: str = "claude-opus-4-5",
        router_max_steps: int = 15,
        router_temperature: float = 0.7,
        router_provider: str = "anthropic",
        router_api_base: str = "https://api.anthropic.com",
        router_api_key_env: str = "ANTHROPIC_API_KEY",
        # --- VerifierAgent 注入参数 ---
        verifier_model: str | None = None,  # None = 使用 router_model
        verifier_max_steps: int = 10,
        verifier_temperature: float = 0.3,
        verifier_enabled: bool = True,  # False = 跳过验证
        # --- Orchestration 参数 ---
        max_tasks_per_run: int = 20,
        orchestration_timeout: int = 600,
        orchestration_mode: str = "agentic",
        router_step_callback=None,
        context_config=None,
        token_limit: int = 80000,
        sandbox_policy=None,
        require_explicit_completion: bool = False,
        state_provider=None,
        supervision_state_store=None,
        task_watchdog=None,
        watchdog_config: WatchdogConfig | None = None,
        mission_runtime_manager: MissionRuntimeManager | None = None,
        control_state_path: str | None = None,
        # Phase 4: signed task dispatch
        coordinator_secret: bytes | None = None,
        coordinator_id: str = "Coordinator",
        # Phase 2: authenticated Temporal shadow write
        memory_read_mode: str = "legacy",
        callback_secret: bytes | None = None,
        memory_config=None,
        memory_ingestor=None,
        # UI static files directory. When None, UI endpoints return 404.
        ui_dir: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._sandbox_policy = sandbox_policy
        self._ui_dir = Path(ui_dir) if ui_dir else None
        self._registry = WorkerRegistry()
        self._agent_registry = AgentRegistry(
            static_config_path=Path(config_path) if config_path else None
        )
        self._task_logger = TaskLogger(base_dir=str(log_dir) if log_dir else "logs")
        self._task_queue = TaskQueue(on_cleanup=self._cleanup_task_logs)
        self._mesh_guide = MeshGuide(self._agent_registry)
        self._worker_ws: Dict[str, WebSocket] = {}
        self._worker_ws_lock = asyncio.Lock()

        # 构建 RouterAgent（注入所有路径和配置参数）
        self._router = RouterAgent(
            registry=self._agent_registry,
            sandbox_policy=self._sandbox_policy,
            prompts_dir=Path(prompts_dir) if prompts_dir else None,
            custom_tools_dir=Path(tools_dir) if tools_dir else None,
            extra_tools=extra_tools,
            skills_dir=Path(skills_dir) if skills_dir else None,
            coordinator_secret=coordinator_secret,
            coordinator_id=coordinator_id,
            model=router_model,
            max_steps=router_max_steps,
            temperature=router_temperature,
            provider=router_provider,
            api_base=router_api_base,
            api_key_env=router_api_key_env,
            log_dir=Path(log_dir) if log_dir else None,
        )

        # 构建 VerifierAgent（可选）
        self._verifier = None
        if verifier_enabled:
            api_key = os.environ.get(router_api_key_env, "")
            if not api_key:
                logger.warning(
                    "Verifier disabled: API key not set (env var '%s'). "
                    "Set the key or pass --no-verifier to suppress this warning.",
                    router_api_key_env,
                )
            else:
                from a2a.coordinator.verifier import VerifierAgent

                v_model = verifier_model or router_model
                self._verifier = VerifierAgent(
                    registry=self._agent_registry,
                    sandbox_policy=self._sandbox_policy,
                    model=v_model,
                    max_steps=verifier_max_steps,
                    temperature=verifier_temperature,
                    provider=router_provider,
                    api_base=router_api_base,
                    api_key_env=router_api_key_env,
                    log_dir=Path(log_dir) if log_dir else None,
                )

        self._max_tasks_per_run = max_tasks_per_run
        self._orchestration_timeout = orchestration_timeout
        self._orchestration_mode = orchestration_mode
        self._router_step_callback = router_step_callback
        self._context_config = context_config
        self._token_limit = token_limit
        self._require_explicit_completion = require_explicit_completion
        self._state_provider = state_provider
        self._log_dir = log_dir

        # Supervision state store and task watchdog (Phase 3)
        self._supervision_state_store = (
            supervision_state_store or SupervisionStateStore(log_dir=log_dir)
        )
        self._task_watchdog = task_watchdog or TaskWatchdog(
            worker_registry=self._registry,
            event_store=event_store,
            supervision_store=self._supervision_state_store,
            barrier=None,
            config=watchdog_config,
        )
        if control_state_path is None and log_dir is not None:
            control_state_path = str(Path(log_dir) / "coordinator-control-state.json")
        self._mission_runtime_manager = (
            mission_runtime_manager
            or MissionRuntimeManager(
                state_path=control_state_path,
                cancel_adapter=self._cancel_recovered_worker,
            )
        )
        if getattr(self._mission_runtime_manager, "_cancel_adapter", None) is None:
            self._mission_runtime_manager.set_cancel_adapter(
                self._cancel_recovered_worker
            )

        self._coordinator_secret = coordinator_secret
        self._team_partition_service: TeamPartitionService = TeamPartitionService()
        self._proof_nonce_store = UsedNonceStore()
        self._mission_runtime_manager.set_team_partition_service(
            self._team_partition_service
        )

        # Phase 2: authenticated Temporal shadow write layer.
        self._memory_read_mode = memory_read_mode
        self._callback_secret = callback_secret
        self._memory_config = memory_config
        self._memory_ingestor = memory_ingestor
        self._callback_auth = None
        self._memory_redactor = None
        self._memory_bridge = None
        if self._memory_read_mode in ("shadow", "read_port"):
            self.configure_memory(
                ingestor=memory_ingestor,
                config=memory_config,
                secret=callback_secret,
            )
        # Production adapters: activate_plan_node fan-out + TeamPartition delivery.
        # Unit tests may inject fakes later via set_*_adapter; these are the defaults.
        from a2a.coordinator.production_adapters import wire_production_adapters

        wired = wire_production_adapters(
            mission_runtime_manager=self._mission_runtime_manager,
            team_partition_service=self._team_partition_service,
            router=self._router,
            agent_registry=self._agent_registry,
            worker_registry=self._registry,
            coordinator_host=self._host
            if self._host not in ("0.0.0.0", "::")
            else "localhost",
            coordinator_port=self._port,
            coordinator_secret=self._coordinator_secret,
        )
        self._team_delivery_sender = wired.get("sender")
        self._barrier = None  # SARBarrier (optional, for map visualization)
        self._semantic_map = (
            None  # SemanticMapStore (optional, for observation ingestion)
        )

        self._observed_step_keys: set[tuple[str, str, str, str]] = set()
        self._app = self._build_app()
        self._a2a_server = None
        self._server_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._uvicorn_server = None

    async def _cancel_recovered_worker(
        self, worker_id: str, worker_task_id: str
    ) -> str:
        """Cancel a recovered Worker task through the native A2A client."""
        from a2a.client import ClientConfig, create_client
        from a2a.types.a2a_pb2 import CancelTaskRequest, TaskState

        agent_info = self._agent_registry.get(worker_id)
        client = await create_client(
            agent_info.endpoint,
            ClientConfig(streaming=False),
        )
        try:
            task = await client.cancel_task(CancelTaskRequest(id=worker_task_id))
            return TaskState.Name(task.status.state)
        finally:
            await client.close()

    def set_barrier(self, barrier) -> None:
        """注入 SARBarrier 引用，供 /map/state SSE 端点使用。"""
        self._barrier = barrier

    def set_team_partition_service(self, service: TeamPartitionService) -> None:
        """注入 TeamPartitionService —— 唯一通信拓扑权威。

        /team-status, callback, reconcile 均必须通过此服务读取分区，
        不得读取 module/global team 或 fallback semantic-map agent list。
        """
        self._team_partition_service = service

    # ── Phase 4: strict /environment-state admission ────────────────────

    def _resolve_environment_state_admission(
        self, worker_task_id: str
    ) -> tuple[str, str, str]:
        """Resolve the task-bound identity to an active worker dispatch.

        Fail-closed admission for ``/environment-state``: the worker identity is
        derived from the **server-issued, opaque ``worker_task_id``** (the A2A
        task id the coordinator assigned when dispatching to this worker), NOT
        from a caller-selected ``worker_id`` with a forgeable shared-secret
        proof.  The server resolves ``worker_task_id → dispatch → worker_id``
        and requires an active (non-terminal) dispatch owned by that worker in
        the current runtime admission.  An unknown task, a terminal dispatch, or
        an unregistered worker is rejected with a typed ``403`` reason — never
        silently downgraded.

        Returns ``(worker_id, dispatch_id, "")`` on success, or
        ``("", "", reason)`` on rejection.  This method never reads simulator /
        oracle truth.
        """
        active_runtime = self._mission_runtime_manager.active_runtime
        if active_runtime is None:
            return "", "", "environment_state_unavailable: no active runtime"

        # Identity is server-derived from the task binding, never from the
        # caller.  An unknown task id fails closed (the caller cannot fabricate
        # a task id they were never dispatched).
        dispatch = active_runtime.resolve_worker_task(worker_task_id)
        if dispatch is None:
            return "", "", "environment_state_unknown_worker_task"

        worker_id = dispatch.worker_id

        # Registry / membership check where facilities exist: the server-derived
        # worker must be a known agent or an online registered worker.
        agent_known = False
        try:
            self._agent_registry.get(worker_id)
            agent_known = True
        except Exception:  # noqa: BLE001 - registry lookup must never raise out
            agent_known = False
        worker_known = False
        try:
            self._registry.get(worker_id)
            worker_known = True
        except Exception:  # noqa: BLE001 - registry lookup must never raise out
            worker_known = False
        if not agent_known and not worker_known:
            return "", "", "environment_state_unknown_worker"

        # Only active (non-terminal) dispatches may serve a worker-scoped view.
        if dispatch.state.terminal:
            # The fetch resolved the PREVIOUS task's now-terminal dispatch
            # while the worker already holds a NEWER active dispatch (a re-send
            # / re-activation raced the stale binding).  This is the same
            # startup binding-ordering window as an unbound task id: it
            # resolves milliseconds later, so it defers as
            # ``environment_state_unknown_worker_task`` instead of latching the
            # read_port→legacy rollback.  A terminal dispatch with NO newer
            # active dispatch for the worker is genuine and stays
            # ``environment_state_no_active_dispatch``.
            if active_runtime.has_active_dispatch_for(worker_id):
                return "", "", "environment_state_unknown_worker_task"
            return "", "", "environment_state_no_active_dispatch"

        return worker_id, dispatch.dispatch_id, ""

    # ── Phase 2: authenticated Temporal shadow write layer ─────────────

    def configure_memory(
        self,
        *,
        ingestor,
        config,
        secret: bytes | None,
        mode: str | None = None,
    ) -> None:
        """Install the authenticated Memory layer (shadow/read_port).

        Fails closed with ``memory_auth_not_configured`` when the secure mode is
        requested without a validated ingestor + secret.  Wires the journal
        receipt seam onto every admitted MissionRuntime and the supervision
        event adapter into TaskWatchdog.
        """
        from a2a.coordinator.memory.callback_auth import (
            MemoryAuthNotConfiguredError,
            CallbackAuthenticator,
        )
        from a2a.coordinator.memory.ingestor import (
            MemoryLifecycleBridge,
            SupervisionEventAdapter,
        )

        if mode is not None:
            self._memory_read_mode = mode
        if ingestor is None or config is None:
            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: shadow/read_port requires a "
                "configured MemoryIngestor and MemoryConfig"
            )
        if not secret or not isinstance(secret, bytes) or len(secret) < 16:
            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: callback secret must be at least 16 bytes"
            )
        self._memory_ingestor = ingestor
        self._memory_config = config
        self._memory_redactor = ingestor.redaction
        self._callback_auth = CallbackAuthenticator(secret, ingestor.store)
        bridge = MemoryLifecycleBridge(ingestor)
        ingestor.set_bridge(bridge)
        self._memory_bridge = bridge

        # Dispatch-bound supervision adapter → TaskWatchdog.
        adapter = SupervisionEventAdapter(
            ingestor, ingestor.scope_factory, ingestor.store
        )
        if self._task_watchdog is not None:
            self._task_watchdog.set_supervision_event_sink(adapter)

        # Every admitted MissionRuntime must have its canonical scope activated
        # BEFORE any callback can be accepted, and the lock-external journal
        # receipt seam attached.  The scope is derived from trusted control
        # state (admitted context_id + active epoch), never from callback data.
        # A closed tuple fails closed (MemoryScopeReuseError) so admission is
        # rejected instead of reopening a closed scope.
        def _on_runtime_created(rt):
            self._memory_ingestor.activate_runtime_scope(
                rt.context_id,
                rt._manager.epoch,  # noqa: SLF001
            )
            rt.attach_receipt_sink(self._memory_ingestor.receipt_sink)

        # Applies to the currently active runtime (if any) as well as every
        # future admission via MissionRuntimeManager.set_runtime_created_hook.
        self._mission_runtime_manager.set_runtime_created_hook(_on_runtime_created)

    @property
    def memory_ingestor(self):
        return self._memory_ingestor

    # ── Phase 5: run-terminal compatibility materialization ──────────────

    def _resolve_export_scope_id(self) -> str | None:
        """Resolve the scope to export at run close (active runtime, else last).

        Never trusts a callback/body-derived identity: the scope comes from the
        admitted MissionRuntime (context_id + epoch) or, after the runtime has
        been released (e.g. coordinator finished early), the most recently
        used canonical scope in the store.
        """
        if self._memory_ingestor is None:
            return None
        rt = getattr(self._mission_runtime_manager, "active_runtime", None)
        if rt is not None:
            context_id = getattr(rt, "context_id", "") or ""
            epoch = getattr(getattr(rt, "_manager", None), "epoch", 0) or 0
            if context_id:
                return self._memory_ingestor.scope_id_for(context_id, epoch)
        scopes = self._memory_ingestor.store.list_scopes()
        if not scopes:
            return None

        def _revision(scope_id: str) -> int:
            try:
                return int(self._memory_ingestor.store.revision_of(scope_id))
            except Exception:  # noqa: BLE001 - read-only best effort
                return 0

        return max(
            scopes,
            key=lambda s: (s.get("closed_at") is None, _revision(s["scope_id"])),
        )["scope_id"]

    def materialize_compatibility_artifacts(
        self, export_dir: str | Path | None = None
    ) -> dict[str, Any] | None:
        """Phase 5 run-terminal compatibility materialization.

        When canonical Memory is configured (``shadow``/``read_port``),
        deterministically rebuild the compatibility artifacts
        (``semantic_map.jsonl`` + canonical exports + ``export_manifest.json``)
        from the committed canonical set for the active/last scope, then mark
        read-only legacy debug artifacts (``events_<task>.ndjson``,
        ``supervision_<dispatch>.ndjson``) as ``legacy_unmigrated``.

        Never backfills ambiguous historical artifacts and never deletes legacy
        consumers.  In ``legacy`` mode (no canonical Memory) this is a no-op.
        """
        if self._memory_ingestor is None:
            logger.info("materialize: canonical Memory not configured; skipping")
            return None
        from a2a.coordinator.memory.exporter import MemoryExporter

        scope_id = self._resolve_export_scope_id()
        if scope_id is None:
            logger.warning("materialize: no canonical scope to export")
            return None
        export_dir = Path(export_dir) if export_dir is not None else Path(self._log_dir or ".")
        export_dir.mkdir(parents=True, exist_ok=True)
        exporter = MemoryExporter(self._memory_ingestor.store)
        report = exporter.export_scope(scope_id, export_dir)

        legacy: dict[str, str] = {}
        for filename in event_store.legacy_artifact_filenames():
            legacy[filename] = (
                "legacy EventStore debug adapter artifact "
                "(events_<task>.ndjson); not a canonical Memory export"
            )
        for filename in self._supervision_state_store.legacy_artifact_filenames():
            legacy[filename] = (
                "legacy SupervisionStateStore debug artifact "
                "(supervision_<dispatch>.ndjson); not a canonical Memory export"
            )
        if legacy:
            exporter.mark_legacy_unmigrated(export_dir, legacy, scope_id=scope_id)

        logger.info(
            "materialize: scope=%s revision=%d artifacts=%d legacy_unmigrated=%d export_dir=%s",
            scope_id,
            report.canonical_revision,
            len(report.artifacts),
            len(legacy),
            export_dir,
        )
        return {
            "scope_id": scope_id,
            "canonical_revision": report.canonical_revision,
            "artifacts": sorted(report.artifacts),
            "legacy_unmigrated": sorted(legacy),
            "export_dir": str(export_dir),
            "manifest_path": str(report.manifest_path)
            if report.manifest_path
            else None,
        }

    @staticmethod
    def _extract_callback_identity(sr) -> tuple[str | None, str]:
        """Return (context_id, task_id) from a parsed StreamResponse."""
        if sr.HasField("task"):
            return (sr.task.context_id or None, sr.task.id)
        if sr.HasField("status_update"):
            return (sr.status_update.context_id or None, sr.status_update.task_id)
        if sr.HasField("artifact_update"):
            return (sr.artifact_update.context_id or None, sr.artifact_update.task_id)
        return (None, "")

    def _ingest_callback_to_memory(
        self,
        *,
        dispatch,
        context_id: str | None,
        worker_task_id: str,
        callback_kind: str,
        normalized_state: str,
        body_bytes: bytes,
        body_dict: dict,
        control_receipts: list,
        runtime_epoch: int,
        projection_inputs: list | None = None,
    ):
        """Fan a validated authenticated callback into canonical Memory.

        When ``projection_inputs`` (normalized Worker evidence) is provided it
        is reduced atomically inside the same canonical bundle as the callback
        Temporal event — the production producer path invokes the canonical
        projection ingest/reducer, not only the Temporal callback write.
        """
        if self._memory_ingestor is None:
            return None
        from a2a.coordinator.memory.ingestor import AuthenticatedCallbackEnvelope

        scope_id = self._memory_ingestor.scope_id_for(
            context_id or dispatch.context_id, runtime_epoch
        )
        envelope = AuthenticatedCallbackEnvelope.build(
            scope_id=scope_id,
            dispatch_id=dispatch.dispatch_id,
            worker_task_id=worker_task_id,
            actor_id=dispatch.worker_id,
            runtime_epoch=runtime_epoch,
            callback_kind=callback_kind,
            normalized_state=normalized_state,
            body_sha256=hashlib.sha256(body_bytes).hexdigest(),
        )
        return self._memory_ingestor.ingest_callback(
            envelope,
            body_dict,
            control_receipts,
            projection_inputs=projection_inputs or [],
        )

    def _normalize_observation_projection_inputs(
        self,
        observations_with_prov: list[tuple[dict, str]],
        *,
        dispatch,
        context_id: str | None,
        worker_task_id: str,
        body_sha256: str,
        runtime_epoch: int,
    ) -> list:
        """Map extracted Worker observations into ``NormalizedProjectionInputV1``.

        Each observation becomes a set of field-level claims (position, scalar
        attributes, agent inventory) with an allowlisted provenance class.  The
        evidence identity is deterministic per (callback, observation) so the
        canonical bundle is idempotent and the external evidence_id is retained
        for causation/audit.
        """
        from a2a.coordinator.memory.contracts import (
            NormalizedProjectionInputV1,
            normalize_inventory,
            scan_forbidden_truth_fields,
        )

        if self._memory_ingestor is None:
            return []
        scope_id = self._memory_ingestor.scope_id_for(
            context_id or dispatch.context_id, runtime_epoch
        )
        correlation_id = f"dispatch:{dispatch.dispatch_id}"
        dispatch_id = dispatch.dispatch_id
        inputs: list = []
        for obs, provenance in observations_with_prov:
            obj_type = str(obs.get("object_type") or "unknown")
            name = str(obs.get("name") or "")
            if not name:
                continue
            if scan_forbidden_truth_fields(obs):
                # Direct-world / oracle / ground-truth masked observation: never
                # enters legacy or canonical sinks.
                continue
            step = obs.get("step")
            evidence_id = (
                f"cb:{worker_task_id}:{body_sha256[:16]}:{obj_type}:{name}:{step}"
            )
            domain = "embodied" if obj_type == "agent" else "spatial"
            entity_type = "agent" if obj_type == "agent" else obj_type
            confidence = float(obs.get("confidence", 1.0))
            actor_id = str(obs.get("reporter") or dispatch.worker_id)

            def _claim(
                field_name: str,
                value,
                env_step,
                *,
                _evidence_id: str = evidence_id,
                _actor_id: str = actor_id,
                _provenance: str = provenance,
                _domain: str = domain,
                _name: str = name,
                _entity_type: str = entity_type,
                _confidence: float = confidence,
            ) -> NormalizedProjectionInputV1:
                return NormalizedProjectionInputV1(
                    scope_id=scope_id,
                    event_id=_evidence_id,
                    sequence=0,
                    env_step=env_step,
                    actor_id=_actor_id,
                    provenance=_provenance,
                    domain=_domain,
                    entity_id=_name,
                    entity_type=_entity_type,
                    field_name=field_name,
                    value=value,
                    confidence=_confidence,
                    runtime_epoch=runtime_epoch,
                    dispatch_id=dispatch_id,
                    worker_task_id=worker_task_id,
                    correlation_id=correlation_id,
                )

            pos = obs.get("position")
            if pos is not None:
                inputs.append(
                    _claim(
                        "position",
                        list(pos) if isinstance(pos, (list, tuple)) else pos,
                        step,
                    )
                )
            attrs = obs.get("attributes") or {}
            for key, value in list(attrs.items())[:8]:
                if key in ("position", "inventory"):
                    continue
                inputs.append(_claim(key, value, step))
            if obj_type == "agent":
                inventory = attrs.get("inventory")
                if inventory is None:
                    inventory = obs.get("inventory")
                if inventory is not None:
                    # Canonical claims the SAME semantic representation as the
                    # legacy map: a normalized resource list (structured parsing,
                    # never eval) so the shadow projections agree.
                    inputs.append(
                        _claim("inventory", normalize_inventory(inventory), step)
                    )
        return inputs

    def _completion_validator(self) -> bool:
        """Read SAR completion truth dynamically; generic servers stay permissive."""
        barrier = self._barrier
        if barrier is None:
            return True
        try:
            return bool(barrier.is_finished())
        except Exception:
            return False

    def set_semantic_map(self, semantic_map) -> None:
        """注入 SemanticMapStore 引用，供 observation ingest 使用。"""
        self._semantic_map = semantic_map
        # Mount Map Agent MCP server now that semantic_map is available
        if semantic_map is not None:
            from sar_orch.map_agent import mount_to_fastapi as mount_map_agent_mcp

            mount_map_agent_mcp(self._app, semantic_map)
            logger.info(
                "Map Agent MCP server mounted at /mcp/map (via set_semantic_map)"
            )

    def _is_step_observation_known(
        self, obs: dict, *, scope_id: str | None = None
    ) -> bool:
        """True when this exact (scope, object_type, name, step) evidence has
        already been ingested for the legacy sink.

        ``scope_id`` is the dedup scope — the ``context_id|worker`` composite
        passed by ``_ingest_observations_from_status`` — so dedup is per
        mission context AND per worker/task, never cross-worker.
        """
        key = (
            scope_id or "",
            str(obs.get("object_type")),
            str(obs.get("name")),
            str(obs.get("step")),
        )
        if key in self._observed_step_keys:
            return True
        self._observed_step_keys.add(key)
        return False

    def _ingest_observations_from_status(
        self,
        worker_task_id: str,
        status_text: str,
        *,
        dispatch_id: str,
        worker_id: str,
        context_id: str | None = None,
        observations: list[dict] | None = None,
    ) -> int:
        """Persist observations carried by a Worker status update once.

        EventStore keys use ``dispatch_id`` (same as status_update) so dispatch
        queries see observations. ``worker_task_id`` is retained for watchdog.
        Observations that mask a forbidden oracle / ground_truth / direct-world
        field are stripped before any sink (H1-INV-1).  ``observations`` may be
        supplied by the caller to avoid a second extraction pass.
        """
        if observations is None:
            observations = _extract_auto_observations(status_text)
        if not observations:
            return 0

        from a2a.coordinator.memory.contracts import scan_forbidden_truth_fields

        observations = [
            obs for obs in observations if not scan_forbidden_truth_fields(obs)
        ]

        event_key = dispatch_id or worker_task_id
        # Legacy-sink dedup is scoped PER (mission context, worker/task), never
        # cross-worker: the same (object_type, name, step) evidence reported by
        # different workers at the same env step is distinct evidence that must
        # reach both the legacy and canonical sinks (canonical ids it by worker
        # task, so it would surface a same-step conflict).  Only a single
        # worker's own re-send inside one mission context (spam) is suppressed.
        # Same-callback spam dedup happens earlier inside
        # ``_extract_observations_with_provenance``.
        dedup_scope = f"{context_id or ''}|{worker_id or worker_task_id or ''}"
        ingested = 0
        for obs in observations:
            if self._is_step_observation_known(obs, scope_id=dedup_scope):
                continue
            # memory-producer: observation_report_ingest; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
            event_store.append(
                event_key,
                "observation_report",
                text=status_text[:500],
                observation=obs,
            )
            if self._semantic_map is not None:
                self._semantic_map.ingest_observation(obs)
            ingested += 1

        if ingested and self._task_watchdog is not None:
            self._task_watchdog.record_progress(
                dispatch_id=dispatch_id or worker_task_id,
                worker_id=worker_id,
                worker_task_id=worker_task_id,
                source="observation_report",
                step=self._barrier._step_counter if self._barrier else 0,
            )
        return ingested

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    @property
    def task_queue(self) -> TaskQueue:
        return self._task_queue

    @property
    def mission_runtime_manager(self) -> MissionRuntimeManager:
        """The sole Coordinator-lifetime MissionRuntime owner."""
        return self._mission_runtime_manager

    @property
    def mesh_guide(self) -> MeshGuide:
        return self._mesh_guide

    def _cleanup_task_logs(self, removed_ids: list[str]) -> None:
        """TaskQueue on_cleanup 回调：清理已完成任务的日志文件。"""
        for tid in removed_ids:
            self._task_logger.cleanup_task(tid)

    def _build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            self._owner_loop = asyncio.get_running_loop()
            # Import MCP session manager (may have been initialized by set_semantic_map)
            _mcp_sm = None
            _mcp_ctx = None
            try:
                from sar_orch.map_agent.server import mcp as _map_agent_mcp

                _mcp_sm = _map_agent_mcp.session_manager
            except (ImportError, RuntimeError):
                pass

            # 启动时: enter MCP session manager if available
            if _mcp_sm is not None:
                _mcp_ctx = _mcp_sm.run()
                await _mcp_ctx.__aenter__()

            # Recovery fences any persisted run before a new agentic context
            # can be admitted.  A live in-process runtime is aborted first.
            if self._mission_runtime_manager.active_runtime is not None:
                await self._mission_runtime_manager.abort("startup_recovery")
            await self._mission_runtime_manager.recover_and_reconcile()
            await self._start_cleanup_task()
            # Inject barrier into watchdog for domain delta detection
            self._task_watchdog._barrier = self._barrier
            self._task_watchdog._supervision_store.set_log_dir(self._log_dir)
            await self._task_watchdog.start()
            a2a_srv = create_coordinator_a2a_server(
                host=self._host,
                port=self._a2a_port,
                agent_registry=self._agent_registry,
                task_queue=self._task_queue,
                router=self._router,  # 注入 RouterAgent
                verifier=self._verifier,  # 注入 VerifierAgent（可为 None）
                task_logger=self._task_logger,  # 注入 TaskLogger
                max_tasks_per_run=self._max_tasks_per_run,
                orchestration_timeout=self._orchestration_timeout,
                orchestration_mode=self._orchestration_mode,
                router_step_callback=self._router_step_callback,
                context_config=self._context_config,
                token_limit=self._token_limit,
                require_explicit_completion=self._require_explicit_completion,
                sandbox_policy=self._sandbox_policy,
                coordinator_host="localhost",
                coordinator_port=self._port,
                state_provider=self._state_provider,
                task_watchdog=self._task_watchdog,
                mission_runtime_manager=self._mission_runtime_manager,
                completion_validator=self._completion_validator,
            )
            self._a2a_server = a2a_srv
            self._server_task = asyncio.create_task(a2a_srv.serve())
            # Phase 3: 启动后台状态同步任务（通过 A2A server 关联的 executor）
            self._sync_task = asyncio.create_task(
                self._a2a_server.executor._periodic_state_sync()
            )
            yield
            # 关闭时
            await self._mission_runtime_manager.abort("coordinator_shutdown")
            # 停止状态同步任务
            if self._sync_task:
                self._sync_task.cancel()
                try:
                    await self._sync_task
                except asyncio.CancelledError:
                    pass
            await self._task_watchdog.stop()
            try:
                await shutdown_uvicorn_server(self._a2a_server, self._server_task)
            finally:
                self._server_task = None
                self._a2a_server = None
            # 关闭 RouterAgent SDK clients
            await self._router.close()
            await self._stop_cleanup_task()
            # Exit MCP session manager
            if _mcp_sm is not None and _mcp_ctx is not None:
                await _mcp_ctx.__aexit__(None, None, None)

        app = FastAPI(title="OpenHarness A2A Coordinator", lifespan=lifespan)

        # 注册 REST API routes
        health.register_routes(app, self)
        workers.register_routes(app, self)

        @app.post("/tasks")
        async def create_task(request: CreateTaskRequest):
            task_id = request.task_id or uuid.uuid4().hex
            if not request.prompt:
                raise HTTPException(status_code=400, detail="prompt required")
            task = DistributedTask(
                task_id=task_id,
                task_type=request.task_type,
                prompt=request.prompt,
            )
            self._task_queue.enqueue(task)
            return {"task_id": task_id, "status": "pending"}

        @app.get("/tasks/{task_id}")
        async def get_task(task_id: str):
            try:
                task = self._task_queue.get(task_id)
                return {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "assigned_worker": task.assigned_worker,
                    "result": task.result,
                    "error": task.error,
                }
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail="Task not found")

        @app.post("/tasks/{task_id}/cancel")
        async def cancel_task(task_id: str):
            """Cancel a running task. Also cancels the worker-side A2A task if assigned."""
            try:
                task = self._task_queue.get(task_id)
                assigned_worker = task.assigned_worker
                context_id = task.context_id
                self._task_queue.cancel(task_id)
                active_runtime = self._mission_runtime_manager.active_runtime
                if (
                    context_id
                    and active_runtime is not None
                    and context_id == active_runtime.context_id
                ):
                    for owned_task in self._task_queue.list_by_context(context_id):
                        if owned_task.task_id == task_id:
                            continue
                        if owned_task.status in (
                            TaskStatus.PENDING,
                            TaskStatus.RUNNING,
                        ):
                            self._task_queue.cancel(owned_task.task_id)
                    await self._mission_runtime_manager.abort("explicit_cancel")
                # Notify worker via WebSocket
                if assigned_worker and assigned_worker in self._worker_ws:
                    async with self._worker_ws_lock:
                        await self._send_to_worker(
                            assigned_worker,
                            {
                                "type": WS_CANCEL_TASK,
                                "payload": {"task_id": task_id},
                            },
                        )
                return {"status": "cancelled", "task_id": task_id}
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail="Task not found")
            except InvalidStatusTransitionError:
                raise HTTPException(
                    status_code=400, detail="Cannot cancel task in current state"
                )
            except Exception as e:
                logger.error(f"Error cancelling task {task_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        async def _do_cancel_experiment(context_id: str) -> dict:
            """Shared logic: cancel all tasks for a context_id, notify workers, stop barrier."""
            tasks = self._task_queue.list_by_context(context_id)
            if not tasks:
                raise HTTPException(
                    status_code=404,
                    detail=f"No tasks found for context_id: {context_id}",
                )

            cancelled_ids = []
            notified_workers: set[str] = set()

            for task in tasks:
                if task.status in (
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                ):
                    try:
                        self._task_queue.cancel(task.task_id)
                        cancelled_ids.append(task.task_id)
                        if task.assigned_worker:
                            notified_workers.add(task.assigned_worker)
                    except (TaskNotFoundError, InvalidStatusTransitionError):
                        pass

            # Notify all affected workers via WebSocket
            async with self._worker_ws_lock:
                for worker_id in notified_workers:
                    if worker_id in self._worker_ws:
                        await self._send_to_worker(
                            worker_id,
                            {
                                "type": WS_CANCEL_TASK,
                                "payload": {
                                    "task_id": "all",
                                    "context_id": context_id,
                                },
                            },
                        )

            # If SARBarrier is attached, stop the environment and wake all workers
            barrier_stopped = False
            if self._barrier is not None:
                try:
                    self._barrier.stop()
                    barrier_stopped = True
                except Exception as e:
                    logger.error("Error stopping barrier: %s", e)

            logger.info(
                "Experiment %s cancelled: %d tasks, %d workers notified, barrier=%s",
                context_id,
                len(cancelled_ids),
                len(notified_workers),
                barrier_stopped,
            )
            return {
                "status": "cancelled",
                "context_id": context_id,
                "cancelled_tasks": cancelled_ids,
                "workers_notified": list(notified_workers),
                "barrier_stopped": barrier_stopped,
            }

        @app.post("/experiment/cancel-by-task/{task_id}")
        async def cancel_experiment_by_task(task_id: str):
            """Cancel the entire experiment that a task belongs to.

            Looks up the task by task_id, finds its context_id, and cancels
            all tasks sharing that context_id. Also stops the SARBarrier if attached.
            """
            try:
                task = self._task_queue.get(task_id)
                cid = task.context_id
                if not cid:
                    # Fallback: use task_id itself as context marker
                    return await _do_cancel_experiment(task_id)
                # Forward to the context_id-based handler
                return await _do_cancel_experiment(cid)
            except TaskNotFoundError:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        @app.post("/experiment/{context_id}/cancel")
        async def cancel_experiment(context_id: str):
            """Cancel all tasks for a given context_id (one complete experiment).

            Finds all tasks in the TaskQueue with the matching context_id,
            cancels them, notifies all affected workers via WebSocket,
            and if a SARBarrier is attached, calls barrier.stop() to shut
            down the environment and wake all waiting worker threads.
            """
            return await _do_cancel_experiment(context_id)

        # ---- UI 和 A2A 代理 ----

        UI_DIR = self._ui_dir

        def _serve_ui_file(filename: str, media_type: str = "text/html"):
            if UI_DIR is None:
                raise HTTPException(status_code=404, detail="UI not configured")
            ui_file = UI_DIR / filename
            if not ui_file.exists():
                raise HTTPException(
                    status_code=404, detail=f"UI file not found: {filename}"
                )
            return FileResponse(ui_file, media_type=media_type)

        @app.get("/ui")
        async def serve_ui():
            """提供任务控制台 HTML 界面。"""
            return _serve_ui_file("task_ui.html")

        # ---- TaskLogger HTTP API ----

        @app.get("/logs")
        async def list_logs():
            task_ids = self._task_logger.list_task_ids()
            return {"logs": task_ids}

        @app.get("/logs/{task_id}")
        async def get_log(task_id: str):
            try:
                path = self._task_logger.get_log_path(task_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid task_id")

            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="log not found")

            return FileResponse(path, media_type="application/x-ndjson")

        @app.get("/logs/{task_id}/stream")
        async def stream_log(task_id: str, request: Request):
            try:
                path = self._task_logger.get_log_path(task_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid task_id")

            async def event_generator():
                # 1. 回放已有内容
                last_size = 0
                if os.path.exists(path):
                    with open(path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                                try:
                                    evt = json.loads(line)
                                    if evt.get("event") in (
                                        "done",
                                        "task_final",
                                        "task_error",
                                    ):
                                        yield "event: done\ndata: {}\n\n"
                                        return
                                except json.JSONDecodeError:
                                    pass
                        last_size = f.tell()

                # 2. 实时轮询新行
                while True:
                    if await request.is_disconnected():
                        break

                    await asyncio.sleep(0.1)

                    if not os.path.exists(path):
                        continue

                    current_size = os.path.getsize(path)
                    if current_size < last_size:
                        # 文件被删除重建，从头开始
                        last_size = 0
                    if current_size > last_size:
                        with open(path, "r") as f:
                            f.seek(last_size)
                            for line in f:
                                line = line.strip()
                                if line:
                                    yield f"data: {line}\n\n"

                                    try:
                                        evt = json.loads(line)
                                        if evt.get("event") in (
                                            "done",
                                            "task_final",
                                            "task_error",
                                        ):
                                            yield "event: done\ndata: {}\n\n"
                                            return
                                    except json.JSONDecodeError:
                                        pass
                        last_size = current_size

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/ui/debug")
        async def debug_ui():
            return _serve_ui_file("debug.html")

        @app.get("/ui/map")
        async def serve_map_ui():
            """提供 SAR 地图可视化界面。"""
            return _serve_ui_file("map.html")

        @app.get("/dashboard")
        async def serve_dashboard():
            """提供独立仪表盘前端。"""
            return _serve_ui_file("dashboard/index.html")

        @app.post("/a2a/push-callback")
        async def handle_push_notification(request: Request):
            from google.protobuf.json_format import ParseDict
            from a2a.types.a2a_pb2 import StreamResponse, TaskState
            from a2a.coordinator.task_store import resolve_global_future

            def _try_resolve_worker_id(task_id: str) -> str:
                """从 TaskStore 映射解析 worker_id；失败返回空字符串。"""
                if (
                    self._task_watchdog is None
                    or self._task_watchdog._task_store is None
                ):
                    return ""
                store = self._task_watchdog._task_store
                dispatch_id = store._worker_to_dispatch.get(task_id, "")
                if dispatch_id:
                    node = store.get_node(dispatch_id)
                    return node.worker_id if node and node.worker_id else ""
                return ""

            def _resolve_legacy_dispatch_id(task_id: str) -> str:
                """Use one EventStore key for legacy status and observation events."""
                if (
                    self._task_watchdog is None
                    or self._task_watchdog._task_store is None
                ):
                    return task_id
                store = self._task_watchdog._task_store
                return store._worker_to_dispatch.get(task_id, task_id)

            body_bytes = await request.body()
            try:
                body = json.loads(body_bytes)
            except json.JSONDecodeError:
                return JSONResponse(
                    {"status": "rejected", "reason": "malformed_json"}, status_code=400
                )

            # Phase 2: authenticated Temporal shadow write.  In shadow/read_port
            # mode the route-front auth gate runs before ANY MissionRuntime /
            # EventStore / SemanticMapStore / TaskWatchdog / MemoryIngestor call:
            # proof failures cause zero domain writer calls.
            secure = (
                self._memory_read_mode in ("shadow", "read_port")
                and self._callback_auth is not None
            )
            auth_dispatch = None
            auth_epoch = 0
            auth_worker_id = ""
            if secure:
                auth = self._callback_auth
                assert auth is not None  # secure implies auth is configured
                auth_worker_id = request.headers.get("X-A2A-Worker-Id", "")
                proof = request.headers.get("X-A2A-Callback-Proof", "")
                digest_prefix = hashlib.sha256(body_bytes).hexdigest()[:16]

                def _reject(reason: str, status_code: int = 401):
                    auth.record_rejection(
                        reason, worker_id=auth_worker_id, body_sha256=digest_prefix
                    )
                    return JSONResponse(
                        {"status": "rejected", "reason": reason},
                        status_code=status_code,
                    )

                ok, reason = auth.verify_proof(auth_worker_id, proof, body_bytes)
                if not ok:
                    return _reject(reason)

                # Resolve active runtime + dispatch + actor from trusted control
                # state BEFORE the durable nonce reservation.
                manager = self._mission_runtime_manager
                active_runtime = manager.active_runtime
                if active_runtime is None:
                    return _reject("unknown_context")
                _id_sr = StreamResponse()
                ParseDict(body, _id_sr)
                _ctx, _task_id = self._extract_callback_identity(_id_sr)
                if _ctx and _ctx != active_runtime.context_id:
                    return _reject("stale_context")
                auth_dispatch = active_runtime.resolve_worker_task(_task_id) or (
                    active_runtime.get_dispatch(_task_id) if _task_id else None
                )
                if auth_dispatch is None:
                    return _reject("unknown_worker_task")
                if auth_dispatch.worker_id != auth_worker_id:
                    return _reject("worker_mismatch")
                auth_epoch = active_runtime._manager.epoch  # noqa: SLF001

                ok, reason = auth.reserve_nonce(auth_worker_id, proof, body_bytes)
                if not ok:
                    return _reject(reason)

                # Sanitize before fan-out to ALL writers.  A valid signature never
                # exempts the body from redaction.
                body = self._memory_redactor.sanitize_callback(body)
                assert self._memory_redactor is not None

            sr = StreamResponse()
            ParseDict(body, sr)

            # Phase 0 route: once a runtime is active, callbacks must be
            # context-bound and must enter its canonical transition API.  The
            # legacy branch below remains only for pre-admission compatibility.
            manager = self._mission_runtime_manager
            if manager.active_runtime is not None:
                callback_context = None
                callback_task_id = ""
                callback_state = None
                callback_artifact = None
                callback_result = None
                if sr.HasField("task"):
                    callback_task_id = sr.task.id
                    callback_context = sr.task.context_id or None
                    callback_state = (
                        TaskState.Name(sr.task.status.state)
                        if sr.task.status.state
                        else "TASK_STATE_UNSPECIFIED"
                    )
                    if sr.task.status.HasField("message"):
                        callback_result = " ".join(
                            p.text for p in sr.task.status.message.parts if p.text
                        )
                elif sr.HasField("status_update"):
                    callback_task_id = sr.status_update.task_id
                    callback_context = sr.status_update.context_id or None
                    callback_state = (
                        TaskState.Name(sr.status_update.status.state)
                        if sr.status_update.HasField("status")
                        and sr.status_update.status.state
                        else "TASK_STATE_UNSPECIFIED"
                    )
                    if sr.status_update.status.HasField("message"):
                        callback_result = " ".join(
                            p.text
                            for p in sr.status_update.status.message.parts
                            if p.text
                        )
                elif sr.HasField("artifact_update"):
                    callback_task_id = sr.artifact_update.task_id
                    callback_context = sr.artifact_update.context_id or None
                    if sr.artifact_update.HasField("artifact"):
                        callback_artifact = " ".join(
                            p.text for p in sr.artifact_update.artifact.parts if p.text
                        )

                if callback_artifact is not None:
                    if secure:
                        with self._memory_ingestor.callback_bundle() as _receipts:
                            routed = manager.handle_artifact(
                                callback_context,
                                callback_task_id,
                                callback_artifact,
                            )
                        if (
                            self._memory_ingestor is not None
                            and auth_dispatch is not None
                        ):
                            self._ingest_callback_to_memory(
                                dispatch=auth_dispatch,
                                context_id=callback_context,
                                worker_task_id=callback_task_id,
                                callback_kind="artifact_update",
                                normalized_state="",
                                body_bytes=body_bytes,
                                body_dict=body,
                                control_receipts=_receipts,
                                runtime_epoch=auth_epoch,
                            )
                    else:
                        routed = manager.handle_artifact(
                            callback_context, callback_task_id, callback_artifact
                        )
                    if routed.status == "ignored":
                        return {"status": "ignored", "reason": routed.reason}
                    # memory-producer: callback_artifact_branch; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                    event_store.append(
                        routed.dispatch_id or callback_task_id,
                        "artifact_update",
                        context_id=callback_context,
                        text=callback_artifact,
                    )
                    active_dispatch = manager.active_runtime.get_dispatch(
                        routed.dispatch_id or ""
                    )
                    if active_dispatch is not None and self._task_watchdog is not None:
                        self._task_watchdog.record_progress(
                            dispatch_id=active_dispatch.dispatch_id,
                            worker_id=active_dispatch.worker_id,
                            worker_task_id=callback_task_id,
                            source="artifact_update",
                        )
                    return {"status": "ok"}

                if secure and self._memory_ingestor is not None:
                    # callback-origin journal receipts are bundled directly into
                    # the canonical transaction — never double bridge-enqueued.
                    with self._memory_ingestor.callback_bundle() as _receipts:
                        routed = manager.handle_callback(
                            callback_context,
                            callback_task_id,
                            callback_state,
                            source="push_callback",
                            result=callback_result,
                        )
                    # Preserve the captured receipts for the canonical bundle.
                    _callback_receipts = list(_receipts)
                else:
                    routed = manager.handle_callback(
                        callback_context,
                        callback_task_id,
                        callback_state,
                        source="push_callback",
                        result=callback_result,
                    )
                    _callback_receipts = []
                # Observation ingestion is independent of physical transitions:
                # repeated WORKING callbacks with new observations must still
                # be ingested even when handle_callback returns stale_transition.
                active_dispatch = None
                if routed.dispatch_id and manager.active_runtime is not None:
                    active_dispatch = manager.active_runtime.get_dispatch(
                        routed.dispatch_id
                    )
                if (
                    active_dispatch is None
                    and manager.active_runtime is not None
                    and callback_task_id
                ):
                    active_dispatch = manager.active_runtime.resolve_worker_task(
                        callback_task_id
                    )
                resolved_dispatch_id = (
                    (
                        active_dispatch.dispatch_id
                        if active_dispatch is not None
                        else None
                    )
                    or routed.dispatch_id
                    or callback_task_id
                )

                if routed.status == "ignored" and routed.reason != "stale_transition":
                    return {"status": "ignored", "reason": routed.reason}

                if routed.status == "ok":
                    # memory-producer: callback_status_ok; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                    event_store.append(
                        resolved_dispatch_id,
                        "status_update",
                        context_id=callback_context,
                        state=str(callback_state),
                    )
                    if active_dispatch is not None and self._task_watchdog is not None:
                        self._task_watchdog.record_state_change(
                            dispatch_id=active_dispatch.dispatch_id,
                            worker_id=active_dispatch.worker_id,
                            worker_task_id=callback_task_id,
                            state_name=str(callback_state),
                        )

                ingested_observations = 0
                projection_inputs: list = []
                observations: list[dict] = []
                if callback_result:
                    obs_with_prov = _extract_observations_with_provenance(
                        callback_result
                    )
                    observations = [o for o, _ in obs_with_prov]
                    if (
                        secure
                        and self._memory_ingestor is not None
                        and auth_dispatch is not None
                        and obs_with_prov
                    ):
                        # Production producer path: normalized structured Worker
                        # evidence flows through the canonical projection
                        # ingest/reducer atomically with the callback Temporal
                        # event (not only the Temporal callback write).
                        projection_inputs = (
                            self._normalize_observation_projection_inputs(
                                obs_with_prov,
                                dispatch=auth_dispatch,
                                context_id=callback_context,
                                worker_task_id=callback_task_id,
                                body_sha256=hashlib.sha256(body_bytes).hexdigest(),
                                runtime_epoch=auth_epoch,
                            )
                        )

                # Canonical Memory is written FIRST so the legacy observation
                # write is strictly PAIRED with it: an observation update may
                # only reach the legacy SemanticMap / EventStore when it can
                # also reach canonical Memory.  A callback not bound to an
                # authenticated active dispatch (or whose canonical write is
                # fenced by a closed/unknown scope) must not write an unpaired
                # legacy observation.  When canonical Memory is not in the
                # picture at all (pure legacy deployment), the trusted legacy
                # sink keeps writing as before.
                canonical_status = "not_configured"
                memory_enabled = secure and self._memory_ingestor is not None
                if memory_enabled and auth_dispatch is not None:
                    _canonical_result = self._ingest_callback_to_memory(
                        dispatch=auth_dispatch,
                        context_id=callback_context,
                        worker_task_id=callback_task_id,
                        callback_kind="status_update",
                        normalized_state=str(callback_state),
                        body_bytes=body_bytes,
                        body_dict=body,
                        control_receipts=_callback_receipts,
                        runtime_epoch=auth_epoch,
                        projection_inputs=projection_inputs,
                    )
                    canonical_status = getattr(_canonical_result, "status", "")
                allow_legacy_observation_write = (not memory_enabled) or (
                    auth_dispatch is not None
                    and canonical_status in ("ok", "duplicate")
                )
                if callback_result and allow_legacy_observation_write:
                    ingested_observations = self._ingest_observations_from_status(
                        callback_task_id,
                        callback_result,
                        dispatch_id=resolved_dispatch_id,
                        worker_id=(
                            active_dispatch.worker_id
                            if active_dispatch is not None
                            else ""
                        ),
                        context_id=callback_context,
                        observations=observations,
                    )
                if routed.status == "ignored" and routed.reason == "stale_transition":
                    if ingested_observations == 0:
                        return {"status": "ignored", "reason": routed.reason}
                    return {
                        "status": "ok",
                        "ingested_observations": ingested_observations,
                    }
                return {"status": "ok"}

            task_id = None
            is_terminal = False

            if sr.HasField("task"):
                t = sr.task
                task_id = t.id
                state_name = (
                    TaskState.Name(t.status.state) if t.status.state else "UNKNOWN"
                )
                is_terminal = t.status.state in (
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                )
                if task_id:
                    callback_context = t.context_id or None
                    dispatch_id = _resolve_legacy_dispatch_id(task_id)
                    # memory-producer: callback_task_legacy; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                    event_store.append(
                        dispatch_id,
                        "status_update",
                        context_id=callback_context,
                        state=state_name,
                    )
                    worker_id = _try_resolve_worker_id(task_id)
                    if worker_id:
                        self._task_watchdog.record_worker_contact(worker_id)
                    self._task_watchdog.record_state_change(
                        dispatch_id=dispatch_id,
                        worker_id=worker_id,
                        worker_task_id=task_id,
                        state_name=state_name,
                    )
                    if is_terminal:
                        self._task_watchdog.record_progress(
                            dispatch_id=dispatch_id,
                            worker_id=worker_id,
                            worker_task_id=task_id,
                            source="status_terminal",
                            step=self._barrier._step_counter if self._barrier else 0,
                        )
            elif sr.HasField("artifact_update"):
                au = sr.artifact_update
                task_id = au.task_id
                if au.HasField("artifact"):
                    texts = [p.text for p in au.artifact.parts if p.text]
                    if texts and task_id:
                        combined = " ".join(texts)
                        _push_artifact_cache.setdefault(task_id, []).extend(texts)
                        callback_context = au.context_id or None
                        dispatch_id = _resolve_legacy_dispatch_id(task_id)
                        # memory-producer: callback_artifact_legacy; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                        event_store.append(
                            dispatch_id,
                            "artifact_update",
                            context_id=callback_context,
                            text=combined,
                        )
                        worker_id = _try_resolve_worker_id(task_id)
                        if worker_id:
                            self._task_watchdog.record_worker_contact(worker_id)
                        self._task_watchdog.record_progress(
                            dispatch_id=dispatch_id,
                            worker_id=worker_id,
                            worker_task_id=task_id,
                            source="artifact_update",
                            step=self._barrier._step_counter if self._barrier else 0,
                        )
            elif sr.HasField("status_update"):
                su = sr.status_update
                task_id = su.task_id
                if su.HasField("status"):
                    state_name = (
                        TaskState.Name(su.status.state)
                        if su.status.state
                        else "UNKNOWN"
                    )
                    is_terminal = su.status.state in (
                        TaskState.TASK_STATE_COMPLETED,
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_CANCELED,
                    )
                    if task_id:
                        callback_context = su.context_id or None
                        dispatch_id = _resolve_legacy_dispatch_id(task_id)
                        # memory-producer: callback_status_legacy; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                        event_store.append(
                            dispatch_id,
                            "status_update",
                            context_id=callback_context,
                            state=state_name,
                        )
                        worker_id = _try_resolve_worker_id(task_id)
                        if worker_id:
                            self._task_watchdog.record_worker_contact(worker_id)
                        if state_name in (
                            "COMPLETED",
                            "FAILED",
                            "CANCELED",
                            "INPUT_REQUIRED",
                            "TASK_STATE_COMPLETED",
                            "TASK_STATE_FAILED",
                            "TASK_STATE_CANCELED",
                            "TASK_STATE_INPUT_REQUIRED",
                        ):
                            self._task_watchdog.record_state_change(
                                dispatch_id=dispatch_id,
                                worker_id=worker_id,
                                worker_task_id=task_id,
                                state_name=state_name,
                            )
                            if is_terminal:
                                self._task_watchdog.record_progress(
                                    dispatch_id=dispatch_id,
                                    worker_id=worker_id,
                                    worker_task_id=task_id,
                                    source="status_terminal",
                                    step=self._barrier._step_counter
                                    if self._barrier
                                    else 0,
                                )
                        if su.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                            question = ""
                            if su.status.HasField("message"):
                                question = " ".join(
                                    p.text for p in su.status.message.parts if p.text
                                )
                            # memory-producer: callback_input_required; canonical_source=memory_ingestor.callback_envelope; idempotency=callback.idempotency_key; auth=shadow
                            event_store.append(
                                dispatch_id,
                                "help_request",
                                context_id=callback_context,
                                text=question,
                            )
                            self._task_watchdog.record_progress(
                                dispatch_id=dispatch_id,
                                worker_id=worker_id,
                                worker_task_id=task_id,
                                source="input_required",
                                step=self._barrier._step_counter
                                if self._barrier
                                else 0,
                            )

                        if su.status.HasField("message"):
                            # Legacy / pre-admission path only (active_runtime
                            # is None). Active runtime branch above already
                            # returned, so this cannot dual-write.
                            status_text = " ".join(
                                p.text for p in su.status.message.parts if p.text
                            )
                            if status_text:
                                self._ingest_observations_from_status(
                                    task_id,
                                    status_text,
                                    dispatch_id=dispatch_id,
                                    worker_id=worker_id or "",
                                    context_id=callback_context,
                                )

            if task_id and is_terminal:

                async def _resolve_with_delay():
                    await asyncio.sleep(0.1)
                    parts = _push_artifact_cache.pop(task_id, [])
                    text = " ".join(parts) if parts else "(no artifact text)"
                    resolve_global_future(task_id, text)

                asyncio.create_task(_resolve_with_delay())

            return {"status": "ok"}

        @app.get("/semantic-map")
        async def semantic_map():
            if self._semantic_map is None:
                return {
                    "status": "unavailable",
                    "known_dynamic_objects": {"fires": [], "persons": []},
                }
            return self._semantic_map.snapshot()

        @app.get("/team-status")
        async def team_status(agent_id: str, proof: str = ""):
            if self._team_partition_service is None:
                return {
                    "error": "team_partition_service not configured",
                    "teammates": [],
                }

            tps = self._team_partition_service
            secret = self._coordinator_secret
            nonce_store = self._proof_nonce_store

            if secret:
                valid, reason = TeamStatusProof.verify(
                    secret,
                    proof,
                    agent_id,
                    nonce_store=nonce_store,
                )
                if not valid:
                    raise HTTPException(
                        status_code=403,
                        detail=f"team_status_unauthorized: {reason}",
                    )

            assignment = tps.get_assignment(agent_id)
            if assignment is None:
                return {
                    "teammates": [],
                    "current_step": 0,
                    "team_partition_revision": tps.team_partition_revision,
                }

            if assignment.is_singleton:
                return {
                    "teammates": [],
                    "current_step": 0,
                    "team_partition_revision": tps.team_partition_revision,
                    "team_id": assignment.team_id,
                    "epoch": assignment.epoch,
                    "is_singleton": True,
                }

            safe_view = TeamStatusProof.safe_team_view(
                team_id=assignment.team_id,
                epoch=assignment.epoch,
                member_ids=assignment.member_ids,
                team_partition_revision=tps.team_partition_revision,
            )
            return {
                "teammates": safe_view["members"],
                "team_id": safe_view["team_id"],
                "epoch": safe_view["epoch"],
                "team_partition_revision": safe_view["team_partition_revision"],
                "current_step": 0,
                "is_singleton": False,
            }

        @app.post("/environment-state")
        async def environment_state(request: Request):
            """Phase 4 authenticated worker read-port query.

            Identity is bound to the **server-issued, opaque ``worker_task_id``**
            (the A2A task id the coordinator assigned when dispatching to this
            worker), resolved server-side via ``resolve_worker_task`` — never to
            a caller-selected ``worker_id`` with a forgeable shared-secret
            proof.  The proof (HMAC over the task id) provides freshness +
            replay protection only.  The request's ``viewer_id`` / ``scope`` /
            ``dispatch`` claims are only candidates: any mismatch with the
            server-derived worker / current admission is rejected.  The renderer
            never performs ACL filtering.
            """
            body = await request.json()
            worker_task_id = str(body.get("worker_task_id", "") or "")
            proof = str(body.get("proof", "") or "")
            if not worker_task_id:
                raise HTTPException(
                    status_code=401,
                    detail="environment_state_unauthorized: missing worker_task_id",
                )

            from a2a.coordinator.team_status_auth import (
                derive_environment_state_principal,
            )

            valid, reason, principal = derive_environment_state_principal(
                self._coordinator_secret,
                worker_task_id,
                proof,
                nonce_store=self._proof_nonce_store,
            )
            if not valid:
                raise HTTPException(
                    status_code=403,
                    detail=f"environment_state_unauthorized: {reason}",
                )

            # Scope is derived from trusted admission; the body's scope claim is
            # only a candidate and must match.
            if self._memory_ingestor is None:
                raise HTTPException(
                    status_code=503,
                    detail="environment_state_unavailable: memory not configured",
                )
            active_runtime = self._mission_runtime_manager.active_runtime
            if active_runtime is None:
                raise HTTPException(
                    status_code=503,
                    detail="environment_state_unavailable: no active runtime",
                )
            runtime_epoch = active_runtime._manager.epoch
            scope_id = self._memory_ingestor.scope_id_for(
                active_runtime.context_id, runtime_epoch
            )
            claimed_scope = body.get("scope_id")
            if claimed_scope and str(claimed_scope) != scope_id:
                raise HTTPException(
                    status_code=403, detail="environment_state_scope_mismatch"
                )

            # Strict admission (fail-closed): identity is server-derived from
            # the task binding → dispatch → worker.  Unknown tasks, terminal
            # dispatches, and unregistered workers are rejected with a typed
            # 403 — never served as a degraded view.
            worker_id, dispatch_id, admission_reason = (
                self._resolve_environment_state_admission(worker_task_id)
            )
            if not worker_id:
                raise HTTPException(status_code=403, detail=admission_reason)

            # The viewer claim must match the server-derived worker; a stale /
            # cross-worker dispatch claim must match the resolved dispatch.
            claimed_viewer = body.get("viewer_id")
            if claimed_viewer and str(claimed_viewer) != worker_id:
                raise HTTPException(
                    status_code=403, detail="environment_state_worker_mismatch"
                )
            claimed_dispatch = body.get("current_dispatch_id")
            if claimed_dispatch and claimed_dispatch != dispatch_id:
                raise HTTPException(
                    status_code=403, detail="environment_state_dispatch_mismatch"
                )

            from Agent.environment_state import EnvironmentStateQuery
            from sar_orch.environment_state_provider import (
                ControlPlaneReadPort,
                EnvironmentStateProvider,
                MemoryReadPort,
            )

            provider = EnvironmentStateProvider(
                MemoryReadPort(self._memory_ingestor.store, scope_id),
                ControlPlaneReadPort(active_runtime),
                scope_id=scope_id,
                viewer_role="worker",
                viewer_id=worker_id,
                current_dispatch_id=dispatch_id,
            )
            query = EnvironmentStateQuery(
                scope_id=scope_id,
                viewer_role="worker",
                viewer_id=worker_id,
                current_dispatch_id=dispatch_id,
                temporal_cursor=int(body.get("temporal_cursor", 0) or 0),
                token_budget=int(body.get("token_budget", 0) or 0),
            )
            view = provider.query_environment_state(query)
            return _environment_state_view_to_json(view)

        @app.get("/map/state")
        async def map_state_stream(request: Request):
            """SSE 端点：实时推送 SAR 网格地图状态。"""

            async def event_generator():
                last_step = -1
                while True:
                    if await request.is_disconnected():
                        break
                    if self._barrier is None:
                        yield "event: waiting\ndata: {}\n\n"
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        snapshot = self._barrier.get_env_snapshot()
                        metrics = self._barrier.get_metrics()
                        current_step = metrics.get("steps", 0)
                        if current_step != last_step or last_step == -1:
                            last_step = current_step

                            # 序列化 position 为 tuple
                            def _serialize(obj):
                                if hasattr(obj, "get"):
                                    return obj.get()
                                return str(obj)

                            data = {
                                "step": current_step,
                                "finished": self._barrier.is_finished(),
                                "coverage": metrics.get("coverage", 0),
                                "transport_rate": metrics.get("transport_rate", 0),
                                "agents": len(snapshot.get("agents", [])),
                                "fires": len(snapshot.get("fires", [])),
                                "persons": len(snapshot.get("persons", [])),
                                "snapshot": json.loads(
                                    json.dumps(snapshot, default=_serialize)
                                ),
                            }
                            yield f"data: {json.dumps(data)}\n\n"
                    except Exception as e:
                        logger.warning(f"map/state error: {e}")
                    await asyncio.sleep(0.5)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/dashboard/stream")
        async def dashboard_stream(request: Request):
            """SSE 端点：统一仪表盘数据流，推送所有可视化所需数据。"""

            def _serialize(obj):
                if hasattr(obj, "get"):
                    return obj.get()
                if isinstance(obj, (list, tuple)):
                    return list(obj)
                return str(obj)

            async def event_generator():
                last_step = -1
                while True:
                    if await request.is_disconnected():
                        break
                    if self._barrier is None:
                        yield "event: waiting\ndata: {}\n\n"
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        metrics = self._barrier.get_metrics()
                        current_step = metrics.get("steps", 0)
                        finished = self._barrier.is_finished()
                        coverage = metrics.get("coverage", 0)
                        transport_rate = metrics.get("transport_rate", 0)

                        if current_step == last_step and last_step != -1:
                            await asyncio.sleep(0.5)
                            continue
                        last_step = current_step

                        # Collect all data sources
                        env_snapshot = self._barrier.get_env_snapshot()
                        trajectory = self._barrier.get_trajectory_history()
                        obs_stream = self._barrier.get_observation_stream(limit=40)
                        last_log = self._barrier.get_last_step_log()

                        sem_map = (
                            self._semantic_map.snapshot()
                            if self._semantic_map is not None
                            else {}
                        )

                        # Token usage from barrier accumulator (fed by coordinator callback)
                        router_tokens = {}
                        if hasattr(self, "_barrier") and self._barrier is not None:
                            acc = getattr(self._barrier, "_token_accumulator", None)
                            if acc is not None:
                                router_tokens = {
                                    "prompt_tokens": acc.get("prompt_tokens", 0),
                                    "completion_tokens": acc.get(
                                        "completion_tokens", 0
                                    ),
                                    "total_tokens": acc.get("total_tokens", 0),
                                    "cumulative_prompt": acc.get(
                                        "cumulative_prompt", 0
                                    ),
                                    "cumulative_completion": acc.get(
                                        "cumulative_completion", 0
                                    ),
                                    "cumulative_total": acc.get("cumulative_total", 0),
                                }

                        data = {
                            "step": current_step,
                            "finished": finished,
                            "coverage": round(coverage, 4),
                            "transport_rate": round(transport_rate, 4),
                            "env_snapshot": env_snapshot,
                            "semantic_map": sem_map,
                            "trajectory_history": trajectory,
                            "observation_stream": obs_stream,
                            "last_step_log": last_log,
                            "router_tokens": router_tokens,
                        }

                        yield f"data: {json.dumps(data, default=_serialize)}\n\n"
                    except Exception as e:
                        logger.warning("dashboard/stream error: %s", e)
                    await asyncio.sleep(0.5)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post("/api/a2a/jsonrpc")
        async def proxy_a2a_jsonrpc(request: Request):
            """将 JSON-RPC 请求流式代理到 A2A Server (8081)，支持 SSE。"""
            body = await request.body()
            headers = {
                "content-type": request.headers.get("content-type", "application/json"),
                "a2a-version": "1.0",
            }
            a2a_url = f"http://127.0.0.1:{self._a2a_port}/api/v1/jsonrpc/"

            async def _stream():
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(600.0)
                    ) as client:
                        async with client.stream(
                            "POST", a2a_url, content=body, headers=headers
                        ) as resp:
                            if resp.status_code >= 400:
                                # 上游返回错误，包装为 JSON-RPC error 并终止流
                                err = json.dumps(
                                    {
                                        "jsonrpc": "2.0",
                                        "error": {
                                            "code": -32000,
                                            "message": f"A2A server returned HTTP {resp.status_code}",
                                        },
                                    }
                                )
                                yield f"data: {err}\n\n".encode()
                                return
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                except httpx.ConnectError:
                    err = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32000,
                                "message": "A2A server not reachable",
                            },
                        }
                    )
                    yield f"data: {err}\n\n".encode()

            return StreamingResponse(_stream(), media_type="text/event-stream")

        @app.websocket("/ws/worker/{worker_id}")
        async def worker_ws(websocket: WebSocket, worker_id: str):
            await websocket.accept()
            async with self._worker_ws_lock:
                self._worker_ws[worker_id] = websocket
            try:
                while True:
                    data = await websocket.receive_text()
                    await self._handle_worker_message(worker_id, json.loads(data))
            except WebSocketDisconnect:
                async with self._worker_ws_lock:
                    self._worker_ws.pop(worker_id, None)
                try:
                    running_tasks = self._task_queue.list_running_by_worker(worker_id)
                    for task in running_tasks:
                        task.status = TaskStatus.PENDING
                        task.assigned_worker = None
                        task.updated_at = datetime.now(timezone.utc)
                        logger.info(
                            f"[Coordinator] Reset task {task.task_id} from disconnected worker {worker_id}"
                        )
                except Exception as e:
                    logger.error(f"Error resetting tasks for worker {worker_id}: {e}")
                finally:
                    # Best effort unregister - ensure cleanup even if requeue had issues
                    try:
                        self._registry.unregister(worker_id)
                        self._agent_registry.unregister_worker(worker_id)
                    except Exception as e:
                        logger.warning(f"Failed to unregister worker {worker_id}: {e}")

        # Mount Map Agent MCP server if semantic map is available
        if self._semantic_map is not None:
            mount_map_agent_mcp(app, self._semantic_map)
            logger.info("Map Agent MCP server mounted at /mcp/map")

        return app

    async def _handle_worker_message(self, worker_id: str, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type")
        payload = msg.get("payload", {})

        if msg_type == WS_REGISTER:
            a2a_endpoint = payload["a2a_endpoint"]

            # 1. WorkerRegistry 记录基本信息（仅连通性）
            self._registry.register_from_ws(worker_id, a2a_endpoint)

            # 2. 通过 A2A 协议拉取 AgentCard（带重试，处理 A2A server 启动时序竞争）
            agent_card_url = f"{a2a_endpoint.rstrip('/')}/.well-known/agent-card.json"
            card_data = await self._fetch_agent_card(
                agent_card_url, retries=3, delay=1.0
            )

            # 3. 解析 AgentCard 并注册到 AgentRegistry
            if card_data:
                self._agent_registry.register_from_agent_card(
                    worker_id=worker_id,
                    endpoint=a2a_endpoint,
                    agent_card=card_data,
                )
            else:
                logger.warning(
                    f"Failed to fetch AgentCard for {worker_id}, using minimal registration"
                )
                self._agent_registry.register(
                    AgentInfo(
                        agent_id=worker_id,
                        description=f"Worker {worker_id} (AgentCard unavailable)",
                        endpoint=a2a_endpoint,
                        status=AgentStatus.ONLINE,
                    )
                )

            # 4. 建立 TeamPartition singleton
            self._team_partition_service.ensure_singletons([worker_id])

        elif msg_type == WS_HEARTBEAT:
            self._registry.update_heartbeat(worker_id)
            self._agent_registry.update_heartbeat_from_worker(worker_id)
            if self._task_watchdog is not None:
                self._task_watchdog.record_worker_contact(worker_id)

        elif msg_type == WS_TASK_PROGRESS:
            task_id = payload.get("task_id")
            event_data = payload.get("event_data", {})
            logger.info(
                f"[Coordinator] Task {task_id} progress from {worker_id}: {event_data}"
            )

        elif msg_type == WS_RELAY_A2A:
            # A2A 中转请求 - Worker 请求直连到其他 Worker
            to_agent_id = payload.get("to_agent_id")
            # 始终使用 worker_id（来自 WebSocket 连接），不接受 payload 中的 from_worker_id
            from_worker_id = worker_id
            if to_agent_id:
                try:
                    info = await self._mesh_guide.relay_via_coordinator(
                        from_worker_id, to_agent_id
                    )
                    async with self._worker_ws_lock:
                        target_ws = self._worker_ws.get(to_agent_id)
                    if target_ws:
                        await target_ws.send_json(
                            {
                                "type": "incoming_relay_request",
                                "payload": {
                                    "from_worker_id": from_worker_id,
                                    "relay_info": info,
                                },
                            }
                        )
                        await self._send_to_worker(
                            from_worker_id,
                            {
                                "type": "relay_initiated",
                                "payload": {
                                    "to_agent_id": to_agent_id,
                                    "relay_info": info,
                                },
                            },
                        )
                    else:
                        # 目标未连接，告知请求者
                        await self._send_to_worker(
                            from_worker_id,
                            {
                                "type": "relay_failed",
                                "payload": {
                                    "error": f"Agent {to_agent_id} is not connected"
                                },
                            },
                        )
                except AgentNotFoundError:
                    logger.warning(f"Agent {to_agent_id} not found for relay request")
                    await self._send_to_worker(
                        from_worker_id,
                        {
                            "type": "relay_failed",
                            "payload": {"error": f"Agent {to_agent_id} not found"},
                        },
                    )

        elif msg_type == WS_CANCEL_TASK:
            task_id = payload.get("task_id")
            if task_id:
                try:
                    task = self._task_queue.get(task_id)
                    assigned_worker = task.assigned_worker
                    self._task_queue.cancel(task_id)
                    logger.info(f"[Coordinator] Task {task_id} cancelled")

                    if assigned_worker and assigned_worker in self._worker_ws:
                        async with self._worker_ws_lock:
                            await self._send_to_worker(
                                assigned_worker,
                                {
                                    "type": WS_CANCEL_TASK,
                                    "payload": {"task_id": task_id},
                                },
                            )
                except TaskNotFoundError:
                    logger.warning(f"Cannot cancel task {task_id}: not found")
                except InvalidStatusTransitionError:
                    logger.warning(
                        f"Cannot cancel task {task_id}: invalid state transition"
                    )
                except Exception as e:
                    logger.error(f"Unexpected error cancelling task {task_id}: {e}")

    async def _fetch_agent_card(
        self, url: str, retries: int = 3, delay: float = 1.0
    ) -> dict | None:
        """从 Worker 拉取 AgentCard，带重试（处理 A2A server 启动时序竞争）。

        Args:
            url: AgentCard URL
            retries: 最大重试次数
            delay: 重试间隔（秒）

        Returns:
            AgentCard JSON dict，失败返回 None
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(retries):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    logger.info(f"Fetched AgentCard from {url}")
                    return resp.json()
                except Exception as e:
                    logger.warning(
                        f"AgentCard fetch attempt {attempt + 1}/{retries} "
                        f"for {url} failed: {e}"
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
        return None

    async def _send_to_worker(self, worker_id: str, msg: Dict[str, Any]) -> bool:
        async with self._worker_ws_lock:
            ws = self._worker_ws.get(worker_id)
        if ws is None:
            return False
        await ws.send_json(msg)
        return True

    async def _start_cleanup_task(self) -> None:
        """启动后台心跳清理任务。"""

        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    # Cancellation requested - exit cleanly
                    break
                try:
                    await self._registry._check_heartbeats()
                    logger.debug("[Coordinator] Heartbeat cleanup completed")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[Coordinator] Heartbeat cleanup error: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def _stop_cleanup_task(self) -> None:
        """停止后台清理任务。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    def run(self) -> None:
        config = uvicorn.Config(self._app, host=self._host, port=self._port)
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.run()

    async def _shutdown_on_owner_loop(self) -> None:
        await self._mission_runtime_manager.abort("coordinator_shutdown")
        # Stop the embedded A2A server first.  Uvicorn waits for its own open
        # HTTP connections before entering the outer lifespan; an active A2A
        # stream would otherwise keep that lifespan from reaching the child
        # server's ActiveTask finalizer.
        if self._a2a_server is not None:
            request_handler = getattr(self._a2a_server, "request_handler", None)
            if request_handler is not None:
                await shutdown_a2a_active_tasks(request_handler)
            self._a2a_server.should_exit = True
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    async def shutdown(self) -> None:
        """Shutdown on the loop that owns runtime Futures."""
        owner_loop = self._owner_loop
        current_loop = asyncio.get_running_loop()
        if (
            owner_loop is not None
            and owner_loop is not current_loop
            and owner_loop.is_running()
        ):
            handoff = asyncio.run_coroutine_threadsafe(
                self._shutdown_on_owner_loop(), owner_loop
            )
            await asyncio.wrap_future(handoff)
            return
        await self._shutdown_on_owner_loop()


def create_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    a2a_port: int = 8081,
    config_path: str | None = None,
    prompts_dir: str | None = None,
    tools_dir: str | None = None,
    extra_tools: list | None = None,
    skills_dir: str | None = None,
    log_dir: str | None = None,
    router_model: str = "deepseek-v4-flash",
    router_max_steps: int = 15,
    router_temperature: float = 0.7,
    router_provider: str = "anthropic",
    router_api_base: str = "https://api.anthropic.com",
    router_api_key_env: str = "ANTHROPIC_API_KEY",
    verifier_model: str | None = None,
    verifier_max_steps: int = 10,
    verifier_temperature: float = 0.3,
    verifier_enabled: bool = True,
    max_tasks_per_run: int = 20,
    orchestration_timeout: int = 600,
    orchestration_mode: str = "agentic",
    router_step_callback=None,
    context_config=None,
    token_limit: int = 80000,
    sandbox_policy=None,
    require_explicit_completion: bool = False,
    state_provider=None,
    supervision_state_store=None,
    task_watchdog=None,
    watchdog_config: WatchdogConfig | None = None,
    mission_runtime_manager: MissionRuntimeManager | None = None,
    control_state_path: str | None = None,
    # Phase 4: signed task dispatch
    coordinator_secret: bytes | None = None,
    coordinator_id: str = "Coordinator",
    # Phase 2: authenticated Temporal shadow write
    memory_read_mode: str = "legacy",
    callback_secret: bytes | None = None,
    memory_config=None,
    memory_ingestor=None,
    ui_dir: str | None = None,
) -> CoordinatorServer:
    return CoordinatorServer(
        sandbox_policy=sandbox_policy,
        host=host,
        port=port,
        a2a_port=a2a_port,
        config_path=config_path,
        prompts_dir=prompts_dir,
        tools_dir=tools_dir,
        extra_tools=extra_tools,
        skills_dir=skills_dir,
        log_dir=log_dir,
        router_model=router_model,
        router_max_steps=router_max_steps,
        router_temperature=router_temperature,
        router_provider=router_provider,
        router_api_base=router_api_base,
        router_api_key_env=router_api_key_env,
        verifier_model=verifier_model,
        verifier_max_steps=verifier_max_steps,
        verifier_temperature=verifier_temperature,
        verifier_enabled=verifier_enabled,
        max_tasks_per_run=max_tasks_per_run,
        orchestration_timeout=orchestration_timeout,
        orchestration_mode=orchestration_mode,
        router_step_callback=router_step_callback,
        context_config=context_config,
        token_limit=token_limit,
        require_explicit_completion=require_explicit_completion,
        state_provider=state_provider,
        supervision_state_store=supervision_state_store,
        task_watchdog=task_watchdog,
        watchdog_config=watchdog_config,
        mission_runtime_manager=mission_runtime_manager,
        control_state_path=control_state_path,
        coordinator_secret=coordinator_secret,
        coordinator_id=coordinator_id,
        memory_read_mode=memory_read_mode,
        callback_secret=callback_secret,
        memory_config=memory_config,
        memory_ingestor=memory_ingestor,
        ui_dir=ui_dir,
    )

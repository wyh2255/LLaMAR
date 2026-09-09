from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from Agent.router_agent.context import ContextConfig

from langchain_openai import ChatOpenAI

from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from sar_orch.map import SemanticMapStore
from sar_orch.map_agent import set_llm_client, set_token_sink
from a2a.coordinator.supervision_state_store import SupervisionStateStore
from sar_orch.tools.coordinator import QuerySARStateTool

logger = logging.getLogger(__name__)


def _validate_long_term_mode_combo(memory_read_mode: str, long_term_mode: str) -> None:
    """Cross-validate ``memory_read_mode`` / ``long_term_mode`` (fail closed).

    ``memory_read_mode="shadow"`` + ``long_term_mode="read"`` is forbidden:
    the H2 shadow compare would surface long-term reads as non-allowlist
    diffs (``.long_term_memory``) and pollute the rollout audit (P5 review
    M-1).  Allowed combinations keep working: ``read_port`` + ``read`` (G3
    target), ``shadow`` + ``shadow``/``off``, ``read_port`` + ``shadow``/``off``.
    """
    if memory_read_mode == "shadow" and long_term_mode == "read":
        from a2a.coordinator.memory.contracts import MemoryConfigError

        raise MemoryConfigError(
            "invalid_mode_combo",
            "memory_read_mode='shadow' + long_term_mode='read' is forbidden: "
            "shadow-compare mode must not inject long-term reads into Context "
            "(read_port+read is the supported read-injection combo)",
        )


class SARCoordinator:
    """SAR Coordinator — wraps the A2A CoordinatorServer with SAR-specific tools and prompts."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        barrier=None,  # SARBarrier instance
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        prompts_dir: str | None = None,
        log_dir: str | None = None,
        supervision_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
        sandbox_policy=None,
        state_mode: str = "semantic",
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        max_steps: int = 50,
        map_summary_path: str | Path | None = None,
        # Phase 2: authenticated Temporal shadow write; default read_port
        # since H3 retirement approval (2026-08-10).
        memory_read_mode: str = "read_port",
        run_id: str | None = None,
        # Phase 4: run-local long-term memory mode (off|shadow|read).  ``off``
        # performs zero long-term DB I/O; shadow/read persist published
        # reflection products but never inject them into Context (P5).
        long_term_mode: str = "off",
        # Phase 4 (P4): optional ``[diagnosis]`` tunables from
        # ``long_term.config`` (DiagnosisRuntimeConfig).  ``None`` keeps the
        # frozen DiagnosisConfig defaults; the diagnosis channel stays
        # disabled when the store cannot be opened (fail-closed, never
        # blocks the coordinator).
        diagnosis_tunables=None,
    ):
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._barrier = barrier
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._orchestration_mode = orchestration_mode
        self._prompts_dir = prompts_dir
        self._log_dir = log_dir
        self._supervision_dir = supervision_dir
        self._exp_logger = exp_logger
        self._sandbox_policy = sandbox_policy
        self._state_mode = state_mode
        self._max_steps = max_steps
        self._map_summary_path = Path(map_summary_path) if map_summary_path else None
        self._enable_peer_mail = enable_peer_mail
        self._coordinator_secret = coordinator_secret
        self._memory_read_mode = memory_read_mode
        self._run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self._long_term_mode = long_term_mode
        # Phase 4 (P4): ``[diagnosis]`` tunables consumed in start() when the
        # run-local diagnosis store is assembled (None → DiagnosisConfig
        # defaults).
        self._diagnosis_tunables = diagnosis_tunables
        # P5 review M-1: shadow compare + long-term read injection would
        # produce non-allowlist diffs (.long_term_memory) in the H2 audit
        # trail — fail closed at construction time (typed error).
        _validate_long_term_mode_combo(memory_read_mode, long_term_mode)
        if enable_peer_mail:
            if coordinator_secret is None or len(coordinator_secret) < 16:
                raise ValueError(
                    f"coordinator_secret must be >= 16 bytes when enable_peer_mail=True, "
                    f"got {len(coordinator_secret) if coordinator_secret else 0}"
                )
        # Phase 2: secure memory modes fail closed without a protected secret.
        if self._memory_read_mode in ("shadow", "read_port"):
            if (
                coordinator_secret is None
                or not isinstance(coordinator_secret, bytes)
                or len(coordinator_secret) < 16
            ):
                from a2a.coordinator.memory.callback_auth import (
                    MemoryAuthNotConfiguredError,
                )

                raise MemoryAuthNotConfiguredError(
                    "memory_auth_not_configured: secure memory mode requires a "
                    "protected coordinator callback secret (>= 16 bytes)"
                )

        self._dispatch_seq = 0
        self._tool_seq = 0
        self._pending_router_tool: dict[str, dict] = {}
        self._server = None
        self._thread = None
        self._semantic_map = None
        self._team_registry = None
        self._sender = None
        # P1: canonical coordinator-decision event producer (set in start()
        # when memory_read_mode is shadow/read_port; None → decision events
        # are skipped silently, the legacy logs path is unaffected).
        self._memory_ingestor = None

    def _initial_step_budget(self) -> dict[str, int]:
        return {
            "current_step": 0,
            "max_steps": self._max_steps,
            "remaining": self._max_steps,
        }

    def _log_send_message(self, step: int, args: dict) -> None:
        """Log the underlying semantic event for a send_message tool call.

        P1 (main plan §3.1): besides the legacy logs, each of the four
        decision kinds (assign_task / reply_to_help / cancel_task /
        activate_plan_node) appends one canonical ``coordinator_decision.*``
        Temporal event through the memory ingestor; the generic else branch
        never enters the canonical stream (D1).
        """
        if self._exp_logger is None:
            return
        message_type = args.get("message_type", "unknown")
        content = args.get("content", "")
        who = args.get("who", "")
        related_task_id = args.get("related_task_id", "")
        if message_type == "assign_task":
            self._dispatch_seq += 1
            correlation_id = f"coordinator-dispatch-{self._dispatch_seq}"
            worker_task_id = f"dispatch-{self._dispatch_seq}"
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=content,
                assigned_to=who,
                correlation_id=correlation_id,
                worker_task_id=worker_task_id,
                event_type="assign_task",
            )
            self._exp_logger.log_subtask(
                subtask_id=worker_task_id,
                status="assigned",
                step=step,
                assigned_to=who,
                subtask=content,
            )
            self._exp_logger.log_event(
                "assign_task",
                step=step,
                agent="Coordinator",
                correlation_id=correlation_id,
                payload=args,
            )
            self._append_decision_event(
                "coordinator_decision.assign_task",
                step=step,
                payload={
                    "content": content,
                    "who": who,
                    "correlation_id": correlation_id,
                    "worker_task_id": worker_task_id,
                    "env_step": step,
                },
                correlation_id=correlation_id,
                worker_task_id=worker_task_id,
            )
        elif message_type == "reply_to_help":
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=f"reply_to_help(task_id={related_task_id})",
                assigned_to="Worker",
                event_type="reply_to_help",
            )
            self._exp_logger.log_event(
                "reply_to_help",
                step=step,
                agent="Coordinator",
                payload={
                    "related_task_id": related_task_id,
                    "response_preview": content[:200],
                },
            )
            self._append_decision_event(
                "coordinator_decision.reply_to_help",
                step=step,
                payload={
                    "related_task_id": related_task_id,
                    "response_preview": content[:200],
                    "env_step": step,
                },
            )
        elif message_type == "cancel_task":
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=f"cancel_task(task_id={related_task_id})",
                assigned_to="Worker",
                event_type="cancel_task",
            )
            self._exp_logger.log_subtask(
                subtask_id=related_task_id,
                status="canceled",
                step=step,
                assigned_to="Worker",
                subtask=f"cancel_task(task_id={related_task_id})",
            )
            self._exp_logger.log_event(
                "cancel_task",
                step=step,
                agent="Coordinator",
                payload={"related_task_id": related_task_id},
            )
            self._append_decision_event(
                "coordinator_decision.cancel_task",
                step=step,
                payload={"related_task_id": related_task_id, "env_step": step},
            )
        elif message_type == "activate_plan_node":
            # P1: DAG node activation — payload = related_task_id only
            # (participants/objective live in the MissionGraph declaration).
            self._exp_logger.log_router_interaction(
                step=step,
                subtask=f"activate_plan_node(task_id={related_task_id})",
                assigned_to="Coordinator",
                event_type="activate_plan_node",
            )
            self._exp_logger.log_event(
                "send_message",
                step=step,
                agent="Coordinator",
                payload=args,
            )
            self._append_decision_event(
                "coordinator_decision.activate_plan_node",
                step=step,
                payload={"related_task_id": related_task_id, "env_step": step},
            )
        else:
            self._exp_logger.log_event(
                "send_message",
                step=step,
                agent="Coordinator",
                payload=args,
            )

    def _append_decision_event(
        self,
        event_type: str,
        *,
        step: int,
        payload: dict,
        correlation_id: str | None = None,
        worker_task_id: str | None = None,
    ) -> None:
        """Best-effort canonical coordinator-decision Temporal event (P1).

        Skips silently when the memory ingestor is not configured or no
        canonical scope is resolvable; the DTO validates the payload
        fail-closed and the ingestor redacts it before the write (R3).  Any
        failure is logged but never raises into the router callback.
        """
        ingestor = getattr(self, "_memory_ingestor", None)
        if ingestor is None:
            return
        scope_id = self._resolve_long_term_scope_id()
        if scope_id is None:
            return
        try:
            from a2a.coordinator.memory.contracts import DecisionEventV1
            from a2a.coordinator.memory.ingestor import (
                coordinator_decision_idempotency_key,
            )

            fields = {k: v for k, v in payload.items() if k != "env_step"}
            evt = DecisionEventV1(
                event_type=event_type,
                actor_id="Coordinator",
                env_step=step,
                **fields,
            ).validate()
            key = coordinator_decision_idempotency_key(
                scope_id=scope_id,
                event_type=event_type,
                correlation_id=correlation_id or "",
                worker_task_id=worker_task_id or "",
                content=str(payload.get("content", "") or ""),
            )
            ingestor.append_decision_event(
                scope_id=scope_id,
                event_type=event_type,
                payload=evt.canonical_payload(),
                correlation_id=correlation_id,
                worker_task_id=worker_task_id,
                idempotency_key=key,
            )
        except Exception:  # canonical write is best-effort; never raise into the router callback
            logger.exception(
                "coordinator decision event append failed event_type=%s", event_type
            )

    def _summarize_plan_node(self, node: dict) -> dict:
        """A3: declarative commit summary of one ``update_plan`` node.

        Maps the UpdatePlanTool schema (``task_id`` / ``participant_ids`` /
        ``depends_on`` / ``objective``) to the decision-event summary keys
        (``logical_id`` / ``participants`` / ``deps`` / ``objective``).
        tool_start cannot diff — this is the submitted declaration, not the
        MissionGraph.replace result.
        """
        return {
            "logical_id": str(node.get("task_id", "")),
            "participants": list(node.get("participant_ids", []) or []),
            "deps": list(node.get("depends_on", []) or []),
            "objective": str(node.get("objective", "")),
        }

    def _log_router_outcome(
        self,
        step: int,
        tool_name: str,
        success: bool,
        error_code: str,
    ) -> None:
        """Phase 5: record the router tool outcome into router_interactions.csv.

        Called after the router ``tool_result`` event; ``success`` and the
        public ``error_code`` are written as the Phase 5 {Success, ErrorType}
        outcome row.  The failed ToolResult's raw error text never reaches the
        experiment logger.
        """
        if self._exp_logger is None:
            return
        self._exp_logger.log_router_interaction(
            step=step,
            subtask=f"{tool_name}()",
            assigned_to="Coordinator",
            event_type=f"{tool_name}_result",
            success=success,
            error_code=error_code,
        )

    def _on_router_event(self, event_type: str, **kw) -> None:
        """Router agent step_callback: dispatches + outcome logging."""
        if self._exp_logger is None:
            return
        step = getattr(self._barrier, "_step_counter", 0)
        if event_type == "llm_response":
            usage = kw.get("usage")
            if usage is not None:
                self._exp_logger.log_token_usage(
                    step=step,
                    agent="Coordinator",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                )
        elif event_type == "tool_start":
            tool_name = kw.get("tool_name", "")
            args = kw.get("arguments", {})
            if tool_name == "send_message":
                self._log_send_message(step, args)
            elif tool_name == "update_plan":
                # P1 / A3: declarative commit summary — the plan list as
                # submitted at tool_start (no before/after diff available).
                plan = args.get("plan", [])
                self._exp_logger.log_router_interaction(
                    step=step,
                    subtask=f"update_plan(nodes={len(plan)})",
                    assigned_to="Coordinator",
                    event_type="update_plan",
                )
                self._append_decision_event(
                    "coordinator_decision.update_plan",
                    step=step,
                    payload={
                        "plan_nodes": [
                            self._summarize_plan_node(node) for node in plan
                        ],
                        "env_step": step,
                    },
                )
            elif tool_name == "query_sar_state":
                self._tool_seq += 1
                corr_id = f"coord-tool-{self._tool_seq}"
                self._pending_router_tool[tool_name] = {
                    "correlation_id": corr_id,
                    "step": step,
                }
                self._exp_logger.log_router_interaction(
                    step=step,
                    subtask="query_sar_state()",
                    assigned_to="Coordinator",
                    correlation_id=corr_id,
                    event_type="query_sar_state",
                )
            elif tool_name == "query_task_events":
                self._exp_logger.log_router_interaction(
                    step=step,
                    subtask=f"query_task_events(task_ids={args.get('task_ids', [])})",
                    assigned_to="Coordinator",
                    event_type="query_task_events",
                )
            elif tool_name == "finish_task":
                self._exp_logger.log_router_interaction(
                    step=step,
                    subtask="finish_task()",
                    assigned_to="Coordinator",
                    event_type="finish_task",
                )
                # M8: mission-level terminal row — completed/failed by the
                # submitted ``success`` flag (append-only, no backfill).
                self._exp_logger.log_subtask(
                    subtask_id="mission",
                    status="completed" if args.get("success") else "failed",
                    step=step,
                    assigned_to="Coordinator",
                    subtask=str(args.get("summary", ""))[:200],
                )
        elif event_type == "tool_result":
            tool_name = kw.get("tool_name", "")
            success = kw.get("success", True)
            error_code = kw.get("error_code", "")
            self._log_router_outcome(step, tool_name, success, error_code)
            if tool_name == "query_sar_state":
                content = kw.get("content", "")
                pending = self._pending_router_tool.pop(tool_name, {})
                self._exp_logger.log_coordinator_state(
                    step=step,
                    state_summary=(content or "")[:2000],
                    correlation_id=pending.get("correlation_id", ""),
                )

    async def start(self):
        """Start the coordinator server in a background thread."""
        from a2a.coordinator.server import create_server
        import threading

        # Build semantic map store from Worker evidence composition only.
        # Phase 3 (H1-INV-1): the online semantic map is never seeded from
        # simulator/Barrier scene priors or checker ground truth.  Reservoirs /
        # deposits / fires / persons / agent positions are discovered through
        # authenticated Worker observations; the map starts empty and is
        # composed purely from Worker evidence.
        semantic_map = SemanticMapStore()
        semantic_map.set_jsonl_path(
            Path(self._log_dir) / "semantic_map.jsonl" if self._log_dir else None
        )
        _agent_names = (
            getattr(getattr(self._barrier, "env", None), "agent_names", [])
            if self._barrier is not None
            else []
        )
        semantic_map.init_priors(
            reservoirs=[],
            deposits=[],
            agents=[{"agent_id": name} for name in _agent_names],
            rules={"Chemical": "Sand", "Non-chemical": "Water"},
            step_budget=self._initial_step_budget(),
            task_objective="Extinguish all fires and rescue all persons",
        )

        self._semantic_map = semantic_map

        from a2a.coordinator.event_store import event_store

        supervision_state_store = SupervisionStateStore(
            log_dir=str(Path(self._supervision_dir))
            if self._supervision_dir
            else (str(Path(self._log_dir)) if self._log_dir else None)
        )

        # Phase 6: construct MapSummarizer in semantic mode when dependencies exist
        map_summarizer = None
        if self._state_mode == "semantic":
            if self._map_summary_path is not None and self._exp_logger is not None:
                from sar_orch.map.summarizer import MapSummarizer

                def _map_summary_token_sink(**kwargs):
                    step = (
                        getattr(self._barrier, "_step_counter", 0)
                        if self._barrier is not None
                        else 0
                    )
                    self._exp_logger.log_token_usage(
                        step=step,
                        agent=kwargs.get("agent", "MapSummarizer"),
                        prompt_tokens=kwargs.get("prompt_tokens", 0),
                        completion_tokens=kwargs.get("completion_tokens", 0),
                        total_tokens=kwargs.get("total_tokens", 0),
                        cache_hit_tokens=kwargs.get("cache_hit_tokens", 0),
                        cache_miss_tokens=kwargs.get("cache_miss_tokens", 0),
                    )
                    self._exp_logger.flush_summary()

                map_summarizer = MapSummarizer(
                    summary_path=self._map_summary_path,
                    token_usage_sink=_map_summary_token_sink,
                )
            else:
                logger.warning(
                    "MapSummarizer disabled: %s %s",
                    "no map_summary_path" if self._map_summary_path is None else "",
                    "no exp_logger" if self._exp_logger is None else "",
                )

        # Phase 4: run-local long-term memory store.  Assembled only when
        # ``long_term_mode != off``; the root is derived from the same
        # ``MemoryConfig.memory_root`` (``<memory_root>/long_term/
        # long_term.sqlite3``) — no separate root parameter.  Created BEFORE
        # the state provider so the Phase 5 read-port provider can receive
        # the store + mode at construction time.
        self._long_term_store = None
        if self._long_term_mode != "off":
            if not self._log_dir:
                from a2a.coordinator.memory.contracts import MemoryConfigError

                raise MemoryConfigError(
                    "invalid_memory_root",
                    "long_term_mode != off requires log_dir (memory_root "
                    "derivation for the run-local long-term DB)",
                )
            from a2a.coordinator.memory.contracts import LongTermMemoryConfig
            from a2a.coordinator.memory.long_term import LongTermMemoryStore

            lt_config = LongTermMemoryConfig(
                experiment_id=self._run_id,
                memory_root=Path(self._log_dir),
                long_term_mode=self._long_term_mode,
            ).validate()
            self._long_term_store = LongTermMemoryStore(
                lt_config.long_term_db_path
            ).open()
            logger.info(
                "long-term memory store opened (mode=%s): %s",
                self._long_term_mode,
                lt_config.long_term_db_path,
            )

        # Phase 4 (P4): run-local diagnosis store (main plan §3.2 / D6).
        # Independent short-lived store at ``<memory_root>/diagnosis/
        # diagnosis.sqlite3``; assembled when long_term_mode != off (the
        # diagnosis channel rides the same rolling trigger).  Fail-closed:
        # an unopenable store disables the channel with a typed warning —
        # it never blocks coordinator startup.
        self._diagnosis_store = None
        self._diagnosis_config = None
        if self._long_term_mode != "off":
            # log_dir 守卫已在上方 long_term 块（同一 mode 分支）执行——这里
            # 不再重复（review Minor 2：前者必先触发，重复块是死代码）。
            from a2a.coordinator.memory.contracts import (
                DiagnosisConfig,
                DiagnosisRuntimeConfig,
            )
            from a2a.coordinator.memory.diagnosis import DiagnosisMemoryStore

            tunables = self._diagnosis_tunables or DiagnosisRuntimeConfig()
            diag_config = DiagnosisConfig(
                experiment_id=self._run_id,
                memory_root=Path(self._log_dir),
                inject_enabled=tunables.inject_enabled,
                min_confidence=tunables.min_confidence,
                max_rounds=tunables.max_rounds,
                diagnosis_sec=tunables.diagnosis_sec,
                # R3 修订: system_health 段固定上限预算档（默认 3，与
                # long_term_memory 同档）——可配置化。
                section_budget_threshold=tunables.section_budget_threshold,
            ).validate()
            try:
                self._diagnosis_store = DiagnosisMemoryStore(
                    diag_config.diagnosis_db_path
                ).open()
            except Exception as exc:  # noqa: BLE001 - fail-closed channel
                logger.warning(
                    "diagnosis store open failed — diagnosis channel disabled: %s",
                    exc,
                )
                self._diagnosis_store = None
                self._diagnosis_config = None
            else:
                self._diagnosis_config = diag_config
                logger.info(
                    "diagnosis store opened (inject_enabled=%s): %s",
                    diag_config.inject_enabled,
                    diag_config.diagnosis_db_path,
                )

        state_provider = SARCoordinatorStateProvider(
            barrier=self._barrier,
            semantic_map=semantic_map,
            event_store=event_store,
            state_mode=self._state_mode,
            supervision_state_store=supervision_state_store,
            map_summarizer=map_summarizer,
            log_dir=str(Path(self._log_dir)) if self._log_dir else None,
            memory_read_mode=self._memory_read_mode,
            # Phase 5 #2: pass the long-term store + mode through to the
            # read-port provider (mode=off → store is None → provider stays
            # on its default off path).
            long_term_mode=self._long_term_mode,
            long_term_store=self._long_term_store,
            # Phase 4 (P4): pass the diagnosis store + injection knob
            # through to the read-port provider (None store → the
            # system_health section never materializes).
            diagnosis_store=self._diagnosis_store,
            diagnosis_inject_enabled=bool(
                self._diagnosis_config is not None
                and self._diagnosis_config.inject_enabled
            ),
            diagnosis_min_confidence=(
                self._diagnosis_config.min_confidence
                if self._diagnosis_config is not None
                else 0.6
            ),
            # R3 修订: system_health 预算档透传（诊断通道未启用时保持默认 3）。
            diagnosis_budget_threshold=(
                self._diagnosis_config.section_budget_threshold
                if self._diagnosis_config is not None
                else 3
            ),
        )
        self._state_provider = state_provider
        self._supervision_state_store = supervision_state_store

        # Phase 2: authenticated Temporal shadow write — MemoryConfig derived
        # from explicit run_id / log root, never inferred from a callback.
        memory_ingestor = None
        memory_config = None
        if self._memory_read_mode in ("shadow", "read_port"):
            if not self._log_dir:
                from a2a.coordinator.memory.callback_auth import (
                    MemoryAuthNotConfiguredError,
                )

                raise MemoryAuthNotConfiguredError(
                    "memory_auth_not_configured: shadow/read_port requires log_dir"
                )
            from a2a.coordinator.memory.contracts import MemoryConfig
            from a2a.coordinator.memory.ingestor import (
                MemoryIngestor,
                MemoryScopeFactory,
            )
            from a2a.coordinator.memory.store import MemoryStore

            memory_config = MemoryConfig(
                experiment_id=self._run_id,
                memory_root=Path(self._log_dir),
            ).validate()
            memory_store = MemoryStore(memory_config.db_path)
            memory_ingestor = MemoryIngestor(
                memory_store,
                MemoryScopeFactory(memory_config),
            )
            self._memory_store = memory_store
            self._memory_ingestor = memory_ingestor
            # Phase 4: inject the coordinator read-port adapter (system
            # principal).  The concrete provider is built lazily when the
            # MissionRuntime is admitted (see set_runtime); it only activates
            # in read_port mode.
            state_provider.set_memory_ingestor(memory_ingestor)

        extra_tools: list = []
        if self._state_mode == "oracle":
            extra_tools.append(QuerySARStateTool(self._barrier))

        # Router step_callback for logging subtask dispatches + coordinator token usage
        def _router_cb(event_type: str, **kw):
            self._on_router_event(event_type, **kw)

        # SAR UI static files live alongside the orchestration code (sar_orch/ui/).
        _sar_ui_dir = Path(__file__).parent / "ui"

        self._server = create_server(
            host=self._host,
            port=self._port,
            a2a_port=self._a2a_port,
            router_model=self._model,
            router_provider=self._provider,
            router_api_base=self._api_base,
            router_api_key_env=self._api_key_env,
            router_max_steps=200,
            orchestration_mode=self._orchestration_mode,
            router_temperature=0.7,
            prompts_dir=self._prompts_dir,
            skills_dir=str(
                Path(self._prompts_dir).parent.parent / "skills" / "coordinator"
            )
            if self._prompts_dir
            else None,
            # Do NOT pass tools_dir — the coord tool needs the barrier instance.
            # Instead, inject via extra_tools.
            extra_tools=extra_tools,
            log_dir=self._log_dir,
            verifier_enabled=False,
            max_tasks_per_run=50,
            orchestration_timeout=1200,
            router_step_callback=_router_cb,
            context_config=ContextConfig(
                strategy="hybrid",
                recent_messages=12,
                pinned_enabled=True,
                state_mode=self._state_mode,
                memory_read_mode=self._memory_read_mode,
            ),
            token_limit=80000,
            require_explicit_completion=True,
            sandbox_policy=self._sandbox_policy,
            state_provider=state_provider,
            supervision_state_store=supervision_state_store,
            coordinator_secret=self._coordinator_secret,
            ui_dir=str(_sar_ui_dir),
            memory_read_mode=self._memory_read_mode,
            callback_secret=self._coordinator_secret,
            memory_config=memory_config,
            memory_ingestor=memory_ingestor,
        )

        # Attach agent registry (created inside server) to state provider
        self._state_provider._agent_registry = getattr(
            self._server, "_agent_registry", None
        )

        # Phase 3: inject LLM client + token sink into Map Agent (for llm_query)
        _map_agent_llm = ChatOpenAI(
            model=self._model,
            openai_api_key=os.environ.get(self._api_key_env, ""),
            openai_api_base=self._api_base,
            temperature=0.0,
        )
        set_llm_client(_map_agent_llm)

        def _map_agent_token_sink(**kwargs):
            step = (
                getattr(self._barrier, "_step_counter", 0)
                if self._barrier is not None
                else 0
            )
            if self._exp_logger is not None:
                self._exp_logger.log_token_usage(
                    step=step,
                    agent=kwargs.get("agent", "MapAgent"),
                    prompt_tokens=kwargs.get("prompt_tokens", 0),
                    completion_tokens=kwargs.get("completion_tokens", 0),
                    total_tokens=kwargs.get("total_tokens", 0),
                    cache_hit_tokens=kwargs.get("cache_hit_tokens", 0),
                    cache_miss_tokens=kwargs.get("cache_miss_tokens", 0),
                )
                self._exp_logger.flush_summary()

        set_token_sink(_map_agent_token_sink)

        # Phase 4: build peer-mail tools AFTER create_server so real registries exist
        if self._enable_peer_mail and self._coordinator_secret is not None:
            from a2a.coordinator.team_registry import CoordinatorTeamRegistry
            from a2a.coordinator.sender_service import CoordinatorSenderService
            from a2a.builtin_tools.configure_team import (
                ConfigureTeamTool,
                DisbandTeamTool,
                SyncTeamTool,
            )
            from a2a.builtin_tools.send_mail import SendMailTool

            self._team_registry = CoordinatorTeamRegistry()
            self._sender = CoordinatorSenderService(
                coordinator_secret=self._coordinator_secret,
            )

            # Resolver — lazily resolves registries from server when tools execute
            def _agent_reg():
                return getattr(self._server, "_agent_registry", None)

            def _worker_reg():
                return getattr(self._server, "_registry", None)

            extra_tools_post = [
                ConfigureTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                    agent_registry=_agent_reg(),
                    worker_registry=_worker_reg(),
                ),
                DisbandTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                ),
                SyncTeamTool(
                    registry=self._team_registry,
                    sender=self._sender,
                ),
                SendMailTool(
                    sender=self._sender,
                    worker_registry=_worker_reg(),
                    agent_registry=_agent_reg(),
                ),
            ]
            # Extend the router's extra_tools list
            if hasattr(self._server, "_router") and self._server._router is not None:
                self._server._router._extra_tools.extend(extra_tools_post)
            else:
                extra_tools.extend(extra_tools_post)
        # Load mode-specific system prompt if not oracle
        if self._state_mode == "semantic" and self._prompts_dir:
            semantic_prompt_path = Path(self._prompts_dir) / "system.semantic.md"
            if semantic_prompt_path.exists():
                self._server._router._system_prompt = semantic_prompt_path.read_text(
                    encoding="utf-8"
                )

        # Inject barrier for real-time map visualization and semantic map for observation ingestion
        self._server.set_barrier(self._barrier)
        self._server.set_semantic_map(semantic_map)
        # Configure EventStore with coordinator log_dir for NDJSON persistence
        from a2a.coordinator.event_store import event_store

        if self._log_dir:
            event_store.set_log_dir(str(self._log_dir))
        logger.info(
            "SAR Coordinator starting on port %d (A2A port %d)",
            self._port,
            self._a2a_port,
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        await asyncio.sleep(2.0)

    async def submit_task(self, task_description: str) -> str:
        """Submit a task to the coordinator via the standard A2A SDK Client."""
        import httpx
        from a2a.client import create_client, ClientConfig
        from a2a.types.a2a_pb2 import (
            SendMessageRequest,
            Message,
            Part,
            Role,
        )
        from google.protobuf.json_format import MessageToDict

        endpoint = f"http://localhost:{self._a2a_port}/"
        config = ClientConfig(
            streaming=True,
            supported_protocol_bindings=[],
            httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(600.0)),
        )

        message = Message(
            role=Role.ROLE_USER,
            parts=[Part(text=task_description)],
        )
        request = SendMessageRequest(message=message)

        events = []
        client = None
        try:
            client = await create_client(endpoint, config)
            async for stream_response in client.send_message(request):
                events.append(MessageToDict(stream_response))
        except Exception as e:
            logger.error("Task submission failed: %s", e)
            # Preserve the historical direct-call return shape for a failure
            # before an A2A client exists.  run_experiment treats this marker
            # as a framework error; once an A2A task exists, failures remain
            # exceptions/status-failed and never become normal final text.
            if client is None:
                return f"Error: {e}"
            raise RuntimeError(f"A2A task submission failed: {e}") from e
        finally:
            if client is not None:
                await client.close()

        for event in events:
            task = event.get("task", {})
            status_update = event.get("statusUpdate", {})
            status = task.get("status", {}) or status_update.get("status", {})
            state = status.get("state")
            if state in {
                "TASK_STATE_FAILED",
                "TASK_STATE_REJECTED",
                "TASK_STATE_CANCELED",
            }:
                message = (status.get("message", {}) or {}).get("parts", [])
                detail = " ".join(
                    part.get("text", "") for part in message if part.get("text")
                )
                raise RuntimeError(
                    f"Coordinator A2A task failed ({state}): {detail or 'no detail'}"
                )

        return json.dumps(events, indent=2)

    def clear_sessions(self) -> None:
        """Clear the CoordinatorAgentExecutor session store."""
        if self._server is not None and hasattr(self._server, "executor"):
            try:
                self._server.executor.clear_sessions()
            except Exception as e:
                logger.warning("Failed to clear coordinator sessions: %s", e)

    # ── Phase 4: long-term reflection snapshot surface ────────────────────

    @property
    def long_term_store(self):
        """Run-local ``LongTermMemoryStore`` (``None`` when mode == off)."""
        return self._long_term_store

    @property
    def diagnosis_store(self):
        """Run-local ``DiagnosisMemoryStore`` (``None`` when not wired).

        Phase 4 (P4): independent short-lived diagnosis store at
        ``<memory_root>/diagnosis/diagnosis.sqlite3`` (D6).  ``None`` when
        long-term mode is off or the store could not be opened (fail-closed).
        """
        return self._diagnosis_store

    @property
    def diagnosis_config(self):
        """Validated run-local ``DiagnosisConfig`` (``None`` when not wired)."""
        return self._diagnosis_config

    @property
    def memory_store(self):
        """Canonical run-local ``MemoryStore`` (``None`` when not configured).

        Assembled only when ``memory_read_mode`` is ``shadow``/``read_port``;
        the diagnosis loop's four read-only query tools read through it.
        """
        return getattr(self, "_memory_store", None)

    def _resolve_long_term_scope_id(self) -> str | None:
        """Active runtime scope, else the most recently written canonical scope.

        Mirrors ``materialize_compatibility_artifacts`` scope resolution
        (server.py ``_resolve_export_scope_id``); reflection snapshots reuse
        the same committed-scope identity.  Never trusts callback-derived ids.
        """
        server = getattr(self, "_server", None)
        resolve = (
            getattr(server, "_resolve_export_scope_id", None) if server else None
        )
        if resolve is not None:
            try:
                scope_id = resolve()
                if scope_id:
                    return scope_id
            except Exception:
                logger.exception("long-term scope resolution via server failed")
        store = getattr(self, "_memory_store", None)
        if store is None:
            return None
        try:
            scopes = store.list_scopes()
        except Exception:  # noqa: BLE001 - best-effort fallback
            return None
        if not scopes:
            return None

        def _revision(scope_id: str) -> int:
            try:
                return int(store.revision_of(scope_id))
            except Exception:  # noqa: BLE001 - best-effort revision read
                return 0

        return max(
            scopes,
            key=lambda s: (s.get("closed_at") is None, _revision(s["scope_id"])),
        )["scope_id"]

    def long_term_snapshot(self):
        """Committed ``ScopeEventSnapshotV1`` for the reflection source.

        Returns ``None`` when long-term mode is off, no canonical memory
        store is configured, or no scope has committed events yet.  The
        snapshot is read through the canonical ``MemoryStore`` atomic API —
        the reflection collector never rebuilds the window itself.
        """
        if self._long_term_store is None:
            return None
        store = getattr(self, "_memory_store", None)
        if store is None:
            return None
        scope_id = self._resolve_long_term_scope_id()
        if scope_id is None:
            return None
        return store.scope_event_snapshot(scope_id)

    def long_term_supervision_count(self) -> int:
        """Committed ``supervision.*`` canonical event count (rolling signal)."""
        store = getattr(self, "_memory_store", None)
        if store is None:
            return 0
        scope_id = self._resolve_long_term_scope_id()
        if scope_id is None:
            return 0
        try:
            return int(store.supervision_event_count(scope_id))
        except Exception:  # noqa: BLE001 - best-effort signal read
            return 0

    async def stop(self):
        """Stop the coordinator and wait for its Uvicorn thread to exit."""
        # Freeze the outcome CSVs before shutdown: the coordinator's in-flight
        # round may keep executing tool calls while workers are being torn down,
        # and those post-terminal failed rows must never pollute the Phase 5
        # acceptance CSVs.
        if self._exp_logger is not None and hasattr(
            self._exp_logger, "freeze_terminal"
        ):
            self._exp_logger.freeze_terminal()
        if self._server is not None and hasattr(self._server, "shutdown"):
            await self._server.shutdown()
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 11)
            if self._thread.is_alive():
                logger.warning("Coordinator server did not stop within 11 seconds")
        if self._long_term_store is not None:
            try:
                self._long_term_store.close()
            except Exception:
                logger.exception("long-term store close failed during shutdown")

"""通用 Coordinator 骨架（env-contract P4-2 自 ``sar_orch/coordinator.py`` 抽取）。

本模块承载与环境无关的 coordinator 装配与运维逻辑：A2A 服务装配（``create_server``
全量注入面）、router 事件记账（dispatch/token/subtask/decision 日志）、任务提交
（A2A SDK client）、canonical Memory / long-term / diagnosis 存储装配、生命周期
（start/stop/clear_sessions）。

环境特化件（观察源 / 摘要器 / state provider / 工具 / UI / 附属 LLM 接线）一律
经 ``orchestration.env_pack.EnvPack`` 工厂注入，本模块零 ``*_orch`` import
（守卫：tests/test_orchestration_dependency_direction.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from Agent.router_agent.context import ContextConfig

from a2a.coordinator.supervision_state_store import SupervisionStateStore
from orchestration.env_pack import CoordinatorEnv, EnvPack
from orchestration.user_command_queue import UserCommandQueue

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


class OrchestratorCoordinator:
    """通用 Coordinator — 装配 A2A CoordinatorServer 并注入 EnvPack 环境能力。"""

    def __init__(
        self,
        *,
        env_pack: EnvPack,
        host: str = "0.0.0.0",
        port: int = 8080,
        a2a_port: int = 8081,
        barrier=None,  # EnvironmentBarrier instance (EnvPack.build_barrier 产物)
        model: str = "deepseek-v4-flash",
        provider: str = "openai",
        api_base: str = "https://api.deepseek.com",
        api_key_env: str = "OPENAI_API_KEY",
        log_dir: str | None = None,
        supervision_dir: str | None = None,
        orchestration_mode: str = "agentic",
        exp_logger=None,
        sandbox_policy=None,
        state_mode: str = "semantic",
        enable_peer_mail: bool = False,
        coordinator_secret: bytes | None = None,
        max_steps: int = 50,
        # Phase 2: authenticated Temporal shadow write; default read_port
        # since H3 retirement approval (2026-08-10).
        memory_read_mode: str = "read_port",
        # P1 cache optimization: history pruning policy
        # (``count_window`` | ``prefix_stable``) for the coordinator context.
        # The default keeps legacy behavior byte-for-byte unchanged;
        # ``prefix_stable`` opts into the append-only discipline
        # (.agents/context-prefix-stability.md).  The coordinator has no count
        # window — phase 1 is the rewrite phase this switch disables.
        prune_policy: str = "count_window",
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
        # C2b: per-environment watchdog thresholds (``a2a.coordinator.
        # task_watchdog.WatchdogConfig``; ``None`` = kernel defaults — SAR
        # behavior unchanged).  Forwarded to ``create_server`` in ``start()``.
        watchdog_config=None,
    ):
        self._env_pack = env_pack
        self._host = host
        self._port = port
        self._a2a_port = a2a_port
        self._barrier = barrier
        self._model = model
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._orchestration_mode = orchestration_mode
        self._log_dir = log_dir
        self._supervision_dir = supervision_dir
        self._exp_logger = exp_logger
        self._sandbox_policy = sandbox_policy
        self._state_mode = state_mode
        self._max_steps = max_steps
        self._enable_peer_mail = enable_peer_mail
        self._coordinator_secret = coordinator_secret
        self._memory_read_mode = memory_read_mode
        self._prune_policy = prune_policy
        self._run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self._long_term_mode = long_term_mode
        # Phase 4 (P4): ``[diagnosis]`` tunables consumed in start() when the
        # run-local diagnosis store is assembled (None → DiagnosisConfig
        # defaults).
        self._diagnosis_tunables = diagnosis_tunables
        # C2b: env-specific watchdog thresholds (None = kernel defaults).
        self._watchdog_config = watchdog_config
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
        # env-contract P4-2: 环境观测摄取源（EnvPack.build_observation_source 产物；
        # 就绪后经内核 set_semantic_map 接入观测摄取链）。
        self._observation_source = None
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
        """Log the call-time records of a ``send_message`` tool call.

        C4: runs on ``tool_start`` and writes only what is already true at
        call time — the router_interactions.csv call row and the canonical
        ``coordinator_decision.*`` event (the coordinator's decision).  The
        dispatch products (``subtasks.csv`` rows and the ``events.ndjson``
        semantic events) are written on ``tool_result`` by
        :meth:`_log_send_message_result` iff the call was accepted; a rejected
        attempt (e.g. graph-mode ``undeclared_task``) must never leave an
        ``assigned``/``canceled`` row behind — its evidence stays in the call
        row plus the ``{tool}_result`` outcome row carrying ``error_code``.

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
            self._pending_router_tool["send_message"] = {
                "args": dict(args),
                "step": step,
                "correlation_id": correlation_id,
                "worker_task_id": worker_task_id,
            }
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
            self._pending_router_tool["send_message"] = {
                "args": dict(args),
                "step": step,
            }
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
            self._pending_router_tool["send_message"] = {
                "args": dict(args),
                "step": step,
            }
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

    def _log_send_message_result(self, success: bool) -> None:
        """Write the dispatch products of a ``send_message`` call (C4).

        Runs on ``tool_result``: only an accepted call (``success=True``) may
        write dispatch state — the ``subtasks.csv`` rows (assigned/canceled)
        and the ``events.ndjson`` semantic events (assign_task / cancel_task /
        reply_to_help) — so the products always describe what actually
        happened, one-to-one with the accepted-dispatch write point of the
        plan-node path (:meth:`_log_plan_node_dispatches`).  A rejected/failed
        attempt writes no state at all: its evidence is the tool_start call
        row plus the outcome row :meth:`_log_router_outcome` appends with the
        public ``error_code``.  The step is the tool_start stash value, so
        attribution is unchanged.
        """
        if self._exp_logger is None:
            return
        pending = self._pending_router_tool.pop("send_message", None)
        if not success or not isinstance(pending, dict):
            return
        args = pending.get("args") or {}
        step = pending.get("step", 0)
        message_type = args.get("message_type", "unknown")
        content = args.get("content", "")
        who = args.get("who", "")
        related_task_id = args.get("related_task_id", "")
        if message_type == "assign_task":
            self._exp_logger.log_subtask(
                subtask_id=pending.get("worker_task_id", ""),
                status="assigned",
                step=step,
                assigned_to=who,
                subtask=content,
            )
            self._exp_logger.log_event(
                "assign_task",
                step=step,
                agent="Coordinator",
                correlation_id=pending.get("correlation_id", ""),
                payload=args,
            )
        elif message_type == "cancel_task":
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
        elif message_type == "reply_to_help":
            self._exp_logger.log_event(
                "reply_to_help",
                step=step,
                agent="Coordinator",
                payload={
                    "related_task_id": related_task_id,
                    "response_preview": content[:200],
                },
            )

    def _log_plan_node_dispatches(self, step: int, data: dict) -> None:
        """Log the fan-out of a successful ``activate_plan_node`` activation.

        Whenever a MissionGraph is declared (graph mode), direct ``assign_task``
        is rejected and every worker assignment goes through
        ``send_message(message_type='activate_plan_node')`` → ``MissionRuntime``
        fan-out instead.  The MissionRuntime dispatch path never runs the
        ``assign_task`` branch of :meth:`_log_send_message`, so without this
        write point ``subtasks.csv`` and the top-level ``events.ndjson``
        ``assign_task`` entries would only exist on the direct-dispatch path —
        the same run would produce different artifacts depending on which
        coordination path the coordinator chose (path-dependent products /
        false negatives in product-based acceptance).

        Each accepted dispatch mirrors the direct-path row/event shape:
        ``SubtaskID``/``related_task_id`` = the physical dispatch id, content =
        the exact per-worker prompt, plus the MissionGraph ``node_id``.  Failed
        or rolled-back activations carry no ``dispatches`` list and are skipped.
        """
        if self._exp_logger is None:
            return
        dispatches = data.get("dispatches")
        if not isinstance(dispatches, list):
            return
        node_id = str(data.get("node_id", "") or "")
        for entry in dispatches:
            if not isinstance(entry, dict):
                continue
            dispatch_id = str(entry.get("dispatch_id", "") or "")
            worker_id = str(entry.get("worker_id", "") or "")
            if not dispatch_id:
                continue
            content = str(entry.get("content", "") or "")
            self._dispatch_seq += 1
            correlation_id = f"coordinator-dispatch-{self._dispatch_seq}"
            self._exp_logger.log_subtask(
                subtask_id=dispatch_id,
                status="assigned",
                step=step,
                assigned_to=worker_id,
                subtask=content,
            )
            self._exp_logger.log_event(
                "assign_task",
                step=step,
                agent="Coordinator",
                correlation_id=correlation_id,
                payload={
                    "message_type": "assign_task",
                    "who": worker_id,
                    "content": content,
                    "related_task_id": dispatch_id,
                    "node_id": node_id,
                },
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
            status = kw.get("status", "ok")
            if usage is not None:
                self._exp_logger.log_token_usage(
                    step=step,
                    agent="Coordinator",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    status=status,
                )
            else:
                # No usage reported (failed / exception paths emit a
                # zero-usage marker row so every llm_request has a
                # matching token_usage row).
                self._exp_logger.log_token_usage(
                    step=step,
                    agent="Coordinator",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    cache_hit_tokens=0,
                    cache_miss_tokens=0,
                    status=status,
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
            if tool_name == "send_message":
                # C4: dispatch products (subtasks rows + semantic events) are
                # written only for an accepted call; the tool_start stash
                # carries the call's step and the dispatch ids.
                self._log_send_message_result(success)
                # Plan-node activation dispatches workers through the
                # MissionRuntime without passing the assign_task logging
                # branch; mirror the dispatch artifacts here so both
                # coordination paths emit the same products.
                self._log_plan_node_dispatches(step, kw.get("data") or {})
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

        from a2a.coordinator.event_store import event_store

        pack = self._env_pack

        supervision_state_store = SupervisionStateStore(
            log_dir=str(Path(self._supervision_dir))
            if self._supervision_dir
            else (str(Path(self._log_dir)) if self._log_dir else None)
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

        self._user_command_queue = UserCommandQueue()

        # env-contract P4-2: 环境包装配窗口 —— 按序调用 EnvPack 工厂构建环境侧
        # 部件（观察源 → 摘要器 → state provider → 工具）；前序产物回填 ctx 供
        # 后序工厂读取。本骨架零环境实现。
        env_ctx = CoordinatorEnv(
            barrier=self._barrier,
            log_dir=str(Path(self._log_dir)) if self._log_dir else None,
            run_id=self._run_id,
            state_mode=self._state_mode,
            max_steps=self._max_steps,
            model=self._model,
            api_base=self._api_base,
            api_key_env=self._api_key_env,
            exp_logger=self._exp_logger,
            event_store=event_store,
            supervision_state_store=supervision_state_store,
            user_command_queue=self._user_command_queue,
            memory_read_mode=self._memory_read_mode,
            long_term_mode=self._long_term_mode,
            long_term_store=self._long_term_store,
            diagnosis_store=self._diagnosis_store,
            diagnosis_config=self._diagnosis_config,
            prune_policy=self._prune_policy,
        )
        self._observation_source = pack.build_observation_source(env_ctx)
        env_ctx.observation_source = self._observation_source
        env_ctx.domain_summarizer = pack.build_domain_summarizer(env_ctx)

        state_provider = pack.build_coordinator_state_provider(env_ctx)
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

        extra_tools: list = list(pack.build_coordinator_tools(env_ctx))

        # Router step_callback for logging subtask dispatches + coordinator token usage
        def _router_cb(event_type: str, **kw):
            self._on_router_event(event_type, **kw)

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
            prompts_dir=pack.coordinator_prompts_dir,
            skills_dir=pack.coordinator_skills_dir,
            # Do NOT pass tools_dir — coordinator tools that need runtime
            # instances (e.g. barrier) are injected via extra_tools.
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
                prune_policy=self._prune_policy,
            ),
            token_limit=80000,
            require_explicit_completion=True,
            sandbox_policy=self._sandbox_policy,
            state_provider=state_provider,
            supervision_state_store=supervision_state_store,
            # C2b: env-specific watchdog thresholds (None = kernel defaults).
            watchdog_config=self._watchdog_config,
            coordinator_secret=self._coordinator_secret,
            ui_dir=pack.ui_dir,
            memory_read_mode=self._memory_read_mode,
            callback_secret=self._coordinator_secret,
            memory_config=memory_config,
            memory_ingestor=memory_ingestor,
            finish_task_tool_factory=pack.finish_task_tool_factory,
            environment_state_provider_factory=pack.environment_state_provider_factory,
            map_mcp_mount_hook=pack.map_mcp_mount_hook,
            mcp_session_lifecycle_provider=pack.mcp_session_lifecycle_provider,
            # env-contract G7: 环境 Context·session 工厂（None = 内核缺省，
            # 逐字等价）经内核透传 CoordinatorAgentExecutor。
            session_factory=pack.build_session_factory(role="coordinator"),
        )

        # Attach agent registry (created inside server) to state provider
        self._state_provider._agent_registry = getattr(
            self._server, "_agent_registry", None
        )

        # env-contract P4-2: 附属 LLM 接线（SAR = Map Agent llm_query 的
        # ChatOpenAI + token sink）；server 就绪后调用。
        pack.attach_auxiliary_llm(env_ctx)

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
        if self._state_mode == "semantic" and pack.coordinator_prompts_dir:
            semantic_prompt_path = (
                Path(pack.coordinator_prompts_dir) / "system.semantic.md"
            )
            if semantic_prompt_path.exists():
                self._server._router._system_prompt = semantic_prompt_path.read_text(
                    encoding="utf-8"
                )

        # Inject barrier for real-time map visualization and observation source
        # for observation ingestion.
        self._server.set_barrier(self._barrier)
        # env-contract G8: run-control seam — the barrier doubles as the
        # EnvironmentRunControl implementation (request_stop/get_run_status),
        # so cancel/shutdown lifecycle paths stop the environment through the
        # canonical interface instead of the barrier fallback.
        self._server.set_run_control(self._barrier)
        if self._observation_source is not None:
            self._server.set_semantic_map(self._observation_source)
        # Inject user-command queue for console UI mid-run injection
        self._server.set_user_command_queue(self._user_command_queue)
        # Configure EventStore with coordinator log_dir for NDJSON persistence
        if self._log_dir:
            event_store.set_log_dir(str(self._log_dir))
        logger.info(
            "%s Coordinator starting on port %d (A2A port %d)",
            (pack.name or "env").upper(),
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
    def observation_source(self):
        """环境观测摄取源（``EnvPack.build_observation_source`` 产物）。

        装配层/环境侧公开读面（P4-4）：就绪后经内核 ``set_semantic_map`` 接入
        观测摄取链；``start()`` 前为 ``None``。SAR 用它重定向 semantic_map
        jsonl 与逐回合刷新 step budget（此前经 ``SARCoordinator._semantic_map``
        兼容别名读取）。
        """
        return self._observation_source

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

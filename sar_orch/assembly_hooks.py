"""SAR 装配钩子 —— ``orchestration.assembly.AssemblyHooks`` 的 SAR 实现（P4-4）。

本模块承载从 ``sar_orch/experiment.py`` 迁出的 SAR 装配期特化件：

- ``SARAssemblyHooks``：装配生命周期钩子实现——环境产物快照
  （``scene_config.json``）、evaluator-private truth recorder 接线、语义地图
  jsonl 重定向、长期记忆滚动反思 + 诊断通道运行时接线、逐回合域状态推进、
  终态评测（memory acceptance / projection quality / 长期记忆终态反思）。
- 迁入的终端函数（保留在 ``sar_orch.experiment`` 的 re-export 面以兼容既有
  测试与调用方）：``_dump_scene_config`` / ``_finalize_truth_recorder`` /
  ``_invoke_run_terminal_memory_eval`` / ``_invoke_run_terminal_long_term_reflection``。

通用装配流程在 ``orchestration.assembly``；本模块只做环境/特性侧动作，
不重复装配骨架逻辑。
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from orchestration.assembly import AssemblyHooks, AssemblyState

logger = logging.getLogger("sar_orch.assembly_hooks")


def _dump_scene_config(exp_dir: Path, barrier, scene: int, seed: int) -> Path | None:
    """Write the initial grid/object layout snapshot to ``<run>/scene_config.json``.

    Called once right after env initialization and before any step executes,
    providing the stable cross-run scene-configuration baseline required by
    trajectory-audit M2.  The schema is flat and stable:

    - top level: ``schema_version``, ``scene``, ``seed``, ``num_agents``,
      ``grid`` {width, height, altitude}, ``objects`` {agents, fires,
      flammables, persons, reservoirs, deposits}.
    - every object entry carries ``id`` + ``name`` + ``type``; concrete
      objects (AbsAgent/Flammable/Person/Reservoir/Deposit) also carry their
      initial ``position`` {x, y, z}; abstract ``Fire`` aggregates have no
      grid position — they list their member ``flammable_ids`` instead.
    - type-specific initial attributes: Fire average_intensity/fire_type;
      Flammable intensity/fire_type/parent_fire; Person load/status/
      spotted/deposited; Reservoir resource_type/available; Deposit and
      AbsAgent inventory.  Enums are stringified via ``read_enum`` and
      infinities are normalized to the ``"infinite"`` marker (JSON-safe).
    """
    env = getattr(barrier, "env", None)
    field = getattr(getattr(env, "controller", None), "field", None)
    if field is None:
        # Test doubles may substitute a barrier without an initialized env;
        # the snapshot is an optional run artifact and must not break them
        # (also: the SAR flat-import path is only guaranteed once a real
        # barrier exists — the ``core``/``misc`` imports move below this
        # guard so fake-barrier assemblies skip cleanly).
        logger.warning(
            "scene_config: barrier has no initialized env; skipping snapshot"
        )
        return None

    # SAR/ is placed on sys.path by sar_orch.barrier (flat-import layout).
    from core import Coordinate
    from misc import read_enum

    def _position(obj) -> dict | None:
        """Serialize an object's position to a JSON-safe {x, y, z} dict."""
        try:
            ptpl = obj.get_position()
        except Exception:  # noqa: BLE001 -- engine objects vary in shape
            return None
        if not ptpl:
            return None
        axes = ("x", "y", "z")[: len(ptpl)]
        return {axn: int(axv) for axn, axv in zip(axes, ptpl)}

    def _enum(value) -> str | None:
        """Stringify an engine enum (or pass scalars through) capitalized."""
        if value is None:
            return None
        try:
            return str(read_enum(value)).capitalize()
        except Exception:  # noqa: BLE001 -- enum shapes are engine-internal
            return str(value).capitalize()

    def _fire_type(obj) -> str | None:
        """Map the internal fire-type code ('A'/'B') to a readable label."""
        ft = getattr(obj, "fire_type", None)
        if ft is None:
            return None
        mapper = getattr(field, "READABLE_TYPE_MAPPER_FIRE", {})
        return str(mapper.get(str(ft).upper(), str(ft))).capitalize()

    def _resource_type(obj) -> str | None:
        """Map the internal reservoir/deposit resource code to a readable label."""
        rt = getattr(obj, "type", None)
        if rt is None:
            return None
        mapper = getattr(field, "READABLE_TYPE_MAPPER_RESOURCE", {})
        return str(mapper.get(str(rt).upper(), str(rt))).capitalize()

    def _available(obj) -> int | str | None:
        """JSON-safe reservoir remaining supply (``math.inf`` → ``"infinite"``)."""
        remaining = getattr(obj, "available", None)
        if remaining is None:
            return None
        try:
            return (
                "infinite"
                if not math.isfinite(float(remaining))
                else int(remaining)
            )
        except (TypeError, ValueError):
            return None

    objects: dict[str, list[dict]] = {
        "agents": [],
        "fires": [],
        "flammables": [],
        "persons": [],
        "reservoirs": [],
        "deposits": [],
    }
    for obj in field.all_objects(expand=True, with_memory=True):
        tp = obj.class_name() if hasattr(obj, "class_name") else type(obj).__name__
        entry: dict = {
            "id": getattr(obj, "id", None),
            "name": getattr(obj, "name", None),
            "type": tp,
        }
        pos = _position(obj)
        if pos is not None:
            entry["position"] = pos

        if tp == "AbsAgent":
            entry["inventory"] = dict(getattr(obj, "inventory", {}) or {})
            objects["agents"].append(entry)
        elif tp == "Fire":
            entry["average_intensity"] = _enum(getattr(obj, "average_intensity", None))
            entry["fire_type"] = _fire_type(obj)
            entry["flammable_ids"] = [
                fl.id for fl in getattr(obj, "flammables", []) if fl.id is not None
            ]
            objects["fires"].append(entry)
        elif tp == "Flammable":
            entry["intensity"] = _enum(getattr(obj, "intensity", None))
            entry["fire_type"] = _fire_type(obj)
            entry["parent_fire"] = getattr(obj, "parent_name", None)
            objects["flammables"].append(entry)
        elif tp == "Person":
            entry["load"] = getattr(obj, "load", None)
            entry["status"] = _enum(getattr(obj, "status", None))
            entry["spotted"] = bool(getattr(obj, "spotted", False))
            entry["deposited"] = bool(getattr(obj, "deposited", False))
            objects["persons"].append(entry)
        elif tp == "Reservoir":
            entry["resource_type"] = _resource_type(obj)
            entry["available"] = _available(obj)
            objects["reservoirs"].append(entry)
        elif tp == "Deposit":
            entry["inventory"] = dict(getattr(obj, "inventory", {}) or {})
            objects["deposits"].append(entry)
        else:
            logger.warning("scene_config: skipping unknown object type %r", tp)

    config = {
        "schema_version": 1,
        "scene": scene,
        "seed": seed,
        "num_agents": getattr(env, "num_agents", 0),
        "grid": {
            "width": int(Coordinate.WIDTH),
            "height": int(Coordinate.HEIGHT),
            "altitude": int(Coordinate.ALTITUDE),
        },
        "objects": objects,
    }
    out_path = exp_dir / "scene_config.json"
    out_path.write_text(
        json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Scene config snapshot written: %s", out_path)
    return out_path


def _finalize_truth_recorder(
    truth_recorder, coordinator, terminal_status: str
) -> dict | None:
    """Resolve the canonical scope (read-only), freeze the evaluator-private
    manifest, and report whether a manifest was produced.

    In legacy memory mode (or when no canonical scope can be resolved) the
    recorder skips cleanly and ``None`` is returned so the caller neither wires
    a truth manifest into the terminal evaluator nor fails the run.
    """
    scope_id = None
    server = getattr(coordinator, "_server", None)
    resolver = (
        getattr(server, "_resolve_export_scope_id", None)
        if server is not None
        else None
    )
    if callable(resolver):
        try:
            scope_id = resolver()
        except Exception:
            logger.exception("truth recorder scope resolution failed")
    truth_recorder.set_scope_id(scope_id)
    manifest = truth_recorder.finalize(terminal_status)
    if manifest is None:
        logger.warning(
            "truth recorder: no canonical scope resolved; manifest skipped "
            "(legacy memory mode)"
        )
        return None
    logger.info(
        "truth manifest frozen: %s (scope %s...)",
        truth_recorder.manifest_path,
        str(scope_id or "")[:12],
    )
    return {
        "manifest": str(truth_recorder.manifest_path),
        "trace": str(truth_recorder.trace_path),
        "scope_id": scope_id,
        "terminal_status": terminal_status,
    }


def _invoke_run_terminal_memory_eval(
    *,
    coordinator,
    exp_dir: Path,
    memory_read_mode: str,
    truth_manifest: str | None,
    truth_trace: str | None,
) -> dict:
    """Phase 5 run-terminal wiring: materialize + acceptance/projection eval.

    Runs only when canonical Memory is configured (``shadow``/``read_port``);
    ``legacy`` runs keep their existing artifacts untouched (legacy retirement
    is NOT enabled here).  The exporter deterministically rebuilds the
    compatibility artifacts (``semantic_map.jsonl`` + export manifest) from the
    committed canonical set; then the acceptance evaluator writes
    ``memory_acceptance.json`` and, when a truth manifest is provided, the
    projection-quality evaluator writes ``memory_projection_quality.json``.

    The acceptance gate is logged but never crashes the experiment: a real run
    may legitimately contain framework errors (worker_busy / task routing), and
    H3 retirement has not been authorised.  Returns a small summary dict.
    """
    result: dict = {"materialized": False}
    if memory_read_mode not in ("shadow", "read_port"):
        return result
    server = getattr(coordinator, "_server", None)
    if server is None:
        logger.info("run-terminal memory eval skipped: coordinator server unavailable")
        return result
    try:
        materialized = server.materialize_compatibility_artifacts(str(exp_dir))
        result["materialized"] = materialized is not None
        if materialized is not None:
            result["scope_id"] = materialized.get("scope_id")
    except Exception:
        logger.exception("run-terminal compatibility materialization failed")

    try:
        from sar_orch.eval.memory_acceptance import (
            evaluate as acceptance_evaluate,
        )
        from sar_orch.eval.memory_acceptance import (
            gate as acceptance_gate,
        )
        from sar_orch.eval.memory_acceptance import (
            write_artifact as acceptance_write_artifact,
        )

        acceptance_result, unknown = acceptance_evaluate(str(exp_dir))
        acceptance_write_artifact(exp_dir, acceptance_result)
        result["acceptance"] = {
            "failed_tool_rows": int(acceptance_result["failed_tool_rows"]),
            "missing_error_code_rows": int(
                acceptance_result["missing_error_code_rows"]
            ),
            "framework_error_counts": acceptance_result["framework_error_counts"],
        }
        try:
            acceptance_gate(acceptance_result, unknown)
            result["acceptance_gate"] = "pass"
        except Exception as exc:  # noqa: BLE001 - gate is logged, not fatal
            logger.warning(
                "memory_acceptance gate not satisfied (logged, run continues): %s", exc
            )
            result["acceptance_gate"] = "fail"
    except Exception:
        logger.exception("memory_acceptance evaluation failed")

    if truth_manifest:
        try:
            from sar_orch.eval.memory_projection_quality import (
                evaluate as pq_evaluate,
            )
            from sar_orch.eval.memory_projection_quality import (
                write_artifact as pq_write_artifact,
            )

            pq_artifact = pq_evaluate(str(exp_dir), truth_manifest, truth_trace)
            pq_write_artifact(exp_dir, pq_artifact)
            result["projection_quality"] = pq_artifact.get("metric_status")
        except Exception:
            logger.exception("memory_projection_quality evaluation failed")
    return result


def _invoke_run_terminal_long_term_reflection(
    *,
    coordinator,
    exp_dir: Path,
    long_term_mode: str,
    lt_config,
    truth_manifest: str | None,
    env: dict,
) -> dict:
    """Phase 4 run-terminal long-term wiring: drain → snapshot → reflection.

    Order (main plan §3.4.1): first drain the in-flight rolling reflection
    (join with ``[timeout] reflection_sec``, default 60s), then take the
    terminal committed snapshot and run the terminal reflection.  A join
    timeout records a typed timeout status and never blocks run exit.
    Phase 4 never invokes a real model: an unconfigured model port skips the
    reflection (``skipped_model_unconfigured``) instead of failing the run —
    except in ``read`` mode, where a missing ``reflection_api_key`` raises a
    typed D8 error (explicit rejection, never a silent downgrade to
    shadow/off).
    With ``quality_enabled`` the read-only ``long_term_memory_quality``
    evaluator writes ``long_term_memory_quality.json`` into the results dir
    (mode=ro; source/run/project DBs untouched).
    """
    from a2a.coordinator.memory.contracts import MemoryContractError

    result: dict = {"status": "off"}
    if long_term_mode == "off" or coordinator is None:
        return result
    store = getattr(coordinator, "long_term_store", None)
    if store is None:
        result["status"] = "no_store"
        return result
    try:
        from sar_orch.long_term_reflection import (
            build_reflection_model_port,
            drain_inflight_reflection,
            reflection_run,
        )

        # 1. drain the in-flight rolling reflection (join with timeout)
        drain = drain_inflight_reflection(
            timeout_sec=getattr(lt_config, "reflection_sec", 60)
        )
        result["drain"] = drain.status
        # Phase 4 (P4): surface the second-channel diagnosis outcome the
        # in-flight worker recorded (typed ok/rejected/timeout/skip — D8).
        # There is deliberately NO terminal diagnosis run: the rolling
        # trigger is the only diagnosis channel and terminal = drop
        # (diagnosis is non-essential and must never block run exit).
        result["diagnosis"] = (drain.result or {}).get("diagnosis")
        if drain.status == "timeout":
            try:
                store.record_audit(
                    "reflection_timeout",
                    "terminal drain timed out; in-flight rolling reflection "
                    "left running (typed timeout, run not blocked)",
                )
            except Exception:
                logger.exception("failed to record reflection timeout audit")

        # 2. terminal committed snapshot (active / last scope)
        snapshot = coordinator.long_term_snapshot()
        if snapshot is None:
            result["status"] = "no_snapshot"
            return result
        if getattr(snapshot, "status", "ok") != "ok":
            result["status"] = f"snapshot_{snapshot.status}"
            return result

        # 3. terminal reflection (offline in P4: no real model call)
        model_port = build_reflection_model_port(env)
        if model_port is None:
            if long_term_mode == "read":
                # D8（M2）：read 模式缺 reflection_api_key → 显式 typed 拒绝，
                # 不静默降级；shadow/off 保持跳过语义。
                raise MemoryContractError(
                    "read_mode_requires_api_key",
                    "read mode requires reflection_api_key (D8); refusing to "
                    "silently downgrade to shadow/off",
                )
            result["status"] = "skipped_model_unconfigured"
            return result
        outcome = reflection_run(
            store=store,
            snapshot=snapshot,
            project_id="llamar",
            model_port=model_port,
        )
        result["status"] = outcome.status
        result["run_id"] = outcome.run_id
        result["long_term_memory_written"] = outcome.long_term_memory_written
        result["reason"] = outcome.reason

        # 4. read-only quality evaluator (terminal-only)
        if getattr(lt_config, "quality_enabled", True):
            try:
                from sar_orch.eval.long_term_memory_quality import (
                    evaluate_long_term_memory_quality,
                )

                artifact = evaluate_long_term_memory_quality(
                    exp_dir, truth_manifest=truth_manifest
                )
                result["quality"] = artifact.get("metrics")
            except Exception:
                logger.exception("long_term_memory_quality evaluation failed")
                result["quality"] = "error"
    except MemoryContractError as exc:
        if exc.code == "read_mode_requires_api_key":
            # D8（M2）：read 模式缺 key 的 typed 拒绝必须向上传播（显式拒绝，
            # 不静默降级）；其余 typed 存储错误仍按“不阻塞 run 退出”转换。
            raise
        logger.exception("run-terminal long-term reflection failed")
        result["status"] = "failed"
    except Exception:
        logger.exception("run-terminal long-term reflection failed")
        result["status"] = "failed"
    return result


class SARAssemblyHooks(AssemblyHooks):
    """SAR 装配生命周期钩子。

    构造参数由 SAR 装配薄壳（``sar_orch/experiment.py``）提供：

    - ``scene`` / ``seed`` / ``num_agents``：环境快照与 truth recorder 元数据；
    - ``truth_out``：evaluator-private truth 输出目录（边界校验已在薄壳完成）；
    - ``memory_read_mode``：``shadow``/``read_port`` 时才跑终态记忆评测；
    - ``long_term_mode`` / ``lt_config`` / ``diag_runtime``：长期记忆与诊断
      通道的运行时接线参数（``off`` 时零长期记忆 I/O）。
    """

    def __init__(
        self,
        *,
        scene: int,
        seed: int,
        num_agents: int,
        truth_out: Path,
        memory_read_mode: str,
        long_term_mode: str = "off",
        lt_config: Any = None,
        diag_runtime: Any = None,
        truth_manifest: str | None = None,
        truth_trace: str | None = None,
    ) -> None:
        self._scene = scene
        self._seed = seed
        self._num_agents = num_agents
        self._truth_out = Path(truth_out)
        self._memory_read_mode = memory_read_mode
        self._long_term_mode = long_term_mode
        self._lt_config = lt_config
        self._diag_runtime = diag_runtime
        self._truth_manifest = truth_manifest
        self._truth_trace = truth_trace
        self._truth_recorder = None
        self._last_supervision_count = 0

    # ── 装配窗口 ─────────────────────────────────────────────────────────

    def coordinator_kwargs(self, state: AssemblyState) -> dict[str, Any]:
        """诊断通道 tunables 透传（``[diagnosis]`` 段；None = 冻结缺省）。"""
        return {"diagnosis_tunables": self._diag_runtime}

    def on_environment_ready(self, state: AssemblyState) -> None:
        """barrier 就绪、任何回合之前：scene_config 快照 + truth recorder。"""
        _dump_scene_config(state.exp_dir, state.barrier, self._scene, self._seed)

        # Phase 5: evaluator-private truth recorder（默认开启；输出目录在
        # run results 之外——H1 边界，薄壳已完成越界校验）。
        from sar_orch.eval.truth_recorder import TruthRecorder

        self._truth_recorder = TruthRecorder(
            state.barrier,
            self._truth_out,
            run_id=state.run_id,
            scene=self._scene,
            num_agents=self._num_agents,
            seed=self._seed,
        )
        logger.info("Truth recorder enabled; output dir: %s", self._truth_out)

    def on_coordinator_started(self, state: AssemblyState) -> None:
        """coordinator 注册等待完成后：长期记忆/诊断接线 + 语义图 jsonl 重定向。"""
        coordinator = state.coordinator

        # Phase 4: rolling long-term reflection runtime（shadow/read；off 零 I/O）
        if (
            self._long_term_mode != "off"
            and getattr(coordinator, "long_term_store", None) is not None
        ):
            from sar_orch.long_term_reflection import (
                build_reflection_model_port,
                configure_long_term_runtime,
            )

            model_port = build_reflection_model_port(state.env)
            configure_long_term_runtime(
                store=coordinator.long_term_store,
                snapshot_provider=coordinator.long_term_snapshot,
                project_id="llamar",
                model_port=model_port,
                config=self._lt_config,
            )
            logger.info(
                "long-term rolling reflection wired (mode=%s, model_port=%s)",
                self._long_term_mode,
                "configured" if model_port is not None else "unconfigured(skip)",
            )
            # Phase 4 (P4): second channel — agentic diagnosis loop on the same
            # rolling trigger（并存不替代, main plan §3.2）。Fail-closed：缺
            # diagnosis store 只是跳过该通道，绝不影响长期记忆或 run。
            diag_config = coordinator.diagnosis_config
            if coordinator.diagnosis_store is not None and diag_config is not None:
                from sar_orch.long_term_reflection import (
                    configure_diagnosis_runtime,
                )

                diagnosis_model_port = build_reflection_model_port(
                    state.env, timeout_sec=diag_config.diagnosis_sec
                )
                configure_diagnosis_runtime(
                    canonical_store=coordinator.memory_store,
                    diagnosis_store=coordinator.diagnosis_store,
                    diagnosis_config=diag_config,
                    model_port=diagnosis_model_port,
                )
                logger.info(
                    "diagnosis channel wired (inject_enabled=%s, min_confidence=%s, "
                    "model_port=%s, timeout_sec=%s)",
                    diag_config.inject_enabled,
                    diag_config.min_confidence,
                    "configured"
                    if diagnosis_model_port is not None
                    else "unconfigured(skip)",
                    diag_config.diagnosis_sec,
                )
            else:
                logger.warning(
                    "diagnosis channel skipped — no diagnosis store "
                    "(long-term mode %s)",
                    self._long_term_mode,
                )

        # 语义地图 jsonl 重定向到 run 根目录（P4-3 遗留：改用骨架公开访问器，
        # 不再依赖 ``_semantic_map`` 私有别名）。
        observation_source = getattr(coordinator, "observation_source", None)
        if observation_source is not None:
            observation_source.set_jsonl_path(
                str(state.exp_dir / "semantic_map.jsonl")
            )
            logger.info(
                "semantic_map.jsonl path set to: %s",
                state.exp_dir / "semantic_map.jsonl",
            )

    # ── 回合窗口 ─────────────────────────────────────────────────────────

    def step_metrics(
        self, state: AssemblyState, *, step_num: int, step_log: dict
    ) -> dict[str, Any]:
        """每回合语义地图质量指标（recall / freshness）。"""
        coordinator = state.coordinator
        observation_source = (
            getattr(coordinator, "observation_source", None)
            if coordinator is not None
            else None
        )
        if observation_source is None:
            return {}
        return {
            "map_recall": observation_source.map_recall(),
            "freshness": observation_source.freshness(),
        }

    def on_step(
        self,
        state: AssemblyState,
        *,
        step_num: int,
        step_log: dict,
        drained_logs: list,
    ) -> None:
        """逐回合推进：语义地图步数预算 + truth recorder 记录。"""
        coordinator = state.coordinator
        observation_source = (
            getattr(coordinator, "observation_source", None)
            if coordinator is not None
            else None
        )
        if observation_source is not None:
            observation_source.update_step_budget(
                current_step=step_num,
                max_steps=state.max_steps,
            )
        if self._truth_recorder is not None:
            try:
                self._truth_recorder.record_step(step_num)
            except Exception:
                logger.exception(
                    "truth recorder record_step failed (step %d)", step_num
                )

    def on_poll(
        self, state: AssemblyState, *, metrics: dict, drained_logs: list
    ) -> None:
        """每轮 poll：长期记忆滚动反思触发点（任务完成 / 每 N 步 / 监督事件）。"""
        coordinator = state.coordinator
        if self._long_term_mode == "off" or coordinator is None:
            return
        if getattr(coordinator, "long_term_store", None) is None:
            return

        from sar_orch.long_term_reflection import maybe_trigger_rolling_reflection

        lt_config = self._lt_config
        for step_log in drained_logs:
            step_num = step_log.get("step", metrics["steps"])
            if lt_config.task_complete and step_log.get("finished"):
                maybe_trigger_rolling_reflection()
            if (
                lt_config.every_env_step
                and step_num > 0
                and step_num % lt_config.every_env_step == 0
            ):
                maybe_trigger_rolling_reflection()
        if lt_config.supervision_event:
            current_supervision = coordinator.long_term_supervision_count()
            if current_supervision != self._last_supervision_count:
                self._last_supervision_count = current_supervision
                maybe_trigger_rolling_reflection()

    # ── 终态窗口 ─────────────────────────────────────────────────────────

    def finalize_artifacts(
        self, state: AssemblyState, *, final_metrics: dict, end_reason: str
    ) -> dict[str, Any]:
        """truth recorder 终态收口：补录边界回合 + 冻结 manifest。"""
        if self._truth_recorder is None:
            return {}
        try:
            pending_logs = (
                state.barrier.drain_step_logs()
                if hasattr(state.barrier, "drain_step_logs")
                else []
            )
            for step_log in pending_logs:
                self._truth_recorder.record_step(int(step_log.get("step", 0)))
            if final_metrics["steps"] >= 1:
                self._truth_recorder.record_step(final_metrics["steps"])
            truth_recorder_result = _finalize_truth_recorder(
                self._truth_recorder, state.coordinator, end_reason
            )
        except Exception:
            logger.exception("truth recorder terminal finalize failed")
            return {}
        if truth_recorder_result is not None:
            state.scratch["truth_recorder"] = truth_recorder_result
            # 生成的 manifest 自动接进终态 projection-quality 评测；显式
            # --truth-manifest 优先，不被生成值覆盖（与迁移前语义一致）。
            if self._truth_manifest is None:
                state.scratch["truth_manifest"] = truth_recorder_result["manifest"]
        return {}

    def run_terminal_evaluations(
        self, state: AssemblyState, *, final_metrics: dict
    ) -> dict[str, Any]:
        """终态评测：memory acceptance / projection quality + 长期记忆收口。"""
        generated_manifest = state.scratch.get("truth_manifest")
        if generated_manifest is not None:
            truth_manifest, truth_trace = generated_manifest, None
        else:
            truth_manifest, truth_trace = self._truth_manifest, self._truth_trace

        terminal_memory = _invoke_run_terminal_memory_eval(
            coordinator=state.coordinator,
            exp_dir=state.exp_dir,
            memory_read_mode=self._memory_read_mode,
            truth_manifest=truth_manifest,
            truth_trace=truth_trace,
        )
        truth_result = state.scratch.get("truth_recorder")
        if truth_result is not None:
            terminal_memory["truth_recorder"] = truth_result

        terminal_long_term = _invoke_run_terminal_long_term_reflection(
            coordinator=state.coordinator,
            exp_dir=state.exp_dir,
            long_term_mode=self._long_term_mode,
            lt_config=self._lt_config,
            truth_manifest=truth_manifest,
            env=state.env,
        )
        return {
            "memory_terminal": terminal_memory,
            "long_term_reflection": terminal_long_term,
        }

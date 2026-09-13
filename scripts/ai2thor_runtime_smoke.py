#!/usr/bin/env python3
"""
AI2Thor Unity Runtime Smoke Probe — G1/G5 迁移门禁脚本（P5-4 起覆盖 unity 接线面）。

用途
----
探测 AI2Thor Unity runtime 能否正常启动、能否按编排层的约定工作：

  fake 模式  确定性假数据，不依赖 ai2thor 包，用于本地验证报告 schema 与退出码；
  unity 模式 真实 ``ai2thor.controller.Controller``（经
             ``ai2thor_orch.executor.unity_controller.UnityController`` +
             ``ControllerExecutor``，即实验将走的同一条接线），在远程 A100 等
             GPU 主机上运行。

unity 模式的 gating 断言（任一失败 → status=error，退出码 1）：

  1. ``import_ok``            ai2thor 包可导入；
  2. ``controller_started``   Controller 启动 + ``UnityController`` 构造成功；
  3. ``multi_agent_events``   ``agentCount=N`` 生效（事件里 N 份 agent 事件）；
  4. ``per_agent_metadata``   每份 agent 事件带 ``agentId`` 与 ``agent.position``；
  5. ``executor_round_ok``    ``ControllerExecutor.execute_step`` 一轮 N 动作全部成功
                              （含 ``NoOp`` → ai2thor ``Pass`` 空动作映射）；
  6. ``adapter_surface_ok``   归一化 metadata 带 ``agents``（N 条）/ ``objects``，
                              即 barrier/verifier 的消费面成立；
  7. ``stop_clean``           ``ControllerExecutor.stop()`` 干净回收（含 Controller.stop）。

非 gating 记录（进 report 的 ``info``，不判定成败）：``MoveAhead`` 是否真的位移、
原始 ``Done`` 动作是否被 build 接受（编排层已把 ``Done`` 映射为空动作，
不依赖该结果）、``GetReachablePositions`` 数量。

退出码
------
  0  — 探针成功，status=ok
  1  — 运行时异常或断言失败（非 ImportError / 非超时）
  2  — ai2thor 包未安装（ImportError）
  3  — 探针整体超时（--timeout 秒内未完成）

环境变量
--------
  LLAMAR_AI2THOR_MODE       默认运行模式（--mode 未显式传递时读取；缺省 "fake"）
  LLAMAR_AI2THOR_HEADLESS   缺省 1（headless 启动；0 = 开窗渲染）
  LLAMAR_AI2THOR_PLATFORM   cloud → CloudRendering（无显示 GPU 渲染）/ linux
  LLAMAR_AI2THOR_X_DISPLAY / LLAMAR_AI2THOR_GPU_DEVICE / WIDTH / HEIGHT / ...

  完整清单与三级运行流程见 docs/system_docs/ai2thor_a100_runbook.md。

用法示例
--------
  # 本地 fake 模式（无 GPU / 无 ai2thor 依赖）
  python scripts/ai2thor_runtime_smoke.py --mode fake

  # 远程 unity 模式（A100；需 GPU + ai2thor extra）
  LLAMAR_AI2THOR_MODE=unity uv run python scripts/ai2thor_runtime_smoke.py \\
      --scene FloorPlan1 --agents 2 --report reports/unity_smoke.json

文档：docs/plans/2026-07-18-ai2thor-a2a-migration-implementation-plan.md §G1
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

# 仓库根入 sys.path —— 直跑 `python scripts/...` 时也能 import ai2thor_orch
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ═══════════════════════════════════════════════════════════════════════
# 报告构建 & 输出
# ═══════════════════════════════════════════════════════════════════════


def make_report(
    status: str,
    mode: str,
    scene: str,
    ai2thor_version: str | None,
    duration_seconds: float,
    metadata_schema: dict[str, Any],
    agent_start: dict[str, float] | None,
    agent_after_move: dict[str, float] | None,
    error: dict[str, str] | None,
    agents: int = 2,
    checks: dict[str, bool] | None = None,
    info: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一 schema 的 JSON 报告字典。"""
    return {
        "status": status,
        "mode": mode,
        "scene": scene,
        "agents": agents,
        "ai2thor_version": ai2thor_version,
        "duration_seconds": round(duration_seconds, 2),
        "checks": checks or {},
        "metadata_schema": metadata_schema,
        "agent_start": agent_start,
        "agent_after_move": agent_after_move,
        "info": info or {},
        "environment": environment or {},
        "error": error,
    }


def emit_report(report: dict[str, Any], report_path: str | None = None) -> None:
    """打印报告到 stdout，并可选地写出到文件。"""
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(f"\n{'=' * 60}")
    print("AI2Thor Runtime Smoke Report")
    print(f"{'=' * 60}")
    print(text)
    print(f"{'=' * 60}")
    if report_path:
        abs_path = os.path.abspath(report_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w") as f:
            f.write(text + "\n")
        print(f"Report written to: {abs_path}")


def _empty_schema() -> dict[str, Any]:
    return {
        "has_objects": False,
        "has_agent": False,
        "has_reachable_positions": False,
        "object_count": 0,
        "reachable_count": 0,
        "sample_object_keys": [],
    }


def _schema_from_metadata(
    metadata: dict[str, Any], reachable_count: int = 0
) -> dict[str, Any]:
    """从 controller metadata 提取 schema 摘要（unity/fake 共用形状）。"""
    obj_list = metadata.get("objects") or []
    agent_state = metadata.get("agent")
    if not isinstance(agent_state, dict):
        agents = metadata.get("agents") or []
        first = agents[0] if agents and isinstance(agents[0], dict) else {}
        agent_state = {"position": first.get("position")} if first else None
    return {
        "has_objects": len(obj_list) > 0,
        "has_agent": isinstance(agent_state, dict),
        "has_reachable_positions": reachable_count > 0,
        "object_count": len(obj_list),
        "reachable_count": reachable_count,
        "sample_object_keys": list(obj_list[0].keys()) if obj_list else [],
    }


def _error_dict(
    typ: str, message: str, traceback_str: str | None = None
) -> dict[str, str]:
    d: dict[str, str] = {"type": typ, "message": message}
    if traceback_str:
        d["traceback"] = traceback_str
    return d


def _position_of(event: Any, agent_idx: int = 0) -> dict[str, Any] | None:
    """从事件里取指定 agent 的位置（MultiAgentEvent / 单 Event 都支持）。"""
    events = getattr(event, "events", None)
    target = None
    if events and agent_idx < len(events):
        target = events[agent_idx]
    elif not events:
        target = event
    metadata = getattr(target, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    agent_state = metadata.get("agent")
    if not isinstance(agent_state, dict):
        agents = metadata.get("agents") or []
        agent_state = agents[agent_idx] if agent_idx < len(agents) else None
    if not isinstance(agent_state, dict):
        return None
    pos = agent_state.get("position")
    if not isinstance(pos, dict):
        return None
    return {"x": pos.get("x"), "y": pos.get("y"), "z": pos.get("z")}


def _environment_info() -> dict[str, Any]:
    """运行环境画像（A100 复现记录：OS / Python / 显示 / GPU / build）。"""
    info: dict[str, Any] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "display": os.environ.get("DISPLAY", ""),
        "headless_env": os.environ.get("LLAMAR_AI2THOR_HEADLESS", ""),
        "platform_env": os.environ.get("LLAMAR_AI2THOR_PLATFORM", ""),
    }
    try:  # best-effort：ai2thor 缺失（fake 模式）时留空
        import ai2thor
        import ai2thor.build

        info["ai2thor_version"] = getattr(ai2thor, "__version__", "unknown")
        info["ai2thor_build_commit"] = getattr(ai2thor.build, "COMMIT_ID", None)
    except Exception:  # noqa: BLE001
        info["ai2thor_version"] = None
        info["ai2thor_build_commit"] = None
    return info


# ═══════════════════════════════════════════════════════════════════════
# Fake 模式 — 确定性假数据，不导入 ai2thor
# ═══════════════════════════════════════════════════════════════════════


def run_fake(scene: str, agents: int = 2) -> dict[str, Any]:
    """生成确定性假 metadata，走完整个报告流水线（本地验证 schema / 退出码）。"""
    agent_start = {"x": -1.5, "y": 0.9009999632835388, "z": 0.0}
    agent_after_move = {"x": -1.5, "y": 0.9009999632835388, "z": 0.25}

    objects = [
        {
            "objectId": "CounterTop|+00.0|+00.0|+00.0",
            "objectType": "CounterTop",
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        },
        {
            "objectId": "Fridge|-01.5|+00.0|+01.0",
            "objectType": "Fridge",
            "position": {"x": -1.5, "y": 0.0, "z": 1.0},
        },
        {
            "objectId": "Mug|-00.5|+00.9|+01.5",
            "objectType": "Mug",
            "position": {"x": -0.5, "y": 0.9009999632835388, "z": 1.5},
        },
        {
            "objectId": "Plate|+01.0|+00.9|-00.5",
            "objectType": "Plate",
            "position": {"x": 1.0, "y": 0.9009999632835388, "z": -0.5},
        },
        {
            "objectId": "Apple|+00.5|+00.9|+01.0",
            "objectType": "Apple",
            "position": {"x": 0.5, "y": 0.9009999632835388, "z": 1.0},
        },
    ]

    reachable_positions = [
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.0},
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.25},
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.5},
        {"x": -1.25, "y": 0.9009999632835388, "z": 0.0},
        {"x": -1.25, "y": 0.9009999632835388, "z": 0.25},
    ]

    fake_metadata = {
        "agent": {
            "position": agent_start,
            "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
        },
        "agents": [
            {
                "name": f"Agent{i}",
                "position": agent_start if i == 0 else agent_after_move,
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                "inventory": {"objects": []},
            }
            for i in range(agents)
        ],
        "objects": objects,
        "reachablePositions": reachable_positions,
        "sceneName": scene,
    }

    schema = _schema_from_metadata(
        {"objects": objects, "agent": fake_metadata["agent"]},
        reachable_count=len(reachable_positions),
    )

    checks = {
        "import_ok": True,
        "controller_started": True,
        "multi_agent_events": True,
        "per_agent_metadata": True,
        "executor_round_ok": True,
        "adapter_surface_ok": True,
        "stop_clean": True,
    }
    info = {
        "fake": True,
        "note": "fake 模式不启动 Unity、不导入 ai2thor；schema 与退出码逻辑本地验证。",
        "agent_positions": [a["position"] for a in fake_metadata["agents"]],
        "done_action_supported": None,
        "move_caused_displacement": True,
    }

    return make_report(
        status="ok",
        mode="fake",
        scene=scene,
        ai2thor_version=None,
        duration_seconds=0.0,  # caller 填充
        metadata_schema=schema,
        agent_start=agent_start,
        agent_after_move=agent_after_move,
        error=None,
        agents=agents,
        checks=checks,
        info=info,
        environment=_environment_info(),
    )


# ═══════════════════════════════════════════════════════════════════════
# Unity 模式 — 真实 AI2Thor Controller（经编排层接线；需要 GPU / Unity build）
# ═══════════════════════════════════════════════════════════════════════


def run_unity(
    scene: str,
    timeout: int,
    *,
    agents: int = 2,
    controller_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """连接真实 AI2Thor Unity Controller，跑通编排层接线并断言。

    使用 signal.alarm 实现整体超时守卫（WSL 和 Linux 均可用）。
    ``controller_factory`` 为测试注入点（本地 mock controller 也可跑完整探针逻辑）。

    返回值恒为报告 dict（异常 → status=error；退出码由 :func:`main` 决定）。
    """

    def _timeout_handler(_signum, _frame):
        raise TimeoutError(f"Unity probe timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)

    checks: dict[str, bool] = {}
    info: dict[str, Any] = {}
    version: str | None = None
    agent_start: dict[str, float] | None = None
    agent_after_move: dict[str, float] | None = None
    schema = _empty_schema()
    environment = _environment_info()
    executor = None

    try:
        # ── 1. 依赖导入（ai2thor 缺失 → ImportError → 退出码 2） ──
        if controller_factory is None:
            try:
                import ai2thor.controller
            except ImportError as e:
                try:
                    import ai2thor as _ath

                    version = getattr(_ath, "__version__", "unknown")
                except ImportError:
                    version = "unknown"
                return make_report(
                    status="error",
                    mode="unity",
                    scene=scene,
                    ai2thor_version=version,
                    duration_seconds=0.0,
                    metadata_schema=_empty_schema(),
                    agent_start=None,
                    agent_after_move=None,
                    error=_error_dict(
                        "ImportError",
                        f"ai2thor.controller not installed: {e}",
                        traceback.format_exc(),
                    ),
                    agents=agents,
                    environment=environment,
                )
        try:
            import ai2thor as _ath
            import ai2thor.build as _ath_build

            version = getattr(_ath, "__version__", "unknown")
            info["ai2thor_build_commit"] = getattr(_ath_build, "COMMIT_ID", None)
        except ImportError:  # 注入式 factory：无 ai2thor 也允许本地干跑
            version = None
        checks["import_ok"] = True

        from ai2thor_orch.executor.controller_executor import ControllerExecutor
        from ai2thor_orch.executor.unity_controller import UnityController

        # ── 2. 启动 Controller（与实验同一条接线） ──
        adapter = UnityController(
            scene=scene,
            num_agents=agents,
            controller_factory=controller_factory,
        )
        checks["controller_started"] = True
        info["launch_options"] = {
            key: str(value) for key, value in adapter.launch_options.items()
        }
        underlying = adapter.controller

        # ── 3. multi-agent 事件结构 ──
        init_event = getattr(underlying, "last_event", None)
        init_events = list(getattr(init_event, "events", []) or [])
        checks["multi_agent_events"] = len(init_events) >= agents
        per_agent_ok = len(init_events) >= agents
        for idx, event in enumerate(init_events[:agents]):
            metadata = getattr(event, "metadata", None) or {}
            if metadata.get("agentId") != idx or not isinstance(
                metadata.get("agent"), dict
            ):
                per_agent_ok = False
        checks["per_agent_metadata"] = per_agent_ok
        agent_start = _position_of(init_event, 0)
        info["agent_positions_init"] = [
            _position_of(init_event, idx) for idx in range(agents)
        ]

        # ── 4. executor 一轮（含 NoOp → Pass 空动作映射） ──
        executor = ControllerExecutor(adapter)
        actions = [{"action": "MoveAhead"}] + [
            {"action": "NoOp"} for _ in range(agents - 1)
        ]
        results = executor.execute_step(actions)
        checks["executor_round_ok"] = len(results) == agents and all(
            bool((result.get("agent_metadata") or {}).get("lastActionSuccess"))
            for result in results
        )
        agent_after_move = _position_of(underlying.last_event, 0)

        # ── 5. 归一化消费面（barrier / verifier 依赖的字段） ──
        surface_ok = len(results) == agents
        for result in results:
            metadata = result.get("agent_metadata") or {}
            if (
                not isinstance(metadata.get("agents"), list)
                or len(metadata["agents"]) < agents
            ):
                surface_ok = False
            if not metadata.get("objects"):
                surface_ok = False
        checks["adapter_surface_ok"] = surface_ok
        if results:
            schema = _schema_from_metadata(
                results[0].get("agent_metadata") or {},
                reachable_count=int(info.get("reachable_positions_count") or 0),
            )

        # ── 6. 非 gating 记录：位移 / Done 支持 / reachable positions ──
        info["move_caused_displacement"] = bool(
            agent_start
            and agent_after_move
            and (
                agent_start.get("x") != agent_after_move.get("x")
                or agent_start.get("z") != agent_after_move.get("z")
            )
        )
        try:
            done_event = underlying.step({"action": "Done", "agentId": 0})
            done_metadata = getattr(done_event, "metadata", {}) or {}
            info["done_action_supported"] = bool(done_metadata.get("lastActionSuccess"))
        except ValueError as exc:  # build 不接受 Done：编排层已映射为空动作，无碍
            info["done_action_supported"] = False
            info["done_action_error"] = str(exc)
        try:
            reach_event = underlying.step(
                {"action": "GetReachablePositions", "agentId": 0}
            )
            reach_metadata = getattr(reach_event, "metadata", {}) or {}
            positions = reach_metadata.get("actionReturn") or []
            info["reachable_positions_count"] = (
                len(positions) if isinstance(positions, list) else 0
            )
        except Exception as exc:  # noqa: BLE001 - 非 gating 记录
            info["reachable_positions_count"] = 0
            info["reachable_positions_error"] = str(exc)
        schema = _schema_from_metadata(
            (results[0].get("agent_metadata") if results else {}) or {},
            reachable_count=int(info.get("reachable_positions_count") or 0),
        )

        # ── 7. 干净回收 ──
        executor.stop()
        executor = None
        checks["stop_clean"] = True

        failed = sorted(name for name, ok in checks.items() if not ok)
        if failed:
            return make_report(
                status="error",
                mode="unity",
                scene=scene,
                ai2thor_version=version,
                duration_seconds=0.0,
                metadata_schema=schema,
                agent_start=agent_start,
                agent_after_move=agent_after_move,
                error=_error_dict(
                    "AssertionError", f"unity 探针断言失败: {', '.join(failed)}"
                ),
                agents=agents,
                checks=checks,
                info=info,
                environment=environment,
            )

        return make_report(
            status="ok",
            mode="unity",
            scene=scene,
            ai2thor_version=version,
            duration_seconds=0.0,
            metadata_schema=schema,
            agent_start=agent_start,
            agent_after_move=agent_after_move,
            error=None,
            agents=agents,
            checks=checks,
            info=info,
            environment=environment,
        )

    except TimeoutError:
        return make_report(
            status="error",
            mode="unity",
            scene=scene,
            ai2thor_version=version,
            duration_seconds=float(timeout),
            metadata_schema=schema,
            agent_start=agent_start,
            agent_after_move=agent_after_move,
            error=_error_dict(
                "TimeoutError",
                f"Unity probe timed out after {timeout}s",
                traceback.format_exc(),
            ),
            agents=agents,
            checks=checks,
            info=info,
            environment=environment,
        )
    except Exception as e:  # noqa: BLE001 - 探针顶层：任何基础设施异常都转成 error 报告
        return make_report(
            status="error",
            mode="unity",
            scene=scene,
            ai2thor_version=version,
            duration_seconds=0.0,
            metadata_schema=schema,
            agent_start=agent_start,
            agent_after_move=agent_after_move,
            error=_error_dict(
                type(e).__name__,
                str(e),
                traceback.format_exc(),
            ),
            agents=agents,
            checks=checks,
            info=info,
            environment=environment,
        )
    finally:
        # ── 取消闹钟，恢复 handler；确保 Controller 释放资源 ──
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if executor is not None:
            try:
                executor.stop()
            except Exception as e:  # noqa: BLE001 - 收尾路径不允许再抛
                print(f"[smoke] executor.stop() failed: {e!r}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════


def exit_code_for(report: dict[str, Any]) -> int:
    """报告 → 退出码（0 ok / 1 运行时错误或断言失败 / 2 ImportError / 3 超时）。"""
    if report.get("status") == "ok":
        return 0
    err = report.get("error") or {}
    err_type = err.get("type", "")
    if err_type == "ImportError":
        return 2
    if err_type == "TimeoutError":
        return 3
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI2Thor Unity Runtime Smoke Probe — G1/G5 migration gate",
    )
    parser.add_argument(
        "--scene",
        default="FloorPlan1",
        help="AI2Thor scene name (default: FloorPlan1)",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=2,
        help="agent count for multi-agent initialization (default: 2)",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="Write JSON report to PATH (always printed to stdout as well)",
    )
    parser.add_argument(
        "--mode",
        choices=["fake", "unity"],
        default=None,
        help=(
            "Runtime mode: fake (deterministic mock, no ai2thor dependency) "
            "or unity (real Controller, requires GPU/Unity). "
            "Default: $LLAMAR_AI2THOR_MODE env var, or 'fake' if unset."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Overall probe timeout in seconds (default: 120, only used in unity mode)",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    # 确定运行模式：--mode > env LLAMAR_AI2THOR_MODE > "fake"
    mode = args.mode or os.environ.get("LLAMAR_AI2THOR_MODE", "fake")

    start_time = time.time()

    # ── 运行探针 ──
    if mode == "fake":
        report = run_fake(scene=args.scene, agents=args.agents)
    elif mode == "unity":
        report = run_unity(scene=args.scene, timeout=args.timeout, agents=args.agents)
    else:
        report = make_report(
            status="error",
            mode=mode,
            scene=args.scene,
            ai2thor_version=None,
            duration_seconds=0.0,
            metadata_schema=_empty_schema(),
            agent_start=None,
            agent_after_move=None,
            error=_error_dict(
                "ValueError",
                f"Unknown mode: {mode!r}. Valid modes: fake, unity",
                None,
            ),
            agents=args.agents,
        )

    # 填充实际耗时
    report["duration_seconds"] = round(time.time() - start_time, 2)

    # ── 输出报告 ──
    emit_report(report, args.report)

    # ── 确定退出码 ──
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(main())

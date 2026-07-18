#!/usr/bin/env python3
"""
AI2Thor Unity Runtime Smoke Probe — G1 迁移门禁脚本。

用途：
  探测 AI2Thor Unity runtime 能否正常启动并与 Controller 通信。
  支持 fake/unity 两种模式，输出统一的 JSON 报告。

运行模式：
  --mode fake   (默认) 确定性假数据，不依赖 ai2thor 包。
                 用于本地开发验证报告 schema 和退出码逻辑。
  --mode unity  真实 AI2Thor Controller，需要 GPU/Unity 环境。
                 仅在远程 A100 等具备 Unity 渲染能力的主机上运行。

退出码：
  0  — 探针成功，status=ok
  1  — 运行时异常（非 ImportError 的其他错误）
  2  — ai2thor 包未安装（ImportError）
  3  — 探针整体超时（--timeout 秒内未完成）

环境变量：
  LLAMAR_AI2THOR_MODE  — 默认运行模式。当 --mode 参数未显式传递时读取。
                         未设置时默认 "fake"。

用法示例：
  # 本地 fake 模式
  python scripts/ai2thor_runtime_smoke.py --mode fake

  # 远程 unity 模式（需 GPU + Unity build）
  LLAMAR_AI2THOR_MODE=unity python scripts/ai2thor_runtime_smoke.py --report reports/unity_smoke.json

  # 指定场景与超时
  python scripts/ai2thor_runtime_smoke.py --scene FloorPlan2 --timeout 60

文档：
  参见 docs/plans/2026-07-18-ai2thor-a2a-migration-implementation-plan.md §G1
"""

import argparse
import json
import os
import signal
import sys
import time
import traceback
from typing import Any, Dict, Optional


# ═══════════════════════════════════════════════════════════════════════
# 报告构建 & 输出
# ═══════════════════════════════════════════════════════════════════════


def make_report(
    status: str,
    mode: str,
    scene: str,
    ai2thor_version: Optional[str],
    duration_seconds: float,
    metadata_schema: Dict[str, Any],
    agent_start: Optional[Dict[str, float]],
    agent_after_move: Optional[Dict[str, float]],
    error: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """构造统一 schema 的 JSON 报告字典。"""
    return {
        "status": status,
        "mode": mode,
        "scene": scene,
        "ai2thor_version": ai2thor_version,
        "duration_seconds": round(duration_seconds, 2),
        "metadata_schema": metadata_schema,
        "agent_start": agent_start,
        "agent_after_move": agent_after_move,
        "error": error,
    }


def emit_report(report: Dict[str, Any], report_path: Optional[str] = None) -> None:
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


def _empty_schema() -> Dict[str, Any]:
    return {
        "has_objects": False,
        "has_agent": False,
        "has_reachable_positions": False,
        "object_count": 0,
        "reachable_count": 0,
        "sample_object_keys": [],
    }


def _error_dict(typ: str, message: str, traceback_str: Optional[str] = None) -> Dict[str, str]:
    d: Dict[str, str] = {"type": typ, "message": message}
    if traceback_str:
        d["traceback"] = traceback_str
    return d


# ═══════════════════════════════════════════════════════════════════════
# Fake 模式 — 确定性假数据，不导入 ai2thor
# ═══════════════════════════════════════════════════════════════════════


def run_fake(scene: str) -> Dict[str, Any]:
    """
    生成确定性假 metadata，走完整个报告流水线。

    用途：本地开发验证报告 schema 和退出码逻辑，完全不依赖 ai2thor 包。
    """
    # ── 确定性假 agent 位置 ──
    agent_start = {"x": -1.5, "y": 0.9009999632835388, "z": 0.0}
    agent_after_move = {"x": -1.5, "y": 0.9009999632835388, "z": 0.25}

    # ── 确定性假 objects（至少 3 个） ──
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

    # ── 确定性假 reachablePositions ──
    reachable_positions = [
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.0},
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.25},
        {"x": -1.5, "y": 0.9009999632835388, "z": 0.5},
        {"x": -1.25, "y": 0.9009999632835388, "z": 0.0},
        {"x": -1.25, "y": 0.9009999632835388, "z": 0.25},
    ]

    # ── 构造伪 metadata（模拟 unity event.metadata 结构） ──
    fake_metadata = {
        "agent": {
            "position": agent_start,
            "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
        },
        "objects": objects,
        "reachablePositions": reachable_positions,
        "sceneName": scene,
    }

    # ── 提取 metadata_schema ──
    obj_list = fake_metadata.get("objects") or []
    reachable_list = fake_metadata.get("reachablePositions") or []
    schema = {
        "has_objects": len(obj_list) > 0,
        "has_agent": isinstance(fake_metadata.get("agent"), dict),
        "has_reachable_positions": len(reachable_list) > 0,
        "object_count": len(obj_list),
        "reachable_count": len(reachable_list),
        "sample_object_keys": list(obj_list[0].keys()) if obj_list else [],
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
    )


# ═══════════════════════════════════════════════════════════════════════
# Unity 模式 — 真实 AI2Thor Controller（需要 GPU / Unity build）
# ═══════════════════════════════════════════════════════════════════════


def run_unity(scene: str, timeout: int) -> Dict[str, Any]:
    """
    连接真实 AI2Thor Unity Controller。

    使用 signal.alarm 实现整体超时守卫（WSL 和 Linux 均可用）。
    Controller.stop() 在 finally 块中确保释放。
    """
    # ── 超时守卫 ──
    def _timeout_handler(_signum, _frame):
        raise TimeoutError(f"Unity probe timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)

    controller = None
    version: Optional[str] = None
    agent_start: Optional[Dict[str, float]] = None

    try:
        # ── 延迟导入 ai2thor，捕获 ImportError ──
        try:
            import ai2thor.controller  # noqa: F401
        except ImportError as e:
            # 仍然尝试读取版本
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
            )

        # ── 获取版本 ──
        import ai2thor as _ath
        version = getattr(_ath, "__version__", "unknown")

        # ── 初始化 Controller（小分辨率 300×300 节省时间） ──
        controller = ai2thor.controller.Controller(
            width=300,
            height=300,
            scene=scene,
            gridSize=0.25,
        )

        # ── Reset → 记录起始位置 ──
        reset_event = controller.step("Reset")
        pos = reset_event.metadata["agent"]["position"]
        agent_start = {"x": pos["x"], "y": pos["y"], "z": pos["z"]}

        # ── 执行一次 MoveAhead ──
        move_event = controller.step(action="MoveAhead")
        pos_move = move_event.metadata["agent"]["position"]
        agent_after_move = {"x": pos_move["x"], "y": pos_move["y"], "z": pos_move["z"]}

        # ── 提取报告字段 ──
        meta = move_event.metadata
        obj_list = meta.get("objects") or []
        reachable_list = meta.get("reachablePositions") or []

        schema = {
            "has_objects": len(obj_list) > 0,
            "has_agent": isinstance(meta.get("agent"), dict),
            "has_reachable_positions": len(reachable_list) > 0,
            "object_count": len(obj_list),
            "reachable_count": len(reachable_list),
            "sample_object_keys": list(obj_list[0].keys()) if obj_list else [],
        }

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
        )

    except TimeoutError:
        return make_report(
            status="error",
            mode="unity",
            scene=scene,
            ai2thor_version=version,
            duration_seconds=float(timeout),
            metadata_schema=_empty_schema(),
            agent_start=agent_start,
            agent_after_move=None,
            error=_error_dict(
                "TimeoutError",
                f"Unity probe timed out after {timeout}s",
                traceback.format_exc(),
            ),
        )
    except Exception as e:
        return make_report(
            status="error",
            mode="unity",
            scene=scene,
            ai2thor_version=version,
            duration_seconds=0.0,
            metadata_schema=_empty_schema(),
            agent_start=agent_start,
            agent_after_move=None,
            error=_error_dict(
                type(e).__name__,
                str(e),
                traceback.format_exc(),
            ),
        )
    finally:
        # ── 取消闹钟，恢复 handler ──
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        # ── 确保 Controller 释放资源 ──
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI2Thor Unity Runtime Smoke Probe — G1 migration gate",
    )
    parser.add_argument(
        "--scene",
        default="FloorPlan1",
        help="AI2Thor scene name (default: FloorPlan1)",
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
        report = run_fake(scene=args.scene)
    elif mode == "unity":
        report = run_unity(scene=args.scene, timeout=args.timeout)
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
        )

    # 填充实际耗时
    report["duration_seconds"] = round(time.time() - start_time, 2)

    # ── 输出报告 ──
    emit_report(report, args.report)

    # ── 确定退出码 ──
    if report["status"] == "ok":
        return 0
    err = report.get("error") or {}
    err_type = err.get("type", "")
    if err_type == "ImportError":
        return 2
    if err_type == "TimeoutError":
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())

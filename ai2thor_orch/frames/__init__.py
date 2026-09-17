"""运行时关键帧捕获（F-frame）——FrameStore 与落盘契约。

设计基线：``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md``
§1.4。捕获点接线在 ``ai2thor_orch.executor.unity_controller``（init / 语义关键
动作成功 / run 终结），装配接线在 ``ai2thor_orch.env_pack``（``LLAMAR_AI2THOR_FRAMES``
开关 + run_dir / agent_names）。
"""

from ai2thor_orch.frames.frame_store import VALID_TAGS, FrameStore

__all__ = ["VALID_TAGS", "FrameStore"]

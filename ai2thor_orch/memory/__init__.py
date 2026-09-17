"""``ai2thor_orch.memory`` —— AI2Thor 专用轻量记忆组件（P2 空间记忆通道）。

设计基线：``.hermes/ai2thor/20260917-ai2thor-vlm-frame-p2-seed-unified-design.md``
§4.3（D3 = A 案自动 ingest）。**不复用** SAR ``SemanticMapStore``（grid 离散
坐标 vs AI2Thor 连续坐标 + objectId 语义，设计已定；SAR 侧零接触）。
"""

from ai2thor_orch.memory.sighting_store import SightingStore

__all__ = ["SightingStore"]

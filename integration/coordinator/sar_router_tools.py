"""
SAR-specific RouterAgent tools — replaces map_server query_map with SARBarrier query_sar_state.

SAR 专用 RouterAgent 工具 — 用 SARBarrier 的 query_sar_state 替代原有的 map_server query_map。
定义了协调器（Coordinator）在路由决策时可调用的查询工具。
"""
from __future__ import annotations

import json
from typing import Any

from openharness_a2a.coordinator.router_tools.base import RouterTool, ToolResult


class QuerySARStateTool(RouterTool):
    """
    SAR 环境状态查询工具 — 替代 MARoS 的 QueryMapTool。
    Router tool: query the current SAR environment state.

    Replaces MARoS's query_map tool. Queries SARBarrier directly instead of
    ROS map_server.  Extends RouterTool for full compatibility with the
    RouterAgent.register_tool() / get_tool_schemas() interface.
    """

    # 工具名称：LLM 通过此名称调用该工具
    name = "query_sar_state"
    # 工具描述：告知 LLM 该工具返回的数据内容和用途
    description = (
        "Get the current state of the Search & Rescue environment. "
        "Returns all fires (position, intensity, type), persons (position, status), "
        "reservoirs (position, resource_type), deposits, and agent states (position, inventory). "
        "Use this to understand the current situation before assigning subtasks."
    )
    # 工具参数模式：无参数（查询为无状态、全量快照）
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, barrier: Any) -> None:
        """
        初始化查询工具，保存 SARBarrier 引用。
        :param barrier: SARBarrier 实例，用于获取环境快照
        """
        self._barrier = barrier

    async def execute(self, **kwargs: Any) -> ToolResult:
        """
        执行环境状态查询，返回格式化的 ToolResult。
        Execute the query and return formatted state as ToolResult.

        :param kwargs: 未使用（无参数工具，保留以匹配 RouterTool 接口）
        :return: 成功时返回包含 JSON 快照的 ToolResult，失败时返回错误信息
        """
        try:
            # 通过 SARBarrier 获取环境快照（包含火情、人员、物资、智能体位置等）
            snapshot = self._barrier.get_env_snapshot()
            return ToolResult(
                success=True,
                content=json.dumps(snapshot, indent=2, default=str),
            )
        except Exception as e:
            # 查询失败时返回错误信息，避免 LLM 获取到不完整/错误的状态
            return ToolResult(
                success=False,
                error=f"query_sar_state failed: {e}",
            )

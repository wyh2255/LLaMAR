"""Minimal integration smoke test — verifies the full pipeline starts.
集成冒烟测试 — 验证 Barrier -> Worker -> Coordinator 整条流水线能否正常启动。"""
import asyncio

import pytest

from integration.sar_barrier import SARBarrier
from integration.sar_workers.sar_worker import SARWorker
from integration.coordinator.sar_router_tools import QuerySARStateTool


def test_barrier_worker_tool_chain():
    """Verify Barrier -> Tools -> Worker instantiation chain works end-to-end.
    集成测试：验证 SARBarrier、SARWorker 和 QuerySARStateTool 能否正确实例化并串联起来。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    worker_alice = SARWorker(
        agent_name="Alice",
        agent_idx=0,
        barrier=barrier,
        port=8191,
        coordinator_url="ws://localhost:8080",
    )
    worker_bob = SARWorker(
        agent_name="Bob",
        agent_idx=1,
        barrier=barrier,
        port=8192,
        coordinator_url="ws://localhost:8080",
    )

    # Verify workers have bound tools (tools are FunctionTool instances with _bound_node set)
    # 验证 Worker 已绑定正确的工具数量（预期 10 个 SAR 动作工具）
    assert len(worker_alice._tools) == 10
    # 验证 Worker 的 agent_name 被正确设置
    assert worker_alice.agent_name == "Alice"
    assert worker_bob.agent_name == "Bob"

    # Verify coordinator tool works
    # 验证 Coordinator 的查询工具名称正确
    tool = QuerySARStateTool(barrier)
    assert tool.name == "query_sar_state"

    barrier.stop()


@pytest.mark.asyncio
async def test_two_agents_submit_and_get_obs():
    """Alice and Bob both submit NoOp and get observations back.
    两智能体并发提交测试：Alice 和 Bob 各自提交 NoOp 动作，验证都能收到观测结果。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(idx):
        return await barrier.submit_action(idx, "NoOp")

    r0, r1 = await asyncio.gather(agent(0), agent(1))

    # 断言两个智能体的返回中都包含 "observation" 字段
    assert "observation" in r0
    assert "observation" in r1
    # 断言观测文本中包含对应智能体的名称或位置信息
    assert "Alice" in r0["observation"] or "I am at" in r0["observation"]
    assert "Bob" in r1["observation"] or "I am at" in r1["observation"]

    barrier.stop()

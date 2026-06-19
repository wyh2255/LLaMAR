"""Unit tests for SARBarrier -- requires no LLM, no Worker, just the barrier.
SARBarrier 单元测试 —— 不依赖 LLM 和 Worker，仅测试 Barrier 自身的功能。"""
import asyncio
import sys
from pathlib import Path

_llamar_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_llamar_root))

import pytest

from integration.sar_barrier import SARBarrier


def test_barrier_creation():
    """Barrier creates SAREnv and resets successfully.
    测试 Barrier 能够成功创建 SAREnv 环境并进行初始化。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    # 断言智能体数量为 2
    assert barrier.num_agents == 2
    # 断言环境已初始化
    assert barrier.env.initialized
    # 断言步数计数器从 0 开始
    assert barrier._step_counter == 0
    # 断言初始状态下任务尚未结束
    assert not barrier.is_finished()
    barrier.stop()


def test_barrier_snapshot():
    """get_env_snapshot returns plausible dict.
    测试 get_env_snapshot 方法返回的环境快照是否包含预期的字段。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    snap = barrier.get_env_snapshot()
    # 断言快照中包含 agents（智能体列表）
    assert "agents" in snap
    # 断言快照中包含 fires（火焰列表）
    assert "fires" in snap
    # 断言快照中包含 persons（待救援人员列表）
    assert "persons" in snap
    # 断言快照中有 2 个智能体（与 num_agents=2 一致）
    assert len(snap["agents"]) == 2
    barrier.stop()


def test_barrier_metrics():
    """get_metrics returns expected keys.
    测试 get_metrics 方法返回的指标字典是否包含预期的所有键。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    metrics = barrier.get_metrics()
    # 断言指标中包含 coverage（探索覆盖率）
    assert "coverage" in metrics
    # 断言指标中包含 transport_rate（运输速率）
    assert "transport_rate" in metrics
    # 断言指标中包含 steps（已执行步数）
    assert "steps" in metrics
    # 断言初始步数为 0
    assert metrics["steps"] == 0
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_two_agents():
    """Two agents submit actions -> step executes -> both get obs back.
    测试两个智能体并发提交动作后，step 正常推进且都能收到观测。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(agent_idx, action):
        return await barrier.submit_action(agent_idx, action)

    # Run both agents concurrently
    # 两个智能体并发提交 NoOp 动作
    results = await asyncio.gather(
        agent(0, "NoOp"),
        agent(1, "NoOp"),
    )

    # 断言返回了 2 个结果
    assert len(results) == 2
    # 断言每个结果的 step 为 1（第一次 step）
    assert results[0]["step"] == 1
    assert results[1]["step"] == 1
    # 断言结果中包含观测信息
    assert "observation" in results[0]
    assert "observation" in results[1]
    # 断言 Barrier 的步数计数器已递增到 1
    assert barrier._step_counter == 1
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_timeout():
    """If only one agent submits, NoOp is auto-filled after timeout.
    测试超时机制：仅一个智能体提交动作时，另一个超时后自动填充 NoOp。"""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    barrier.STEP_TIMEOUT = 0.5  # Short timeout for test 设置短超时以加速测试

    result = await barrier.submit_action(0, "NoOp")
    # Should complete (second agent auto-NoOp), not hang
    # 断言动作成功执行且 step 推进到 1（第二个智能体自动 NoOp 不会阻塞）
    assert result["step"] == 1
    barrier.stop()

"""Unit tests for SARBarrier -- requires no LLM, no Worker, just the barrier."""
import asyncio
import sys
from pathlib import Path

_llamar_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_llamar_root))

import pytest

from integration.sar_barrier import SARBarrier


def test_barrier_creation():
    """Barrier creates SAREnv and resets successfully."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    assert barrier.num_agents == 2
    assert barrier.env.initialized
    assert barrier._step_counter == 0
    assert not barrier.is_finished()
    barrier.stop()


def test_barrier_snapshot():
    """get_env_snapshot returns plausible dict."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    snap = barrier.get_env_snapshot()
    assert "agents" in snap
    assert "fires" in snap
    assert "persons" in snap
    assert len(snap["agents"]) == 2
    barrier.stop()


def test_barrier_metrics():
    """get_metrics returns expected keys."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    metrics = barrier.get_metrics()
    assert "coverage" in metrics
    assert "transport_rate" in metrics
    assert "steps" in metrics
    assert metrics["steps"] == 0
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_two_agents():
    """Two agents submit actions -> step executes -> both get obs back."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(agent_idx, action):
        return await barrier.submit_action(agent_idx, action)

    # Run both agents concurrently
    results = await asyncio.gather(
        agent(0, "NoOp"),
        agent(1, "NoOp"),
    )

    assert len(results) == 2
    assert results[0]["step"] == 1
    assert results[1]["step"] == 1
    assert "observation" in results[0]
    assert "observation" in results[1]
    assert barrier._step_counter == 1
    barrier.stop()


@pytest.mark.asyncio
async def test_submit_action_timeout():
    """If only one agent submits, NoOp is auto-filled after timeout."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    barrier.STEP_TIMEOUT = 0.5  # Short timeout for test

    result = await barrier.submit_action(0, "NoOp")
    # Should complete (second agent auto-NoOp), not hang
    assert result["step"] == 1
    barrier.stop()

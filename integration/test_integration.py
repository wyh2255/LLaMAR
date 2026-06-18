"""Minimal integration smoke test — verifies the full pipeline starts."""
import asyncio
import sys
from pathlib import Path

import pytest

_llamar_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_llamar_root))
sys.path.insert(0, "/home/wyh/daily_work/MARoS/maros_ws/a2a_lib")
sys.path.insert(0, "/home/wyh/daily_work/MARoS/my_a2a/src")

from integration.sar_barrier import SARBarrier
from integration.sar_workers.sar_worker import SARWorker
from integration.coordinator.sar_coordinator import SARCoordinator
from integration.coordinator.sar_router_tools import QuerySARStateTool


def test_barrier_worker_tool_chain():
    """Verify Barrier -> Tools -> Worker instantiation chain works end-to-end."""
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
    assert len(worker_alice._tools) == 10
    assert worker_alice.agent_name == "Alice"
    assert worker_bob.agent_name == "Bob"

    # Verify coordinator tool works
    tool = QuerySARStateTool(barrier)
    assert tool.name == "query_sar_state"

    barrier.stop()


@pytest.mark.asyncio
async def test_two_agents_submit_and_get_obs():
    """Alice and Bob both submit NoOp and get observations back."""
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)

    async def agent(idx):
        return await barrier.submit_action(idx, "NoOp")

    r0, r1 = await asyncio.gather(agent(0), agent(1))

    assert "observation" in r0
    assert "observation" in r1
    assert "Alice" in r0["observation"] or "I am at" in r0["observation"]
    assert "Bob" in r1["observation"] or "I am at" in r1["observation"]

    barrier.stop()

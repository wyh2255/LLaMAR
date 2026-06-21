"""Tests for SARCoordinator refactored methods.
SARCoordinator 重构后方法测试 — 验证 _build_router、_build_app 和 _handle_worker_ws 的正确性。

Refactoring extracted ``start()`` into three private methods:
  - ``_build_router()`` — creates SARRouterAgent with 7 SAR tools registered
  - ``_build_app(router)`` — builds FastAPI app with SAR executor and routes
  - ``_handle_worker_ws()`` — WebSocket handler for worker connections

Layer 1: static tests (no network, no LLM)
Layer 2: integration test (starts/stops actual coordinator)
Layer 3: existing tests (test_sar_barrier.py, test_integration.py) — referenced in comments
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import socket
from typing import Any
from unittest.mock import MagicMock

import pytest

from integration.coordinator.sar_coordinator import (
    SARCoordinator,
    SAR_COORDINATOR_SYSTEM_PROMPT,
    SARRouterAgent,
)
from integration.sar_barrier import SARBarrier


# ═══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_fake_coord_server():
    """Create a minimal fake CoordinatorServer for static testing.

    Provides all ``_-prefixed`` internal attributes that ``_build_router()``
    and ``_build_app()`` access, avoiding the need for rclpy / LLM / ROS 2
    infrastructure in Layer 1 tests.

    Returns:
        SimpleNamespace with _llm_client, _agent_registry, _memory_client,
        _task_queue, _task_logger, _a2a_port, _worker_ws, _worker_ws_lock,
        and async stub methods (_start_cleanup_task, _stop_cleanup_task,
        _handle_worker_message).
    """
    from types import SimpleNamespace

    # Agent registry mock — returns plausible agent descriptions
    agent_registry = MagicMock()
    agent_registry.get_all_agents_prompt_text.return_value = (
        "- **Alice**: SAR rescue robot. A2A endpoint at http://localhost:8191/\n"
        "- **Bob**: SAR rescue robot. A2A endpoint at http://localhost:8192/"
    )
    # Also support agent_registry.get(agent_id) for PushTaskTool
    agent_registry.get.return_value = {
        "name": "test-agent",
        "endpoint": "http://localhost:8190",
    }

    # Task queue mock
    task_queue = MagicMock()

    # Memory client mock
    memory_client = MagicMock()

    # Task logger mock
    task_logger = MagicMock()

    # Async stub methods (replace real CoordinatorServer lifecycle methods)
    async def _start_cleanup_task():
        pass

    async def _stop_cleanup_task():
        pass

    async def _handle_worker_message(worker_id: str, data: dict):
        pass

    cs = SimpleNamespace()
    cs._llm_client = None  # Not needed for static tool registration
    cs._agent_registry = agent_registry
    cs._memory_client = memory_client
    cs._task_queue = task_queue
    cs._task_logger = task_logger
    cs._a2a_port = 9090
    cs._worker_ws = {}
    cs._worker_ws_lock = asyncio.Lock()
    cs._a2a_server_task = None
    cs._start_cleanup_task = MagicMock(side_effect=_start_cleanup_task)
    cs._stop_cleanup_task = MagicMock(side_effect=_stop_cleanup_task)
    cs._handle_worker_message = MagicMock(side_effect=_handle_worker_message)

    return cs


def _port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Check whether a TCP server is listening on the given port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        result = s.connect_ex((host, port))
        return result == 0
    except socket.error:
        return False
    finally:
        s.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 1 — Static tests (no network, no LLM)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRouterBuilder:
    """Tests for SARCoordinator._build_router() — tool registration and configuration."""

    def test_router_registers_all_tools(self):
        """_build_router() returns SARRouterAgent with exactly 7 tools registered."""
        barrier = SARBarrier(num_agents=2, scene=1, seed=42)
        coordinator = SARCoordinator(
            barrier=barrier,
            agent_names=["Alice", "Bob"],
            worker_ports={"Alice": 8191, "Bob": 8192},
            port=9098,
        )
        coordinator._coord_server = _make_fake_coord_server()

        try:
            router = coordinator._build_router()

            assert isinstance(router, SARRouterAgent)
            assert len(router._tools) == 7, (
                f"Expected 7 tools, got {len(router._tools)}: {list(router._tools.keys())}"
            )

            expected_names = {
                "push_task",
                "wait_for_result",
                "cancel_task",
                "query_memory",
                "query_sar_state",
                "record_note",
                "bash",
            }
            actual_names = set(router._tools.keys())
            assert actual_names == expected_names, (
                f"Tool name mismatch.\n"
                f"  Missing: {expected_names - actual_names}\n"
                f"  Unexpected: {actual_names - expected_names}"
            )
        finally:
            barrier.stop()

    def test_router_tools_share_pending_tasks(self):
        """PushTaskTool, WaitForResultTool, CancelTaskTool all share the same _pending_tasks dict."""
        barrier = SARBarrier(num_agents=2, scene=1, seed=42)
        coordinator = SARCoordinator(
            barrier=barrier,
            agent_names=["Alice", "Bob"],
            worker_ports={"Alice": 8191, "Bob": 8192},
            port=9098,
        )
        coordinator._coord_server = _make_fake_coord_server()

        try:
            router = coordinator._build_router()

            push_tool = router._tools["push_task"]
            wait_tool = router._tools["wait_for_result"]
            cancel_tool = router._tools["cancel_task"]

            assert push_tool._pending_tasks is wait_tool._pending_tasks
            assert push_tool._pending_tasks is cancel_tool._pending_tasks
            assert push_tool._pending_tasks is coordinator._pending_tasks
        finally:
            barrier.stop()

    def test_router_has_query_sar_state_tool(self):
        """query_sar_state tool's _barrier reference matches coordinator._barrier."""
        barrier = SARBarrier(num_agents=2, scene=1, seed=42)
        coordinator = SARCoordinator(
            barrier=barrier,
            agent_names=["Alice", "Bob"],
            worker_ports={"Alice": 8191, "Bob": 8192},
            port=9098,
        )
        coordinator._coord_server = _make_fake_coord_server()

        try:
            router = coordinator._build_router()

            assert "query_sar_state" in router._tools
            tool = router._tools["query_sar_state"]
            assert tool._barrier is coordinator._barrier
            assert tool._barrier is barrier
            assert tool.name == "query_sar_state"
            assert isinstance(tool.description, str) and len(tool.description) > 0
        finally:
            barrier.stop()


class TestSystemPrompt:
    """Tests for SARRouterAgent._build_system_prompt()."""

    def test_router_system_prompt_contains_agents(self):
        """_build_system_prompt injects agent descriptions into the SAR template."""
        registry = MagicMock()
        registry.get_all_agents_prompt_text.return_value = (
            "- **Alice**: SAR rescue robot. A2A endpoint at http://localhost:8191/\n"
            "- **Bob**: SAR rescue robot. A2A endpoint at http://localhost:8192/"
        )

        router = SARRouterAgent(
            sar_system_prompt=SAR_COORDINATOR_SYSTEM_PROMPT,
            llm_client=None,
            registry=registry,
            memory_client=None,
        )

        prompt = router._build_system_prompt("find and rescue trapped persons")

        assert "Alice" in prompt
        assert "Bob" in prompt
        assert "rescue" in prompt.lower()
        assert "{agents_text}" not in prompt, (
            "System prompt still contains unfilled {agents_text} placeholder"
        )

    def test_system_prompt_without_registry_is_still_valid(self):
        """_build_system_prompt works even when _registry is None."""
        router = SARRouterAgent(
            sar_system_prompt=SAR_COORDINATOR_SYSTEM_PROMPT,
            llm_client=None,
            registry=None,
            memory_client=None,
        )

        prompt = router._build_system_prompt("test request")

        assert isinstance(prompt, str)
        assert len(prompt) > 0
        assert "Search & Rescue" in prompt or "rescue" in prompt.lower()


class TestHandleWorkerWS:
    """Tests for SARCoordinator._handle_worker_ws() method."""

    def test_handle_worker_ws_signature(self):
        """_handle_worker_ws exists, is async, and has correct signature."""
        barrier = SARBarrier(num_agents=2, scene=1, seed=42)
        coordinator = SARCoordinator(
            barrier=barrier,
            agent_names=["Alice", "Bob"],
            worker_ports={"Alice": 8191, "Bob": 8192},
        )

        try:
            assert hasattr(coordinator, "_handle_worker_ws")

            method = coordinator._handle_worker_ws
            assert asyncio.iscoroutinefunction(method)

            sig = inspect.signature(method)
            param_names = list(sig.parameters.keys())

            assert "websocket" in param_names
            assert "worker_id" in param_names
            assert len(param_names) == 2

            # worker_id should be typed as str
            worker_id_param = sig.parameters["worker_id"]
            anno_str = str(worker_id_param.annotation)
            assert "str" in anno_str
        finally:
            barrier.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 2 — Integration test (starts/stops actual coordinator)
# ═══════════════════════════════════════════════════════════════════════════════


# ROS 2 rclpy 检查 — 无此依赖时跳过 coordinator 集成测试
_has_rclpy = importlib.util.find_spec("rclpy") is not None


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_rclpy, reason="requires ROS 2 (rclpy not available)")
async def test_coordinator_start_stop_cycle():
    """SARCoordinator.start() binds port, stop() releases it.

    Uses a non-standard port (9099) to avoid conflicts with other services.
    """
    barrier = SARBarrier(num_agents=2, scene=1, seed=42)
    coordinator = SARCoordinator(
        barrier=barrier,
        agent_names=["Alice", "Bob"],
        worker_ports={"Alice": 8191, "Bob": 8192},
        port=9099,
    )

    try:
        await coordinator.start()
        await asyncio.sleep(0.5)

        assert _port_is_listening(9099), (
            "Coordinator should be listening on port 9099 after start()"
        )
        assert hasattr(coordinator, "_coord_server")
        assert hasattr(coordinator, "_uvicorn_server")
        assert coordinator._server_task is not None

    finally:
        try:
            await coordinator.stop()
        except Exception:
            pass

        await asyncio.sleep(0.5)
        assert not _port_is_listening(9099, timeout=0.5), (
            "Port 9099 should be released after coordinator.stop()"
        )

        barrier.stop()

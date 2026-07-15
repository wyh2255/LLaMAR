"""Tests for ConfigureTeamTool, DisbandTeamTool, SyncTeamTool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from a2a.builtin_tools.configure_team import ConfigureTeamTool, DisbandTeamTool, SyncTeamTool
from a2a.coordinator.team_registry import CoordinatorTeamRegistry
from a2a.coordinator.sender_service import CoordinatorSenderService
from a2a.shared.types import WorkerNode, WorkerStatus


@pytest.fixture
def registry() -> CoordinatorTeamRegistry:
    return CoordinatorTeamRegistry()


@pytest.fixture
def mock_sender():
    s = MagicMock(spec=CoordinatorSenderService)
    s.send_team_update = AsyncMock(return_value={"success": True, "task_id": "t-1"})
    s.send_team_revoke = AsyncMock(return_value={"success": True, "task_id": "t-2"})
    return s


@pytest.fixture
def mock_worker_registry():
    r = MagicMock()
    r.get.side_effect = lambda wid: WorkerNode(
        worker_id=wid, a2a_endpoint=f"http://{wid}:9999/", status=WorkerStatus.ONLINE,
    )
    return r


@pytest.fixture
def configure_tool(registry, mock_sender, mock_worker_registry):
    return ConfigureTeamTool(registry=registry, sender=mock_sender, worker_registry=mock_worker_registry)


@pytest.fixture
def disband_tool(registry, mock_sender):
    return DisbandTeamTool(registry=registry, sender=mock_sender)


@pytest.fixture
def sync_tool(registry, mock_sender):
    return SyncTeamTool(registry=registry, sender=mock_sender)


class TestConfigureTeamTool:
    @pytest.mark.asyncio
    async def test_name(self, configure_tool):
        assert configure_tool.name == "configure_team"

    @pytest.mark.asyncio
    async def test_empty_members(self, configure_tool):
        assert (await configure_tool.execute(member_ids=[])).error == "empty_members"

    @pytest.mark.asyncio
    async def test_duplicate_members(self, configure_tool):
        assert (await configure_tool.execute(member_ids=["A", "A"])).error == "duplicate_members"

    @pytest.mark.asyncio
    async def test_missing_worker(self, configure_tool, mock_worker_registry):
        mock_worker_registry.get.side_effect = KeyError("X")
        assert (await configure_tool.execute(member_ids=["X"])).error == "worker_not_found"

    @pytest.mark.asyncio
    async def test_configure_uses_for_delivery(self, configure_tool, registry):
        """configure_for_delivery returns atomic plan used directly."""
        await configure_tool.execute(member_ids=["Alice", "Bob"])
        dto = registry.current()
        assert dto is not None
        assert "Alice" in dto.member_ids

    @pytest.mark.asyncio
    async def test_sends_team_update_to_all(self, configure_tool, mock_sender):
        await configure_tool.execute(member_ids=["Alice", "Bob"])
        assert mock_sender.send_team_update.await_count == 2

    @pytest.mark.asyncio
    async def test_partial_delivery_failure(self, configure_tool, mock_sender):
        mock_sender.send_team_update = AsyncMock()
        mock_sender.send_team_update.side_effect = [
            {"success": True, "task_id": "t-1"},
            {"success": False, "error": "timeout"},
        ]
        result = await configure_tool.execute(member_ids=["Alice", "Bob"])
        assert result.success is False
        assert result.error == "partial_delivery_failure"
        assert result.data.get("committed") is True
        assert "Bob" in result.data.get("failed_member_ids", [])

    @pytest.mark.asyncio
    async def test_epoch_increments(self, configure_tool, registry):
        await configure_tool.execute(member_ids=["A"])
        e1 = registry.epoch
        await configure_tool.execute(member_ids=["A", "B"])
        assert registry.epoch == e1 + 1

    @pytest.mark.asyncio
    async def test_revokes_removed(self, configure_tool, mock_sender):
        await configure_tool.execute(member_ids=["A", "B"])
        await configure_tool.execute(member_ids=["A"])
        assert mock_sender.send_team_revoke.await_count >= 1


class TestDisbandTeamTool:
    @pytest.mark.asyncio
    async def test_no_active_team(self, disband_tool):
        assert (await disband_tool.execute()).error == "no_active_team"

    @pytest.mark.asyncio
    async def test_disband_for_delivery_atomic(self, configure_tool, disband_tool, registry):
        """disband_for_delivery returns plan with registry already cleared."""
        await configure_tool.execute(member_ids=["Alice", "Bob"])
        result = await disband_tool.execute()
        assert result.success is True
        assert registry.current() is None  # registry cleared atomically

    @pytest.mark.asyncio
    async def test_disband_partial_failure(self, configure_tool, disband_tool, mock_sender):
        await configure_tool.execute(member_ids=["Alice", "Bob"])
        mock_sender.send_team_revoke = AsyncMock()
        mock_sender.send_team_revoke.side_effect = [
            {"success": True},
            {"success": False, "error": "timeout"},
        ]
        result = await disband_tool.execute()
        assert result.success is False
        assert result.error == "partial_revoke_failure"
        assert disband_tool._registry.current() is None

    @pytest.mark.asyncio
    async def test_epoch_increments_after_disband(self, configure_tool, disband_tool, registry):
        await configure_tool.execute(member_ids=["A"])
        e1 = registry.epoch
        await disband_tool.execute()
        assert registry.epoch == e1 + 1


class TestSyncTeamTool:
    @pytest.mark.asyncio
    async def test_no_active_team(self, sync_tool):
        assert (await sync_tool.execute()).error == "no_active_team"

    @pytest.mark.asyncio
    async def test_sync_all(self, configure_tool, sync_tool, mock_sender):
        await configure_tool.execute(member_ids=["A", "B"])
        result = await sync_tool.execute()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_sync_specific(self, configure_tool, sync_tool, mock_sender):
        await configure_tool.execute(member_ids=["A", "B"])
        mock_sender.send_team_update.reset_mock()
        result = await sync_tool.execute(member_ids=["A"])
        assert result.success is True
        assert mock_sender.send_team_update.await_count == 1

    @pytest.mark.asyncio
    async def test_sync_invalid(self, configure_tool, sync_tool):
        await configure_tool.execute(member_ids=["A"])
        assert (await sync_tool.execute(member_ids=["Ghost"])).error == "not_team_members"

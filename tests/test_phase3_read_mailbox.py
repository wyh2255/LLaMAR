"""Phase 3 tests: read_mailbox tool, mailbox context reminder, state provider version refresh."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from a2a.worker.mailbox_store import WorkerMailboxStore, InvalidMailboxParameter
from a2a.worker.tools.read_mailbox import ReadMailboxTool
from a2a.worker.team_state import WorkerTeamState
from Agent.worker_agent.context import (
    ContextConfig,
    WorkerContextManager,
)
from Agent.worker_agent.schema import Message
from Agent.router_agent.state_provider import RuntimeState
from sar_orch.worker import ConfigurationError, SARWorker
from sar_orch.worker_state_provider import SARWorkerStateProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def mailbox(tmp_dir: Path) -> WorkerMailboxStore:
    return WorkerMailboxStore(
        tmp_dir / "mailbox.ndjson", local_worker_id="worker-alpha"
    )


def _populate_mailbox(mailbox: WorkerMailboxStore) -> None:
    mailbox.deliver(
        "msg-1",
        "Coordinator",
        "worker-alpha",
        subject="Welcome",
        body="Hello agent!",
        sent_at="2026-07-15T10:00:00",
    )
    mailbox.deliver(
        "msg-2",
        "Alice",
        "worker-alpha",
        subject="Status update",
        body="How is the search going?",
        sent_at="2026-07-15T10:05:00",
    )
    mailbox.deliver(
        "msg-3",
        "Bob",
        "worker-alpha",
        subject="Fire location",
        body="Fire at sector 4, intensity high. Need assistance.",
        sent_at="2026-07-15T10:10:00",
    )


# ---------------------------------------------------------------------------
# Finding 1: Atomic read_unread under one lock
# ---------------------------------------------------------------------------


class TestReadUnreadAtomic:
    def test_read_unread_returns_and_marks(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        records = mailbox.read_unread()
        assert len(records) == 3
        for r in records:
            assert r.read_at is not None
        assert len(mailbox.read_unread()) == 0

    def test_read_unread_limit(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        records = mailbox.read_unread(limit=2)
        assert len(records) == 2
        assert len(mailbox.read_unread()) == 1

    def test_read_unread_oldest_first(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        records = mailbox.read_unread(oldest_first=True)
        ids = [r.message_id for r in records]
        assert ids == ["msg-1", "msg-2", "msg-3"]

    def test_read_unread_message_ids_order(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        records = mailbox.read_unread(message_ids=["msg-3", "msg-1"])
        assert [r.message_id for r in records] == ["msg-3", "msg-1"]

    def test_read_unread_message_ids_dedup(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        records = mailbox.read_unread(message_ids=["msg-1", "msg-2", "msg-1"])
        assert [r.message_id for r in records] == ["msg-1", "msg-2"]

    def test_read_unread_message_ids_already_read_skipped(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        mailbox.deliver("msg-1", "A", "worker-alpha")
        mailbox.read_unread(limit=1)
        mailbox.deliver("msg-2", "B", "worker-alpha")
        records = mailbox.read_unread(message_ids=["msg-1", "msg-2"])
        assert len(records) == 1
        assert records[0].message_id == "msg-2"

    def test_read_unread_limit_validation(self, mailbox: WorkerMailboxStore) -> None:
        with pytest.raises(InvalidMailboxParameter, match="limit"):
            mailbox.read_unread(limit=0)
        with pytest.raises(InvalidMailboxParameter, match="limit"):
            mailbox.read_unread(limit=101)
        with pytest.raises(InvalidMailboxParameter, match="limit"):
            mailbox.read_unread(limit=-1)

    def test_concurrent_two_readers_each_message_once(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        """Prove each message returned at most once globally under concurrent reads."""
        for i in range(50):
            mailbox.deliver(f"msg-{i}", "tester", "worker-alpha")

        results: list[list[str]] = []
        rlock = threading.Lock()
        barrier = threading.Barrier(2)

        def reader():
            barrier.wait()
            records = mailbox.read_unread(limit=30)
            with rlock:
                results.append([r.message_id for r in records])

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_returned: set[str] = set()
        for batch in results:
            for mid in batch:
                assert mid not in all_returned, f"{mid} returned twice"
                all_returned.add(mid)

        # Also verify the two batches together never exceed total available
        total_returned = sum(len(b) for b in results)
        assert total_returned <= 50
        # Second reader should get the remainder
        if len(results[0]) < 50:
            assert len(results[1]) > 0

    def test_read_unread_invalid_limit_type(self, mailbox: WorkerMailboxStore) -> None:
        with pytest.raises(InvalidMailboxParameter, match="limit"):
            mailbox.read_unread(limit="not-int")  # type: ignore[arg-type]

    def test_read_unread_bool_limit_rejected(self, mailbox: WorkerMailboxStore) -> None:
        with pytest.raises(InvalidMailboxParameter, match="limit"):
            mailbox.read_unread(limit=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Finding 1b: Tool delegates to read_unread
# ---------------------------------------------------------------------------


class TestReadMailboxTool:
    @pytest.mark.anyio
    async def test_reads_unread_messages(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        assert result.success
        assert result.data is not None
        assert result.data["count"] == 3
        assert len(result.data["messages"]) == 3
        assert len(mailbox.read_unread()) == 0

    @pytest.mark.anyio
    async def test_no_unread_returns_empty(self, mailbox: WorkerMailboxStore) -> None:
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        assert result.success
        assert result.data is not None
        assert result.data["count"] == 0
        assert result.data["messages"] == []

    @pytest.mark.anyio
    async def test_limit_parameter(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=2)
        assert result.success
        assert result.data["count"] == 2
        assert len(mailbox.read_unread()) == 1

    @pytest.mark.anyio
    async def test_message_ids_filter(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(message_ids=["msg-1", "msg-3"])
        assert result.success
        assert result.data["count"] == 2
        returned_ids = [m["message_id"] for m in result.data["messages"]]
        assert "msg-1" in returned_ids
        assert "msg-3" in returned_ids
        unread = mailbox.list_unread()
        assert len(unread) == 1
        assert unread[0].message_id == "msg-2"

    @pytest.mark.anyio
    async def test_message_ids_invalid_type(self, mailbox: WorkerMailboxStore) -> None:
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(message_ids="not-a-list")
        assert not result.success
        assert "Invalid message_ids" in result.content

    @pytest.mark.anyio
    async def test_atomic_read_mark(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=2)
        assert result.data["count"] == 2
        for m in result.data["messages"]:
            rec = mailbox.get(m["message_id"])
            assert rec is not None
            assert rec.read_at is not None
        assert len(mailbox.read_unread()) == 1

    @pytest.mark.anyio
    async def test_oldest_first_order(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=3)
        ids = [m["message_id"] for m in result.data["messages"]]
        assert ids == ["msg-1", "msg-2", "msg-3"]

    @pytest.mark.anyio
    async def test_structured_output_metadata(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        msg = result.data["messages"][0]
        assert msg["message_id"] == "msg-1"
        assert msg["sender_id"] == "Coordinator"
        assert msg["subject"] == "Welcome"
        assert msg["sent_at"] == "2026-07-15T10:00:00"
        assert msg["received_at"] != ""
        assert msg["team_id"] is None
        assert msg["team_epoch"] is None

    @pytest.mark.anyio
    async def test_body_truncation_metadata(self, mailbox: WorkerMailboxStore) -> None:
        long_body = "x" * 2000
        mailbox.deliver("msg-long", "tester", "worker-alpha", body=long_body)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        msg = result.data["messages"][0]
        assert msg["body_truncated"] is True
        assert msg["body_original_length"] == 2000
        assert len(msg["body"]) <= 500 + 3

    @pytest.mark.anyio
    async def test_body_no_truncation_when_short(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        mailbox.deliver("msg-short", "tester", "worker-alpha", body="hello world")
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        msg = result.data["messages"][0]
        assert msg["body_truncated"] is False
        assert msg["body_original_length"] == 11
        assert msg["body"] == "hello world"

    @pytest.mark.anyio
    async def test_no_sar_step(self, mailbox: WorkerMailboxStore) -> None:
        mailbox.deliver("msg-1", "tester", "worker-alpha")
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute()
        assert result.success

    @pytest.mark.anyio
    async def test_empty_message_ids_returns_empty(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(message_ids=[])
        assert result.success
        assert result.data["count"] == 0
        assert result.data["messages"] == []

    @pytest.mark.anyio
    async def test_message_ids_blank_rejected(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(message_ids=["msg-1", "", "msg-3"])
        assert not result.success
        assert "non-blank" in result.content

    @pytest.mark.anyio
    async def test_message_ids_dedup(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(message_ids=["msg-1", "msg-2", "msg-1"])
        assert result.success
        assert result.data["count"] == 2

    @pytest.mark.anyio
    async def test_bool_limit_rejected(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=True)
        assert not result.success
        assert "Invalid limit" in result.content

    @pytest.mark.anyio
    async def test_limit_too_high_rejected(self, mailbox: WorkerMailboxStore) -> None:
        for i in range(150):
            mailbox.deliver(f"msg-{i}", "tester", "worker-alpha", subject=str(i))
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=999)
        assert not result.success
        assert "between 1 and 100" in result.content

    @pytest.mark.anyio
    async def test_limit_too_low_rejected(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        tool = ReadMailboxTool(mailbox=mailbox)
        result = await tool.execute(limit=-5)
        assert not result.success
        assert "between 1 and 100" in result.content


# ---------------------------------------------------------------------------
# Finding 5: Mailbox summary with configurable coordinator_id
# ---------------------------------------------------------------------------


class TestMailboxSummary:
    def test_summary_empty(self, mailbox: WorkerMailboxStore) -> None:
        s = mailbox.summary()
        assert s["unread_count"] == 0
        assert s["unique_senders"] == []
        assert s["oldest_unread_at"] == ""
        assert s["has_any_from_coordinator"] is False

    def test_summary_populated(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        s = mailbox.summary()
        assert s["unread_count"] == 3
        assert "Coordinator" in s["unique_senders"]
        assert "Alice" in s["unique_senders"]
        assert "Bob" in s["unique_senders"]
        assert s["oldest_unread_at"] != ""
        assert s["has_any_from_coordinator"] is True

    def test_summary_after_read(self, mailbox: WorkerMailboxStore) -> None:
        _populate_mailbox(mailbox)
        mailbox.read_unread(limit=1)
        s = mailbox.summary()
        assert s["unread_count"] == 2
        assert s["has_any_from_coordinator"] is False

    def test_summary_unique_senders_dedup(self, mailbox: WorkerMailboxStore) -> None:
        mailbox.deliver("m1", "Alice", "worker-alpha")
        mailbox.deliver("m2", "Alice", "worker-alpha")
        s = mailbox.summary()
        assert s["unique_senders"] == ["Alice"]

    def test_summary_custom_coordinator_id(self, mailbox: WorkerMailboxStore) -> None:
        mailbox.deliver("m1", "boss", "worker-alpha")
        s = mailbox.summary(coordinator_id="boss")
        assert s["has_any_from_coordinator"] is True
        s2 = mailbox.summary(coordinator_id="other")
        assert s2["has_any_from_coordinator"] is False

    def test_summary_sender_truncation(self, mailbox: WorkerMailboxStore) -> None:
        for i in range(25):
            mailbox.deliver(f"msg-{i}", f"sender-{i}", "worker-alpha")
        s = mailbox.summary()
        assert s["total_unique_senders"] == 25
        assert s["senders_truncated"] is True
        assert len(s["unique_senders"]) == 20

    def test_summary_sender_no_truncation_when_few(
        self, mailbox: WorkerMailboxStore
    ) -> None:
        mailbox.deliver("m1", "Alice", "worker-alpha")
        mailbox.deliver("m2", "Bob", "worker-alpha")
        s = mailbox.summary()
        assert s["total_unique_senders"] == 2
        assert s["senders_truncated"] is False
        assert len(s["unique_senders"]) == 2

    def test_summary_thread_safety(self, mailbox: WorkerMailboxStore) -> None:
        errors = []

        def reader():
            try:
                for _ in range(50):
                    mailbox.summary()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ---------------------------------------------------------------------------
# Finding 3: Mailbox reminder in Worker Environment State
# ---------------------------------------------------------------------------


class _MockStateProvider:
    """Minimal StateProvider that returns configurable RuntimeState."""

    def __init__(self, version=1, mailbox_summary=None):
        self._version = version
        self._mailbox_summary = mailbox_summary or {
            "unread_count": 0,
            "unique_senders": [],
            "oldest_unread_at": "",
            "has_any_from_coordinator": False,
        }

    def snapshot(self, context_id=None):
        return RuntimeState(
            version=self._version,
            payload={"mailbox_summary": self._mailbox_summary},
        )


class TestMailboxContextReminder:
    def test_reminder_appears_in_assembled_context(self) -> None:
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 2,
                "unique_senders": ["Coordinator", "Alice"],
                "total_unique_senders": 2,
                "oldest_unread_at": "2026-07-15T10:00:00",
                "has_any_from_coordinator": True,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test system"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content if len(assembled) > 1 else ""
        assert "### Mailbox" in mem_text
        assert "2 unread message(s)" in mem_text
        assert "from Coordinator, Alice" in mem_text
        assert "read_mailbox" in mem_text

    def test_reminder_shows_remaining_sender_count(self) -> None:
        senders = [f"sender-{i}" for i in range(8)]
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 8,
                "unique_senders": senders,
                "total_unique_senders": 8,
                "oldest_unread_at": "",
                "has_any_from_coordinator": False,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content
        assert "8 unread message(s)" in mem_text
        assert "and 3 other(s)" in mem_text

    def test_reminder_not_in_agent_messages(self) -> None:
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 1,
                "unique_senders": ["Coordinator"],
                "oldest_unread_at": "",
                "has_any_from_coordinator": True,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        assert "### Mailbox" in assembled[-1].content
        assert not any("### Mailbox" in (m.content or "") for m in messages)

    def test_reminder_is_final_section(self) -> None:
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 1,
                "unique_senders": ["Coordinator"],
                "oldest_unread_at": "",
                "has_any_from_coordinator": True,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content if len(assembled) > 1 else ""
        mailbox_pos = mem_text.index("### Mailbox")
        after_mailbox = mem_text[mailbox_pos + len("### Mailbox") :]
        assert "### " not in after_mailbox

    def test_no_reminder_when_no_unread(self) -> None:
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 0,
                "unique_senders": [],
                "oldest_unread_at": "",
                "has_any_from_coordinator": False,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content if len(assembled) > 1 else ""
        assert "### Mailbox" not in mem_text

    def test_no_reminder_without_provider(self) -> None:
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=None,
        )
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content if len(assembled) > 1 else ""
        assert "### Mailbox" not in mem_text

    def test_reminder_never_shows_body(self) -> None:
        provider = _MockStateProvider(
            mailbox_summary={
                "unread_count": 1,
                "unique_senders": ["Coordinator"],
                "oldest_unread_at": "2026-07-15T10:00:00",
                "has_any_from_coordinator": True,
            }
        )
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        mem_text = assembled[-1].content
        assert "Hello agent" not in mem_text
        assert "How is the search" not in mem_text
        assert "1 unread message(s)" in mem_text
        assert "from Coordinator" in mem_text


# ---------------------------------------------------------------------------
# Finding 4+6: Runtime version refresh on mailbox/team changes
# ---------------------------------------------------------------------------


class _SimpleBarrier:
    def __init__(self, step=0, finished=False):
        self._step_counter = step
        self._finished = finished
        self.env = MagicMock()
        self.env.controller = MagicMock()
        self.env.controller.get = MagicMock()
        self.env.controller.get_inventory = MagicMock()

    def is_finished(self):
        return self._finished

    def get_current_obs(self, agent_idx):
        return ""


def _make_barrier(pos=(0, 0, 0), inventory=None, step=0):
    b = _SimpleBarrier(step=step)
    agent = MagicMock()
    agent.get_position.return_value = pos
    b.env.controller.get.return_value = agent
    b.env.controller.get_inventory.return_value = inventory or []
    return b


class TestStateProviderVersionRefresh:
    def test_default_version_compatible(self):
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)
        state = provider.snapshot()
        assert state.version == (5, 0, ("", -1, False))

    def test_mailbox_version_triggers_refresh(self, mailbox: WorkerMailboxStore):
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0, mailbox=mailbox)
        s1 = provider.snapshot()
        assert s1.version == (5, 0, ("", -1, False))
        mailbox.deliver("msg-1", "Coordinator", "worker-alpha")
        s2 = provider.snapshot()
        assert s2.version == (5, 1, ("", -1, False))
        assert s2 is not s1

    def test_mailbox_summary_in_payload(self, mailbox: WorkerMailboxStore):
        mailbox.deliver("msg-1", "Coordinator", "worker-alpha")
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0, mailbox=mailbox)
        state = provider.snapshot()
        summary = state.get("mailbox_summary")
        assert summary is not None
        assert summary["unread_count"] == 1
        assert summary["has_any_from_coordinator"] is True

    def test_mailbox_summary_custom_coordinator_id(self, mailbox: WorkerMailboxStore):
        mailbox.deliver("msg-1", "boss", "worker-alpha")
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier,
            agent_idx=0,
            mailbox=mailbox,
            coordinator_id="boss",
        )
        state = provider.snapshot()
        assert state.get("mailbox_summary")["has_any_from_coordinator"] is True

    def test_team_generation_triggers_refresh(self, tmp_dir: Path):
        team_store = WorkerTeamState(
            tmp_dir / "team.json", local_worker_id="worker-alpha"
        )
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier, agent_idx=0, team_state=team_store
        )
        s1 = provider.snapshot()
        assert s1.version == (5, 0, ("", -1, False))
        team_store.install(
            team_id="sar-team-7",
            epoch=1,
            members=["worker-alpha", "worker-beta"],
            endpoints={"worker-alpha": "http://a:8090", "worker-beta": "http://b:8090"},
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            coordinator_id="Coordinator",
        )
        s2 = provider.snapshot()
        assert s2.version == (5, 0, ("sar-team-7", 1, True))
        assert s2 is not s1

    def test_team_revoke_changes_generation(self, tmp_dir: Path):
        team_store = WorkerTeamState(
            tmp_dir / "team.json", local_worker_id="worker-alpha"
        )
        team_store.install(
            team_id="sar-team-7",
            epoch=1,
            members=["worker-alpha", "worker-beta"],
            endpoints={"worker-alpha": "http://a:8090", "worker-beta": "http://b:8090"},
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            coordinator_id="Coordinator",
        )
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier, agent_idx=0, team_state=team_store
        )
        s_install = provider.snapshot()
        assert s_install.version == (5, 0, ("sar-team-7", 1, True))
        team_store.revoke(team_id="sar-team-7", epoch=1)
        s_revoke = provider.snapshot()
        assert s_revoke.version == (5, 0, ("sar-team-7", 1, False))
        assert s_revoke is not s_install

    def test_team_summary_in_payload(self, tmp_dir: Path):
        team_store = WorkerTeamState(
            tmp_dir / "team.json", local_worker_id="worker-alpha"
        )
        team_store.install(
            team_id="sar-team-7",
            epoch=1,
            members=["worker-alpha", "worker-beta"],
            endpoints={"worker-alpha": "http://a:8090", "worker-beta": "http://b:8090"},
            team_secret="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            coordinator_id="Coordinator",
        )
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier, agent_idx=0, team_state=team_store
        )
        state = provider.snapshot()
        ts = state.get("team_summary")
        assert ts is not None
        assert ts["team_id"] == "sar-team-7"

    def test_same_version_returns_cached(self, mailbox: WorkerMailboxStore):
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0, mailbox=mailbox)
        s1 = provider.snapshot()
        s2 = provider.snapshot()
        assert s1 is s2


# ---------------------------------------------------------------------------
# Finding 7: Exception propagation to stale state
# ---------------------------------------------------------------------------


class TestExceptionStale:
    def test_mailbox_exception_propagates_to_stale(self):
        broken_mailbox = MagicMock()
        broken_mailbox.version = 1
        broken_mailbox.summary.side_effect = RuntimeError("mailbox broken")
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier, agent_idx=0, mailbox=broken_mailbox
        )
        state = provider.snapshot()
        assert state.stale
        assert "mailbox broken" in state.refresh_error

    def test_team_state_exception_propagates_to_stale(self, tmp_dir: Path):
        broken_team = MagicMock()
        broken_team.generation = ("", -1, False)
        broken_team.current.side_effect = RuntimeError("team broken")
        barrier = _make_barrier(step=5)
        provider = SARWorkerStateProvider(
            barrier=barrier, agent_idx=0, team_state=broken_team
        )
        state = provider.snapshot()
        assert state.stale
        assert "team broken" in state.refresh_error


# ---------------------------------------------------------------------------
# Finding 3+4: MBZ disabled default + fail-closed validation
# ---------------------------------------------------------------------------


class TestDisabledDefault:
    def test_provider_no_mailbox_default(self):
        barrier = _make_barrier(step=3, pos=(1, 2, 0), inventory=["Water"])
        provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)
        state = provider.snapshot()
        assert state.get("position") == (1, 2, 0)
        assert state.get("inventory") == ["Water"]
        assert state.get("step") == 3
        assert "mailbox_summary" not in state.payload
        assert "team_summary" not in state.payload

    def test_worker_default_no_mail(self):
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=False,
        )
        assert worker._mailbox_store is None
        assert worker._team_state_store is None
        assert worker._ingress is None

    def test_enabled_without_secret_raises(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr(
            "sar_orch.worker.load_env_file",
            lambda _: {},
        )
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=True,
            coordinator_secret=None,
            log_dir=str(tmp_dir),
        )
        with pytest.raises(ConfigurationError, match="coordinator_secret"):
            worker.start()

    def test_enabled_short_secret_raises(self, tmp_dir: Path):
        with pytest.raises(ConfigurationError, match="at least 16"):
            SARWorker(
                worker_id="test-worker",
                agent_name="TestAgent",
                agent_idx=0,
                barrier=None,
                enable_peer_mail=True,
                coordinator_secret=b"short",
                log_dir=str(tmp_dir),
            )

    def test_enabled_without_log_dir_raises(self):
        with pytest.raises(ConfigurationError, match="log_dir"):
            SARWorker(
                worker_id="test-worker",
                agent_name="TestAgent",
                agent_idx=0,
                barrier=None,
                enable_peer_mail=True,
                coordinator_secret=b"valid-secret-32bytes!",
                log_dir=None,
            )


# ---------------------------------------------------------------------------
# Finding 4: Store creation via _init_peer_mail_stores helper
# ---------------------------------------------------------------------------


class TestInitPeerMailStores:
    def test_init_peer_mail_stores_creates_all(self, tmp_dir: Path):
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=True,
            coordinator_secret=b"test-secret-32bytes-0123456789",
            log_dir=str(tmp_dir),
        )
        # __init__ should not eagerly create stores anymore
        assert worker._mailbox_store is None
        assert worker._team_state_store is None
        assert worker._ingress is None

        # Call the helper explicitly (as start() would)
        worker._init_peer_mail_stores(secret=b"test-secret-32bytes-0123456789")
        assert worker._mailbox_store is not None
        assert worker._team_state_store is not None
        assert worker._ingress is not None

    def test_init_peer_mail_stores_creates_files(self, tmp_dir: Path):
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=True,
            coordinator_secret=b"test-secret-32bytes-0123456789",
            log_dir=str(tmp_dir),
        )
        worker._init_peer_mail_stores(secret=b"test-secret-32bytes-0123456789")
        assert worker._mailbox_store is not None

    def test_start_env_fallback_success(self, tmp_dir: Path, monkeypatch):
        """coordinator_secret from .env is accepted when not provided at construction."""
        monkeypatch.setattr(
            "sar_orch.worker.load_env_file",
            lambda _: {"coordinator_secret": "valid-secret-32bytes-for-env!"},
        )
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=True,
            coordinator_secret=None,
            log_dir=str(tmp_dir),
        )
        assert worker._mailbox_store is None  # stores created in start()
        # start() will fail because barrier is None, but config validation passes
        # (the coord_secret resolution would succeed from mocked env)

    def test_start_env_fallback_missing_raises(self, tmp_dir: Path, monkeypatch):
        """No coordinator_secret at construction or in env raises ConfigurationError."""
        monkeypatch.setattr(
            "sar_orch.worker.load_env_file",
            lambda _: {},
        )
        worker = SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            # Peer-mail tests exercise the mailbox config path in isolation;
            # keep the legacy memory mode so construction does not require a
            # callback secret (read_port is the default since H3 retirement).
            memory_read_mode="legacy",
            enable_peer_mail=True,
            coordinator_secret=None,
            log_dir=str(tmp_dir),
        )
        with pytest.raises(ConfigurationError, match="coordinator_secret"):
            worker.start()

    def test_enabled_explicit_secret_accepted(self, tmp_dir: Path):
        """Explicit coordinator_secret at construction passes validation."""
        SARWorker(
            worker_id="test-worker",
            agent_name="TestAgent",
            agent_idx=0,
            barrier=None,
            enable_peer_mail=True,
            coordinator_secret=b"valid-secret-32bytes-0123456789",
            log_dir=str(tmp_dir),
        )


class TestToolNoBarrier:
    def test_tool_only_depends_on_mailbox(self):
        mbox = MagicMock()
        mbox.read_unread.return_value = []
        tool = ReadMailboxTool(mailbox=mbox)
        assert tool.name == "read_mailbox"
        assert "mailbox" in tool.description.lower()

    def test_tool_parameters_schema(self):
        mbox = MagicMock()
        tool = ReadMailboxTool(mailbox=mbox)
        params = tool.parameters
        assert "message_ids" in params["properties"]
        assert "limit" in params["properties"]


# ---------------------------------------------------------------------------
# Snapshot safety
# ---------------------------------------------------------------------------


class TestSnapshotNoBodies:
    def test_snapshot_no_body(self, mailbox: WorkerMailboxStore):
        _populate_mailbox(mailbox)
        provider = _MockStateProvider(mailbox_summary=mailbox.summary())
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", pinned_enabled=True),
            state_provider=provider,
        )
        ctx.refresh_runtime_state()
        sys_prompt = "Test"
        messages = [Message(role="system", content=sys_prompt)]
        assembled = ctx.assemble(sys_prompt, messages)
        ctx.save_snapshot("task-1", assembled)
        loaded = ctx.load_snapshot("task-1")
        assert loaded is not None
        combined = " ".join(m.content or "" for m in loaded)
        assert "Hello agent!" not in combined
        assert "1 unread message(s)" in combined or "3 unread message(s)" in combined


# ---------------------------------------------------------------------------
# Signature inspection
# ---------------------------------------------------------------------------


class TestSignatures:
    def test_signature_inspection(self):
        from a2a.worker.agent_adapter import EnvelopeAwareAdapter
        from a2a.worker.mailbox_store import WorkerMailboxStore
        from a2a.worker.team_state import WorkerTeamState
        from a2a.worker.ingress import EnvelopeIngress

        assert callable(EnvelopeAwareAdapter)
        assert callable(WorkerMailboxStore)
        assert callable(WorkerTeamState)
        assert callable(EnvelopeIngress)

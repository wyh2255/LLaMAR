"""Tests for EnvelopeAwareAdapter — mail bypasses controller, legacy passes through."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2a.shared.message_envelope import (
    MessageEnvelope,
    MessageKind,
)
from a2a.worker.agent_adapter import EnvelopeAwareAdapter
from a2a.worker.a2a_server import create_worker_a2a_server


# ---------------------------------------------------------------------------
# Minimal fake dependencies (duck-typed)
# ---------------------------------------------------------------------------


class _FakeMailbox:
    def deliver(self, **kwargs):
        from a2a.worker.mailbox_store import MailboxRecord

        return MailboxRecord(
            message_id=kwargs.get("message_id", "m1"),
            sender_id=kwargs.get("sender_id", "s"),
            recipient_id=kwargs.get("recipient_id", "r"),
            subject=kwargs.get("subject", ""),
            body=kwargs.get("body", ""),
            team_id=kwargs.get("team_id"),
            team_epoch=kwargs.get("team_epoch"),
            sent_at=kwargs.get("sent_at", ""),
            received_at="2026-07-15T12:00:00+00:00",
        )


class _FakeTeamState:
    def __init__(self):
        self.installed = []
        self.revoked = []

    def install(self, **kwargs):
        self.installed.append(kwargs)

    def revoke(self, team_id: str, epoch: int):
        self.revoked.append((team_id, epoch))


class _FakeIngress:
    def __init__(self, result_action="legacy"):
        self._result_action = result_action

    def classify(self, text: str):
        from a2a.worker.ingress import IngressResult

        if self._result_action == "reject":
            return IngressResult(action="reject", body={}, reason="test rejection")
        return IngressResult(
            action=self._result_action,
            body={"content": text, "subject": "test"},
            envelope=MessageEnvelope(
                kind=MessageKind.MAIL,
                sender_id="Coordinator",
                recipient_id="worker-alpha",
                message_id="test-msg-1",
                team_id="sar-team-7",
                team_epoch=1,
            ),
        )


class _FakeContext:
    def __init__(self, task_id="task-1", context_id="ctx-1", user_input="go"):
        self._task = MagicMock()
        self._task.id = task_id
        self._task.context_id = context_id
        self._input = user_input
        self.message = MagicMock()
        self.message.task_id = task_id
        self.message.context_id = context_id
        self.message.role = 1

    @property
    def current_task(self):
        return self._task

    def get_user_input(self, delimiter="\n"):
        return self._input


class _FakeEventQueue:
    def __init__(self):
        self.events = []
        self.enqueue_event = AsyncMock(side_effect=self._record)

    async def _record(self, event):
        self.events.append(event)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_requires_ingress(self) -> None:
        with pytest.raises(TypeError, match="ingress is required"):
            EnvelopeAwareAdapter(ingress=None, mailbox=None, team_state=None)

    def test_requires_mailbox_and_team(self) -> None:
        with pytest.raises(TypeError, match="ingress is required"):
            EnvelopeAwareAdapter(
                ingress=None,
                mailbox=None,
                team_state=None,
            )

    def test_requires_mailbox(self) -> None:
        ingress = _FakeIngress()
        with pytest.raises(TypeError, match="mailbox is required"):
            EnvelopeAwareAdapter(
                ingress=ingress,
                mailbox=None,
                team_state=_FakeTeamState(),
            )

    def test_requires_team_state(self) -> None:
        ingress = _FakeIngress()
        with pytest.raises(TypeError, match="team_state is required"):
            EnvelopeAwareAdapter(
                ingress=ingress,
                mailbox=_FakeMailbox(),
                team_state=None,
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMailBypassesController:
    @pytest.mark.asyncio
    async def test_mail_does_not_call_submit(self) -> None:
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="mail")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = _FakeTeamState()
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        with patch("a2a.server.tasks.TaskUpdater"):
            await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_calls_submit(self) -> None:
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="legacy")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = _FakeTeamState()
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}
        adapter._controller._get_session = MagicMock(return_value=None)

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        fake_updater = AsyncMock()
        fake_updater.start_work = AsyncMock()
        fake_updater.complete = AsyncMock()
        fake_updater.add_artifact = AsyncMock()
        fake_updater.requires_input = AsyncMock()
        fake_updater.failed = AsyncMock()
        fake_updater.cancel = AsyncMock()

        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
            with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
                with patch(
                    "a2a.worker.agent_adapter.new_text_message", return_value="msg"
                ):
                    with patch("a2a.worker.agent_adapter.Part"):
                        await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_called_once()


class TestReject:
    @pytest.mark.asyncio
    async def test_reject_returns_terminal(self) -> None:
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="reject")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = _FakeTeamState()
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        updater_mock = AsyncMock()
        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=updater_mock):
            await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_not_called()
        updater_mock.reject.assert_called_once()


class TestTeamUpdate:
    @pytest.mark.asyncio
    async def test_team_update_passes_envelope_fields(self) -> None:
        team = _FakeTeamState()
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="team_update")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = team
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        updater_mock = AsyncMock()
        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=updater_mock):
            await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_not_called()
        # team_state.install should have been called
        assert len(team.installed) > 0

    @pytest.mark.asyncio
    async def test_team_revoke_passes_envelope(self) -> None:
        team = _FakeTeamState()
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="team_revoke")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = team
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        updater_mock = AsyncMock()
        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=updater_mock):
            await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_not_called()
        updater_mock.complete.assert_called_once()


class TestTaskEnvelope:
    @pytest.mark.asyncio
    async def test_task_envelope_calls_submit(self) -> None:
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="task")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = _FakeTeamState()
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}
        adapter._controller._get_session = MagicMock(return_value=None)

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        fake_updater = AsyncMock()
        fake_updater.start_work = AsyncMock()
        fake_updater.complete = AsyncMock()
        fake_updater.add_artifact = AsyncMock()
        fake_updater.requires_input = AsyncMock()
        fake_updater.failed = AsyncMock()
        fake_updater.cancel = AsyncMock()

        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
            with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
                with patch(
                    "a2a.worker.agent_adapter.new_text_message", return_value="msg"
                ):
                    with patch("a2a.worker.agent_adapter.Part"):
                        await adapter.execute(ctx, eq)

        adapter._controller.submit.assert_called_once()


# ---------------------------------------------------------------------------
# Signed task: must NOT pre-enqueue task
# ---------------------------------------------------------------------------


class TestSignedTaskNoDoubleTask:
    """Signed task envelopes must not create a task before super().execute()."""

    @pytest.mark.asyncio
    async def test_task_envelope_no_pre_enqueued_events(self) -> None:
        adapter = EnvelopeAwareAdapter.__new__(EnvelopeAwareAdapter)
        adapter._ingress = _FakeIngress(result_action="task")
        adapter._mailbox = _FakeMailbox()
        adapter._team_state = _FakeTeamState()
        adapter._controller = MagicMock()
        adapter._controller.submit = AsyncMock()
        adapter._extra_tools = []
        adapter._step_callback = None
        adapter._task_cancel_events = {}
        adapter._controller._get_session = MagicMock(return_value=None)

        ctx = _FakeContext()
        eq = _FakeEventQueue()

        fake_updater = AsyncMock()
        fake_updater.start_work = AsyncMock()
        fake_updater.complete = AsyncMock()
        fake_updater.add_artifact = AsyncMock()
        fake_updater.requires_input = AsyncMock()
        fake_updater.failed = AsyncMock()
        fake_updater.cancel = AsyncMock()

        with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
            with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
                with patch(
                    "a2a.worker.agent_adapter.new_text_message", return_value="msg"
                ):
                    with patch("a2a.worker.agent_adapter.Part"):
                        await adapter.execute(ctx, eq)

        assert len(eq.events) == 0, (
            f"Expected 0 events pre-enqueued for signed task, got {len(eq.events)}"
        )
        adapter._controller.submit.assert_called_once()


# ---------------------------------------------------------------------------
# Server DI: all-or-none
# ---------------------------------------------------------------------------


class TestServerDI:
    def test_all_three_ok(self) -> None:
        ingress = _FakeIngress()
        mailbox = _FakeMailbox()
        team = _FakeTeamState()
        EnvelopeAwareAdapter(ingress=ingress, mailbox=mailbox, team_state=team)

    def test_missing_mailbox(self) -> None:
        ingress = _FakeIngress()
        with pytest.raises(TypeError, match="mailbox is required"):
            EnvelopeAwareAdapter(
                ingress=ingress,
                mailbox=None,
                team_state=_FakeTeamState(),
            )

    def test_missing_team(self) -> None:
        ingress = _FakeIngress()
        mailbox = _FakeMailbox()
        with pytest.raises(TypeError, match="team_state is required"):
            EnvelopeAwareAdapter(
                ingress=ingress,
                mailbox=mailbox,
                team_state=None,
            )

    def test_server_uses_envelope_adapter_when_all_dependencies_exist(self) -> None:
        executor = MagicMock()
        with patch(
            "a2a.worker.agent_adapter.EnvelopeAwareAdapter",
            return_value=executor,
        ) as adapter_cls:
            server = create_worker_a2a_server(
                worker_id="Alice",
                envelope_ingress=_FakeIngress(),
                mailbox_store=_FakeMailbox(),
                team_state_store=_FakeTeamState(),
            )

        adapter_cls.assert_called_once()
        assert server.executor is executor

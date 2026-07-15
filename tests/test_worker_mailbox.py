"""Tests for WorkerMailboxStore — persistence, idempotency, retention, concurrency."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from a2a.worker.mailbox_store import (
    WorkerMailboxStore,
    MailboxError,
    RecipientMismatchError,
    SubjectTooLongError,
    BodyTooLongError,
    DuplicateMessageIdError,
    InvalidMailboxParameter,
    MAX_SUBJECT_LENGTH,
    MAX_BODY_LENGTH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mailbox_file(tmp_path: Path) -> Path:
    return tmp_path / "mailbox.ndjson"


@pytest.fixture
def store(mailbox_file: Path) -> WorkerMailboxStore:
    return WorkerMailboxStore(mailbox_file, local_worker_id="worker-alpha")


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestInit:
    def test_rejects_zero_max_records(self, mailbox_file: Path) -> None:
        with pytest.raises(InvalidMailboxParameter, match="max_records"):
            WorkerMailboxStore(mailbox_file, "w", max_records=0)

    def test_rejects_negative_max_records(self, mailbox_file: Path) -> None:
        with pytest.raises(InvalidMailboxParameter, match="max_records"):
            WorkerMailboxStore(mailbox_file, "w", max_records=-1)


# ---------------------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------------------


class TestDeliver:
    def test_deliver_creates_record(self, store: WorkerMailboxStore) -> None:
        r = store.deliver("msg-1", "alice", "worker-alpha", subject="hi", body="hello")
        assert r.message_id == "msg-1"
        assert r.sender_id == "alice"
        assert r.recipient_id == "worker-alpha"
        assert r.subject == "hi"
        assert r.body == "hello"
        assert r.read_at is None
        assert r.received_at != ""

    def test_deliver_returns_defensive_copy(self, store: WorkerMailboxStore) -> None:
        r1 = store.deliver("msg-1", "a", "worker-alpha", subject="hi")
        r2 = store.deliver("msg-1", "a", "worker-alpha", subject="hi")
        # Same content → idempotent, copies are equal but not same object
        assert r1.message_id == r2.message_id
        assert r1 is not r2

    def test_deliver_idempotent_same_content(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "alice", "worker-alpha", subject="hi", body="hello")
        r2 = store.deliver("msg-1", "alice", "worker-alpha", subject="hi", body="hello")
        assert r2.subject == "hi"
        assert r2.body == "hello"

    def test_deliver_conflicting_content_raises(
        self, store: WorkerMailboxStore
    ) -> None:
        store.deliver("msg-1", "alice", "worker-alpha", subject="hi", body="hello")
        with pytest.raises(DuplicateMessageIdError, match="different content"):
            store.deliver("msg-1", "bob", "worker-alpha", subject="bye", body="world")

    def test_recipient_mismatch(self, store: WorkerMailboxStore) -> None:
        with pytest.raises(RecipientMismatchError, match="worker-alpha"):
            store.deliver("msg-1", "alice", "other-worker")

    def test_subject_too_long(self, store: WorkerMailboxStore) -> None:
        with pytest.raises(SubjectTooLongError):
            store.deliver(
                "msg-1", "alice", "worker-alpha", subject="x" * (MAX_SUBJECT_LENGTH + 1)
            )

    def test_body_too_long(self, store: WorkerMailboxStore) -> None:
        with pytest.raises(BodyTooLongError):
            store.deliver(
                "msg-1", "alice", "worker-alpha", body="x" * (MAX_BODY_LENGTH + 1)
            )

    def test_validates_nonblank_ids(self, store: WorkerMailboxStore) -> None:
        with pytest.raises(InvalidMailboxParameter, match="message_id"):
            store.deliver("", "a", "worker-alpha")
        with pytest.raises(InvalidMailboxParameter, match="sender_id"):
            store.deliver("m1", "", "worker-alpha")
        with pytest.raises(InvalidMailboxParameter, match="recipient_id"):
            store.deliver("m1", "a", "")

    def test_validates_team_coherence(self, store: WorkerMailboxStore) -> None:
        with pytest.raises(
            InvalidMailboxParameter, match="team_epoch requires team_id"
        ):
            store.deliver("m1", "a", "worker-alpha", team_epoch=1)
        with pytest.raises(
            InvalidMailboxParameter, match="team_id requires team_epoch"
        ):
            store.deliver("m1", "a", "worker-alpha", team_id="t")
        with pytest.raises(InvalidMailboxParameter, match=">= 0"):
            store.deliver("m1", "a", "worker-alpha", team_id="t", team_epoch=-1)


class TestListUnread:
    def test_all_unread(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha", subject="s1")
        store.deliver("msg-2", "b", "worker-alpha", subject="s2")
        unread = store.list_unread()
        assert len(unread) == 2

    def test_after_mark_read(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        store.mark_read("msg-1")
        assert len(store.list_unread()) == 0

    def test_newest_first(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        store.deliver("msg-2", "b", "worker-alpha")
        unread = store.list_unread()
        assert unread[0].message_id == "msg-2"


class TestMarkRead:
    def test_mark_read(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        r = store.mark_read("msg-1")
        assert r is not None
        assert r.read_at is not None

    def test_mark_read_unknown(self, store: WorkerMailboxStore) -> None:
        assert store.mark_read("no-such-msg") is None

    def test_mark_read_idempotent(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        r1 = store.mark_read("msg-1")
        r2 = store.mark_read("msg-1")
        assert r2 is not None
        assert r2.read_at == r1.read_at

    def test_mark_read_batch(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        store.deliver("msg-2", "b", "worker-alpha")
        results = store.mark_read_batch(["msg-1", "msg-2", "no-such"])
        assert len(results) == 3
        assert results[0] is not None and results[0].read_at is not None
        assert results[1] is not None and results[1].read_at is not None
        assert results[2] is None


class TestGet:
    def test_get_existing(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        assert store.get("msg-1") is not None

    def test_get_missing(self, store: WorkerMailboxStore) -> None:
        assert store.get("no-such") is None

    def test_get_defensive_copy(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        r1 = store.get("msg-1")
        r2 = store.get("msg-1")
        assert r1 is not None and r2 is not None
        assert r1 is not r2
        assert r1.message_id == r2.message_id


# ---------------------------------------------------------------------------
# Version monotonicity
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_increments_on_deliver(self, store: WorkerMailboxStore) -> None:
        v0 = store.version
        store.deliver("msg-1", "a", "worker-alpha")
        assert store.version == v0 + 1

    def test_version_increments_on_mark_read(self, store: WorkerMailboxStore) -> None:
        store.deliver("msg-1", "a", "worker-alpha")
        v1 = store.version
        store.mark_read("msg-1")
        assert store.version == v1 + 1

    def test_version_locked_property(self, store: WorkerMailboxStore) -> None:
        v = store.version
        assert isinstance(v, int)


# ---------------------------------------------------------------------------
# Persistence: replay, event order, tombstones
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_replay(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha", subject="hello")
        s1.deliver("msg-2", "b", "worker-alpha", subject="world")

        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        assert len(s2) == 2
        assert s2.get("msg-1") is not None
        assert s2.get("msg-2") is not None

    def test_replay_with_read_state(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha")
        s1.mark_read("msg-1")

        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        r = s2.get("msg-1")
        assert r is not None
        assert r.read_at is not None

    def test_replay_correct_version(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha")
        s1.mark_read("msg-1")
        v1 = s1.version

        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        assert s2.version == v1

    def test_replay_event_order(self, mailbox_file: Path) -> None:
        """Replay processes events in order, later events override earlier."""
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha", subject="v1")
        s1.deliver("msg-1", "a", "worker-alpha", subject="v1")  # idempotent
        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        assert s2.get("msg-1") is not None

    def test_tombstone_persistence(self, mailbox_file: Path) -> None:
        """Trimmed records stay gone after restart."""
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha", max_records=2)
        s1.deliver("msg-1", "a", "worker-alpha")
        s1.mark_read("msg-1")
        s1.deliver("msg-2", "b", "worker-alpha")
        s1.deliver("msg-3", "c", "worker-alpha")  # triggers trim of msg-1

        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha", max_records=2)
        # msg-1 was tombstoned, should NOT be resurrected
        assert s2.get("msg-1") is None
        assert s2.get("msg-2") is not None
        assert s2.get("msg-3") is not None

    def test_truncated_final_line_skipped(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha")
        with open(mailbox_file, "a") as f:
            f.write('{"v": 99, "e": "deliver", "record": {"message_id": "msg-2"')
        s2 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        assert len(s2) == 1
        assert s2.get("msg-1") is not None

    def test_malformed_middle_line_raises(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha")
        with open(mailbox_file, "a") as f:
            f.write("{invalid json}\n")
        with open(mailbox_file, "a") as f:
            f.write(
                json.dumps(
                    {
                        "v": 99,
                        "e": "deliver",
                        "record": {
                            "message_id": "msg-3",
                            "sender_id": "c",
                            "recipient_id": "worker-alpha",
                            "subject": "",
                            "body": "",
                            "received_at": "",
                        },
                    }
                )
                + "\n"
            )
        with pytest.raises(MailboxError, match="Malformed NDJSON"):
            WorkerMailboxStore(mailbox_file, "worker-alpha")

    def test_read_event_without_timestamp_raises(self, mailbox_file: Path) -> None:
        s1 = WorkerMailboxStore(mailbox_file, "worker-alpha")
        s1.deliver("msg-1", "a", "worker-alpha")
        with open(mailbox_file, "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"v": 2, "e": "read", "message_id": "msg-1"})
                + "\n"
            )

        with pytest.raises(MailboxError, match="missing read_at"):
            WorkerMailboxStore(mailbox_file, "worker-alpha")


# ---------------------------------------------------------------------------
# Bounded retention
# ---------------------------------------------------------------------------


class TestRetention:
    def test_never_evicts_unread(self, mailbox_file: Path) -> None:
        store = WorkerMailboxStore(mailbox_file, "worker-alpha", max_records=5)
        for i in range(10):
            store.deliver(f"msg-{i}", "a", "worker-alpha", subject=str(i))
        assert len(store) == 10

    def test_trims_oldest_read(self, mailbox_file: Path) -> None:
        store = WorkerMailboxStore(mailbox_file, "worker-alpha", max_records=5)
        for i in range(6):
            store.deliver(f"msg-{i}", "a", "worker-alpha", subject=str(i))
        for i in range(3):
            store.mark_read(f"msg-{i}")
        store.deliver("msg-new", "a", "worker-alpha")
        assert store.get("msg-0") is None
        assert store.get("msg-1") is None
        for mid in ("msg-2", "msg-3", "msg-4", "msg-5", "msg-new"):
            assert store.get(mid) is not None, f"{mid} should survive"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_deliver(self, mailbox_file: Path) -> None:
        store = WorkerMailboxStore(mailbox_file, "worker-alpha")
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def worker(idx: int) -> None:
            barrier.wait()
            try:
                store.deliver(f"msg-{idx}", "a", "worker-alpha", subject=str(idx))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(store) == 10

    def test_concurrent_deliver_same_id(self, mailbox_file: Path) -> None:
        store = WorkerMailboxStore(mailbox_file, "worker-alpha")
        barrier = threading.Barrier(10)
        results: list[str] = []
        rlock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            r = store.deliver("same-id", "a", "worker-alpha")
            with rlock:
                results.append(r.message_id)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 10
        assert len(store) == 1

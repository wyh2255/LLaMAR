"""Worker-local persistent mailbox with NDJSON event-log persistence.

Thread-safe store for peer-to-peer mail messages.  Uses an event-sourced
NDJSON append log (deliver / read / tombstone events) so that every state
change is durable and replay correctly restores records, read state,
deletions, and the maximum version.

Trimming writes tombstone events — trimmed records are never resurrected
after restart.  The event log is append-only; long-running instances may
accumulate tombstones over time.

Domain record fields: message_id, sender_id, recipient_id, optional
team_id/team_epoch, subject, body, sent_at, received_at, read_at.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import copy as _shallow_copy
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_SUBJECT_LENGTH = 256
MAX_BODY_LENGTH = 65536
DEFAULT_RETENTION_MAX_RECORDS = 10_000
MAX_SUMMARY_SENDERS = 20

_EVENT_DELIVER = "deliver"
_EVENT_READ = "read"
_EVENT_TOMBSTONE = "tombstone"
_KNOWN_EVENT_TYPES = {_EVENT_DELIVER, _EVENT_READ, _EVENT_TOMBSTONE}


class MailboxError(Exception):
    """Base for mailbox errors."""


class RecipientMismatchError(MailboxError):
    """Record recipient does not match local worker."""


class BodyTooLongError(MailboxError):
    """Body exceeds maximum allowed length."""


class SubjectTooLongError(MailboxError):
    """Subject exceeds maximum allowed length."""


class DuplicateMessageIdError(MailboxError):
    """Same message_id delivered with conflicting content."""


class InvalidMailboxParameter(MailboxError):
    """Invalid parameter value."""


@dataclass
class MailboxRecord:
    """A delivered mail record in the worker's mailbox."""

    message_id: str
    sender_id: str
    recipient_id: str
    subject: str
    body: str
    team_id: str | None = None
    team_epoch: int | None = None
    sent_at: str = ""
    received_at: str = ""
    read_at: str | None = None
    _version: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_version", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MailboxRecord:
        d.pop("_version", None)
        return cls(**d)


class WorkerMailboxStore:
    """Thread-safe persistent mailbox with event-sourced NDJSON log.

    Every state change generates an event (deliver / read / tombstone)
    with a monotonically increasing version number.  On replay, events
    are processed in order to reconstruct records, read state, deletions,
    and the maximum version.

    Trimming (bounded retention) writes tombstone events so trimmed
    records are never resurrected after restart.

    All returned records are defensive (shallow) copies so callers cannot
    mutate store state outside the lock.
    """

    MailboxEventCallback = Callable[[str, dict[str, Any]], None]
    """Callback: ``(event_name, event_data)``.

    Event names: ``mail_delivered``, ``mail_read``.
    Event data excludes body content, secrets, and signatures.
    """

    def __init__(
        self,
        path: str | Path,
        local_worker_id: str,
        max_records: int = DEFAULT_RETENTION_MAX_RECORDS,
        event_callback: "MailboxEventCallback | None" = None,
    ):
        if max_records <= 0:
            raise InvalidMailboxParameter("max_records must be > 0")
        self._path = Path(path)
        self._local_worker_id = local_worker_id
        self._max_records = max_records
        self._lock = threading.Lock()
        self._records: dict[str, MailboxRecord] = {}
        self._version = 0
        self._event_callback = event_callback
        if self._path.exists():
            self._replay()

    # ── public API ──────────────────────────────────────────────────────

    def deliver(
        self,
        message_id: str,
        sender_id: str,
        recipient_id: str,
        *,
        subject: str = "",
        body: str = "",
        team_id: str | None = None,
        team_epoch: int | None = None,
        sent_at: str = "",
    ) -> MailboxRecord:
        self._validate_deliver_params(
            message_id,
            sender_id,
            recipient_id,
            self._local_worker_id,
            subject,
            body,
            team_id,
            team_epoch,
        )
        with self._lock:
            existing = self._records.get(message_id)
            if existing is not None:
                if _content_match(
                    existing,
                    message_id,
                    sender_id,
                    recipient_id,
                    subject,
                    body,
                    team_id,
                    team_epoch,
                ):
                    return _defensive_copy(existing)
                raise DuplicateMessageIdError(
                    f"message_id '{message_id}' already exists with different content"
                )

            self._version += 1
            record = MailboxRecord(
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                subject=subject,
                body=body,
                team_id=team_id,
                team_epoch=team_epoch,
                sent_at=sent_at,
                received_at=datetime.now(timezone.utc).isoformat(),
                _version=self._version,
            )
            self._append_event(_EVENT_DELIVER, record.to_dict())
            self._records[message_id] = record
            self._maybe_trim()
            result = _defensive_copy(record)

        # Event callback outside lock
        self._fire_event(
            "mail_delivered",
            {
                "message_id": result.message_id,
                "sender_id": result.sender_id,
                "recipient_id": result.recipient_id,
                "team_id": result.team_id,
                "team_epoch": result.team_epoch,
                "received_at": result.received_at,
            },
        )
        return result

    def list_unread(self) -> list[MailboxRecord]:
        """Return all records where read_at is None (newest first)."""
        with self._lock:
            result = [
                _defensive_copy(r) for r in self._records.values() if r.read_at is None
            ]
            result.sort(key=lambda r: r._version, reverse=True)
            return result

    def mark_read(self, message_id: str) -> MailboxRecord | None:
        return self.mark_read_batch([message_id])[0]

    def mark_read_batch(self, message_ids: list[str]) -> list[MailboxRecord | None]:
        """Atomically mark multiple records as read.

        Returns a list of updated records (or ``None`` for unknown IDs) in
        the same order as *message_ids*.
        """
        read_events: list[dict[str, Any]] = []
        with self._lock:
            results: list[MailboxRecord | None] = []
            for mid in message_ids:
                record = self._records.get(mid)
                if record is None:
                    results.append(None)
                    continue
                if record.read_at is None:
                    self._version += 1
                    record._version = self._version
                    read_at = datetime.now(timezone.utc).isoformat()
                    self._append_event(
                        _EVENT_READ,
                        {"message_id": mid, "read_at": read_at},
                    )
                    record.read_at = read_at
                    read_events.append(
                        {
                            "message_id": mid,
                            "sender_id": record.sender_id,
                            "recipient_id": self._local_worker_id,
                            "team_id": record.team_id,
                            "team_epoch": record.team_epoch,
                        }
                    )
                results.append(_defensive_copy(record))

        # Events outside lock
        for ev in read_events:
            self._fire_event("mail_read", ev)
        return results

    def get(self, message_id: str) -> MailboxRecord | None:
        """Lookup a record by message_id (defensive copy)."""
        with self._lock:
            r = self._records.get(message_id)
            return _defensive_copy(r) if r else None

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def read_unread(
        self,
        limit: int | None = None,
        message_ids: list[str] | None = None,
        *,
        oldest_first: bool = True,
    ) -> list[MailboxRecord]:
        """Atomically select currently unread messages, mark them read, and return defensive copies.

        All operations happen under a single lock so concurrent readers never
        see the same message returned twice globally.

        Args:
            limit: Maximum messages to return (1..100, or ``None`` for unlimited).
            message_ids: Optional explicit list of message IDs to read (in caller
                order, deduplicated preserving first occurrence). Only unread
                messages among the IDs are selected.
            oldest_first: When ``True`` (default), return oldest received first.
                Ignored when *message_ids* is provided — caller order is used.

        Returns:
            Defensive copies of the records that were atomically read-and-marked.
        """
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise InvalidMailboxParameter("limit must be an integer")
            if limit < 1 or limit > 100:
                raise InvalidMailboxParameter("limit must be 1..100")

        with self._lock:
            all_unread = [r for r in self._records.values() if r.read_at is None]

            if message_ids is not None:
                unread_by_id = {r.message_id: r for r in all_unread}
                seen: set[str] = set()
                selected: list[MailboxRecord] = []
                for mid in message_ids:
                    if mid not in seen and mid in unread_by_id:
                        seen.add(mid)
                        selected.append(unread_by_id[mid])
            else:
                selected = sorted(
                    all_unread,
                    key=lambda r: r.received_at if oldest_first else r._version,
                )

            if limit is not None:
                selected = selected[:limit]

            if not selected:
                return []

            now = datetime.now(timezone.utc).isoformat()
            read_events: list[dict[str, Any]] = []
            for r in selected:
                self._version += 1
                r._version = self._version
                self._append_event(
                    _EVENT_READ,
                    {"message_id": r.message_id, "read_at": now},
                )
                r.read_at = now
                read_events.append(
                    {
                        "message_id": r.message_id,
                        "sender_id": r.sender_id,
                        "recipient_id": self._local_worker_id,
                        "team_id": r.team_id,
                        "team_epoch": r.team_epoch,
                    }
                )

            result = [_defensive_copy(r) for r in selected]

        # Events outside lock
        for ev in read_events:
            self._fire_event("mail_read", ev)
        return result

    def summary(self, coordinator_id: str | None = None) -> dict[str, Any]:
        """Thread-safe unread message summary.

        Args:
            coordinator_id: When provided, ``has_any_from_coordinator`` checks
                against this sender ID instead of the hardcoded ``"Coordinator"``.

        Returns bounded dict with:
          - unread_count: int
          - unique_senders: list[str] (at most ``MAX_SUMMARY_SENDERS`` entries)
          - total_unique_senders: int (total before truncation)
          - senders_truncated: bool
          - oldest_unread_at: str (ISO timestamp, empty if none)
          - has_any_from_coordinator: bool
        """
        coord_id = coordinator_id or "Coordinator"
        with self._lock:
            unread = [r for r in self._records.values() if r.read_at is None]
            count = len(unread)
            senders = list(dict.fromkeys(r.sender_id for r in unread))
            total_senders = len(senders)
            truncated = total_senders > MAX_SUMMARY_SENDERS
            display_senders = senders[:MAX_SUMMARY_SENDERS]
            oldest = min((r.received_at for r in unread), default="")
            has_coord = any(r.sender_id == coord_id for r in unread)
            return {
                "unread_count": count,
                "unique_senders": display_senders,
                "total_unique_senders": total_senders,
                "senders_truncated": truncated,
                "oldest_unread_at": oldest,
                "has_any_from_coordinator": has_coord,
            }

    def clear(self) -> None:
        """Clear all records — **testing only** (no persistence impact)."""
        with self._lock:
            self._records.clear()
            self._version = 0

    # ── observability ─────────────────────────────────────────────────

    def _fire_event(self, name: str, data: dict[str, Any]) -> None:
        if self._event_callback is not None:
            try:
                self._event_callback(name, data)
            except Exception:
                logger.debug("Mailbox event callback failed", exc_info=True)

    # ── validation ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_deliver_params(
        message_id: str,
        sender_id: str,
        recipient_id: str,
        local_worker_id: str,
        subject: str,
        body: str,
        team_id: str | None,
        team_epoch: int | None,
    ) -> None:
        if not message_id or not message_id.strip():
            raise InvalidMailboxParameter("message_id must be non-blank")
        if not sender_id or not sender_id.strip():
            raise InvalidMailboxParameter("sender_id must be non-blank")
        if not recipient_id or not recipient_id.strip():
            raise InvalidMailboxParameter("recipient_id must be non-blank")
        if recipient_id != local_worker_id:
            raise RecipientMismatchError(
                f"Recipient '{recipient_id}' != local worker '{local_worker_id}'"
            )
        if len(subject) > MAX_SUBJECT_LENGTH:
            raise SubjectTooLongError(f"Subject {len(subject)} > {MAX_SUBJECT_LENGTH}")
        if len(body) > MAX_BODY_LENGTH:
            raise BodyTooLongError(f"Body {len(body)} > {MAX_BODY_LENGTH}")
        if team_epoch is not None and team_id is None:
            raise InvalidMailboxParameter("team_epoch requires team_id")
        if team_id is not None and team_epoch is None:
            raise InvalidMailboxParameter("team_id requires team_epoch")
        if team_epoch is not None and team_epoch < 0:
            raise InvalidMailboxParameter("team_epoch must be >= 0")

    # ── persistence ─────────────────────────────────────────────────────

    def _append_event(self, event_type: str, data: dict[str, Any]) -> None:
        event: dict[str, Any] = {"v": self._version, "e": event_type}
        if event_type == _EVENT_DELIVER:
            event["record"] = data
        elif event_type == _EVENT_READ:
            event["message_id"] = data["message_id"]
            if "read_at" in data:
                event["read_at"] = data["read_at"]
        elif event_type == _EVENT_TOMBSTONE:
            event["message_id"] = data["message_id"]
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _replay(self) -> None:
        """Replay NDJSON events in order with strict validation.

        Rejection rules:
        - Malformed JSON at the final line position is treated as a
          truncated write and silently skipped (crash-safety).
        - Malformed JSON at any other position raises ``MailboxError``.
        - Structurally complete but semantically invalid events (bad
          record data, unknown event type, non-strictly-increasing or
          non-positive version, blank message_id) also raise.
        """
        raw = self._path.read_text(encoding="utf-8")
        if not raw.strip():
            return
        lines = raw.splitlines()
        last_v = 0
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if i == len(lines) - 1:
                    logger.warning("Skipping truncated final event line %d", i + 1)
                    continue
                raise MailboxError(f"Malformed NDJSON line {i + 1}: {exc}") from exc

            v = event.get("v", 0)
            if not isinstance(v, int) or v <= 0:
                raise MailboxError(
                    f"Line {i + 1}: version must be a positive integer, got {v!r}"
                )
            if v <= last_v:
                raise MailboxError(
                    f"Line {i + 1}: version {v} is not strictly increasing "
                    f"(last was {last_v})"
                )
            last_v = v
            if v > self._version:
                self._version = v

            ev_type = event.get("e", "")
            if ev_type not in _KNOWN_EVENT_TYPES:
                raise MailboxError(f"Line {i + 1}: unknown event type {ev_type!r}")

            try:
                if ev_type == _EVENT_DELIVER:
                    record_data = event.get("record")
                    if not isinstance(record_data, dict):
                        raise MailboxError(
                            f"Line {i + 1}: deliver event missing 'record' dict"
                        )
                    record = MailboxRecord.from_dict(record_data)
                    self._validate_deliver_params(
                        record.message_id,
                        record.sender_id,
                        record.recipient_id,
                        self._local_worker_id,
                        record.subject,
                        record.body,
                        record.team_id,
                        record.team_epoch,
                    )
                    record._version = v
                    self._records[record.message_id] = record
                elif ev_type == _EVENT_READ:
                    mid = event.get("message_id", "")
                    if not mid or not mid.strip():
                        raise MailboxError(
                            f"Line {i + 1}: read event missing message_id"
                        )
                    read_at = event.get("read_at")
                    if not isinstance(read_at, str) or not read_at.strip():
                        raise MailboxError(f"Line {i + 1}: read event missing read_at")
                    try:
                        datetime.fromisoformat(read_at)
                    except ValueError as exc:
                        raise MailboxError(
                            f"Line {i + 1}: invalid read_at timestamp"
                        ) from exc
                    rec = self._records.get(mid)
                    if rec is not None:
                        rec.read_at = read_at
                        rec._version = v
                elif ev_type == _EVENT_TOMBSTONE:
                    mid = event.get("message_id", "")
                    if not mid or not mid.strip():
                        raise MailboxError(
                            f"Line {i + 1}: tombstone event missing message_id"
                        )
                    self._records.pop(mid, None)
            except (KeyError, TypeError, MailboxError):
                raise
            except Exception as exc:
                raise MailboxError(f"Bad event data on line {i + 1}: {exc}") from exc

    def _maybe_trim(self) -> None:
        """Trim oldest read records if over the limit.

        Writes tombstone events so trimmed records are never resurrected.
        """
        if len(self._records) <= self._max_records:
            return
        read_records = sorted(
            [r for r in self._records.values() if r.read_at is not None],
            key=lambda r: r._version,
        )
        to_evict = len(self._records) - self._max_records
        if to_evict <= 0:
            return
        evicted = 0
        for r in read_records[:to_evict]:
            self._version += 1
            self._append_event(_EVENT_TOMBSTONE, {"message_id": r.message_id})
            del self._records[r.message_id]
            evicted += 1
        if evicted:
            logger.info(
                "Trimmed %d old read records (count=%d)",
                evicted,
                len(self._records),
            )


# ── module-level helpers ───────────────────────────────────────────────


def _content_match(
    existing: MailboxRecord,
    message_id: str,
    sender_id: str,
    recipient_id: str,
    subject: str,
    body: str,
    team_id: str | None,
    team_epoch: int | None,
) -> bool:
    return (
        existing.message_id == message_id
        and existing.sender_id == sender_id
        and existing.recipient_id == recipient_id
        and existing.subject == subject
        and existing.body == body
        and existing.team_id == team_id
        and existing.team_epoch == team_epoch
    )


def _defensive_copy(record: MailboxRecord) -> MailboxRecord:
    return _shallow_copy(record)

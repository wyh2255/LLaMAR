"""Phase 0 memory scope lifecycle: fail-closed admission, epoch isolation,
and the durable closed-scope fence."""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryScopeV1,
    MemoryScopeValidationError,
)
from a2a.coordinator.memory.store import (
    MemoryStore,
    ScopeActivationStatus,
    scope_tuple_reuse,
)


def test_scope_tuple_is_unique_and_repeat_activation_returns_same_handle(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    first = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))
    assert first.status is ScopeActivationStatus.ACTIVE
    assert first.scope_id

    second = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))
    assert second.status is ScopeActivationStatus.ACTIVE
    assert second.scope_id == first.scope_id
    assert len(store.list_scopes()) == 1


def test_same_context_different_epoch_is_isolated(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    epoch_1 = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))
    epoch_2 = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 2))

    assert epoch_1.scope_id != epoch_2.scope_id
    assert store.get_scope(epoch_1.scope_id) is not None
    assert store.get_scope(epoch_2.scope_id) is not None
    assert len(store.list_scopes()) == 2


def test_closed_scope_cannot_be_reopened(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))

    assert store.close_scope(scope.scope_id) is True

    reopened = store.activate_scope(MemoryScopeV1("llamar", "run-1", "ctx-1", 1))
    assert reopened.status is ScopeActivationStatus.SCOPE_TUPLE_REUSE
    assert reopened.reason == scope_tuple_reuse

    # Closing the same scope again is idempotent.
    assert store.close_scope(scope.scope_id) is False


def test_missing_scope_field_fails_closed_and_is_audited(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    with pytest.raises(MemoryScopeValidationError) as exc:
        store.activate_scope(
            MemoryScopeV1(
                project_id="llamar",
                experiment_id="run-1",
                context_id="",
                runtime_epoch=1,
            )
        )
    assert exc.value.code == "missing_scope_field"

    # A redacted security audit is recorded; the reason names the missing field.
    audits = store.security_audit_entries()
    assert audits and audits[-1]["kind"] == "scope_rejected"
    assert "context_id" in audits[-1]["reason"]

    # No domain scope was created.
    assert len(store.list_scopes()) == 0


def test_scope_validation_rejects_negative_epoch(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    with pytest.raises(MemoryScopeValidationError) as exc:
        store.activate_scope(
            MemoryScopeV1(
                project_id="llamar",
                experiment_id="run-1",
                context_id="ctx-1",
                runtime_epoch=-1,
            )
        )
    assert exc.value.code == "missing_scope_field"
    assert "runtime_epoch" in exc.value.reason
    assert len(store.list_scopes()) == 0

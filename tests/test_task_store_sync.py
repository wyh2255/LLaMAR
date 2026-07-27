"""Tests for TaskStore.sync_task_states — protobuf enum to PlanNode state mapping."""

import pytest

from a2a.coordinator.task_store import TaskStore


@pytest.fixture
def store():
    s = TaskStore("test request", router=None)
    s.update_plan([
        {"task_id": "t1", "worker_id": "Alice", "description": "Task 1"},
        {"task_id": "t2", "worker_id": "Bob", "description": "Task 2"},
        {"task_id": "t3", "worker_id": "Alice", "description": "Task 3"},
    ])
    return s


class TestSyncTaskStates:
    def test_submitted_maps_to_running(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_SUBMITTED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "running"

    def test_working_maps_to_running(self, store):
        store.set_state("t1", "pending")
        changed = store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "running"

    def test_running_task_active_in_worker_query(self, store):
        store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        active = store.get_active_tasks_by_worker("Alice")
        assert "t1" in active

    def test_completed_maps_to_done(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_COMPLETED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "done"

    def test_completed_updates_progress(self, store):
        store.sync_task_states({"t1": "TASK_STATE_COMPLETED", "t2": "TASK_STATE_COMPLETED"})
        prog = store.progress
        assert prog["done"] == 2

    def test_failed_maps_to_failed(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_FAILED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "failed"

    def test_rejected_maps_to_failed(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_REJECTED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "failed"

    def test_auth_required_maps_to_failed(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_AUTH_REQUIRED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "failed"

    def test_canceled_maps_to_canceled(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_CANCELED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "canceled"

    def test_input_required_maps_to_running(self, store):
        changed = store.sync_task_states({"t1": "TASK_STATE_INPUT_REQUIRED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "running"

    def test_unreachable_skipped(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "unreachable"})
        assert changed == []
        assert store.get_node("t1").state == "running"

    def test_unknown_skipped(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "unknown"})
        assert changed == []
        assert store.get_node("t1").state == "running"

    def test_unspecified_skipped(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "TASK_STATE_UNSPECIFIED"})
        assert changed == []
        assert store.get_node("t1").state == "running"

    def test_unrecognized_skipped_no_exception(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "BOGUS_STATE"})
        assert changed == []
        assert store.get_node("t1").state == "running"

    def test_terminal_state_done_not_overwritten(self, store):
        store.set_state("t1", "done")
        changed = store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        assert changed == []
        assert store.get_node("t1").state == "done"

    def test_terminal_state_verified_not_overwritten(self, store):
        store.set_state("t1", "verified")
        changed = store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        assert changed == []
        assert store.get_node("t1").state == "verified"

    def test_terminal_state_failed_not_overwritten(self, store):
        store.set_state("t1", "failed")
        changed = store.sync_task_states({"t1": "TASK_STATE_COMPLETED"})
        assert changed == []
        assert store.get_node("t1").state == "failed"

    def test_terminal_state_canceled_not_overwritten(self, store):
        store.set_state("t1", "canceled")
        changed = store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        assert changed == []
        assert store.get_node("t1").state == "canceled"

    def test_non_terminal_running_can_transition_to_done(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "TASK_STATE_COMPLETED"})
        assert changed == ["t1"]
        assert store.get_node("t1").state == "done"

    def test_multiple_tasks_synced(self, store):
        changed = store.sync_task_states({
            "t1": "TASK_STATE_COMPLETED",
            "t2": "TASK_STATE_FAILED",
            "t3": "TASK_STATE_WORKING",
        })
        assert len(changed) == 3
        assert store.get_node("t1").state == "done"
        assert store.get_node("t2").state == "failed"
        assert store.get_node("t3").state == "running"

    def test_no_change_for_same_state(self, store):
        store.set_state("t1", "running")
        changed = store.sync_task_states({"t1": "TASK_STATE_WORKING"})
        assert changed == []

    def test_empty_worker_states(self, store):
        changed = store.sync_task_states({})
        assert changed == []

    def test_unknown_task_id_in_worker_states(self, store):
        changed = store.sync_task_states({"nonexistent": "TASK_STATE_COMPLETED"})
        assert changed == []

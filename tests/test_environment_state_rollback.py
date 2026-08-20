"""Phase 4 — read_port -> legacy rollback drill.

The design (§Phase4 / H2) requires a ``read_port -> legacy`` rollback drill with
audit: on provider/error/ACL gate failure, the coordinator must (effectively)
select the legacy context rendering for the current operation and all
subsequent read-path calls in the process/run, preserve canonical DB/outbox
read-only, and append a redacted ``memory_rollout_audit`` record describing the
reason / mode transition.  A successful read_port case must NOT roll back.
"""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from a2a.coordinator.mission_runtime import PhysicalDispatch, PhysicalState
from Agent.environment_state import EnvironmentStateView, Freshness
from Agent.router_agent.context import (
    ContextConfig as RouterConfig,
)
from Agent.router_agent.context import (
    CoordinatorContextManager,
)
from Agent.router_agent.schema import Message as RouterMessage
from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider
from sar_orch.environment_state_provider import (
    MemoryRolloutController,
    RolloutAuditWriter,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def ingestor(store, scope_factory):
    ing = MemoryIngestor(
        store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


class FakeRuntime:
    def __init__(self, dispatches: list[PhysicalDispatch]):
        self.context_id = "ctx-1"
        self._dispatches = {d.dispatch_id: d for d in dispatches}
        self._control_revisions = {d.dispatch_id: 1 for d in dispatches}

    @property
    def dispatches(self) -> dict[str, PhysicalDispatch]:
        return self._dispatches

    def control_revision_of(self, dispatch_id: str) -> int:
        return self._control_revisions.get(dispatch_id, 0)

    class _Manager:
        epoch = 0

    _manager = _Manager()


def _provider(
    store, ingestor, scope_id, tmp_path, *, rollout=None
) -> SARCoordinatorStateProvider:
    runtime = FakeRuntime(
        [
            PhysicalDispatch(
                dispatch_id="d1",
                context_id="ctx-1",
                logical_node_id="n1",
                worker_id="Alice",
                worker_task_id="wt-1",
                state=PhysicalState.RUNNING,
            )
        ]
    )
    p = SARCoordinatorStateProvider(
        barrier=None,
        state_mode="semantic",
        log_dir=str(tmp_path),
        memory_read_mode="read_port",
    )
    # Attach the read-port provider so query_environment_state works.
    p.set_memory_ingestor(ingestor)
    p.set_runtime(runtime)
    if rollout is not None:
        p._memory_rollout = rollout
    return p


def _seed(ingestor, scope_id) -> None:
    ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_fire",
                sequence=0,
                env_step=8,
                actor_id="alice",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="FireA",
                entity_type="fire",
                field_name="intensity",
                value="High",
            )
        ]
    )


def _render(p, tmp_path) -> tuple[str, bool]:
    """Render the memory block and return (rendered, rolled_back_reason)."""
    ctx = CoordinatorContextManager(
        config=RouterConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=p,
    )
    messages = [
        RouterMessage(role="system", content="system"),
        RouterMessage(role="user", content="go"),
    ]
    assembled = ctx.assemble("system", messages)
    block = ""
    if len(assembled) > 1:
        content = assembled[-1].content
        block = content if isinstance(content, str) else ""
    return block, p.rollout_rolled_back()


# ---------------------------------------------------------------------------
# Successful read_port case does NOT roll back
# ---------------------------------------------------------------------------


def test_read_port_success_does_not_roll_back(store, ingestor, scope_factory, tmp_path):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _seed(ingestor, scope_id)
    p = _provider(store, ingestor, scope_id, tmp_path)

    block, rolled = _render(p, tmp_path)
    assert rolled is False
    assert "## Environment State" in block
    assert "Spatial State" in block
    assert "FireA" in block
    # No rollback audit written.
    assert not (tmp_path / "memory_rollout_audit.ndjson").exists()


# ---------------------------------------------------------------------------
# Read-port provider failure triggers rollback + legacy source + audit
# ---------------------------------------------------------------------------


def test_read_port_failure_rolls_back_to_legacy_with_audit(
    store, ingestor, scope_factory, tmp_path
):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _seed(ingestor, scope_id)
    p = _provider(store, ingestor, scope_id, tmp_path)

    # Force the canonical provider to fail after a good seed.
    p._env_state_provider = _BrokenProvider(scope_id)

    block, rolled = _render(p, tmp_path)
    assert rolled is True
    # Rolled back → legacy rendering used (Environment heading, not Spatial State
    # from canonical; the pinned legacy path renders "### Environment" etc.).
    assert "## Environment State" in block
    assert "Spatial State" not in block

    # Audit written (redacted reason).
    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert any("read_port_to_legacy_rollback" in line for line in lines)
    assert any("provider_error" in line for line in lines)

    # Canonical DB / revision untouched by the rollback.
    assert store.revision_of(scope_id) == 1
    assert store.temporal_event_count(scope_id) == 1


def test_read_port_unavailable_view_rolls_back(
    store, ingestor, scope_factory, tmp_path
):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _seed(ingestor, scope_id)
    p = _provider(store, ingestor, scope_id, tmp_path)

    # Provider returns UNAVAILABLE (e.g. scope mismatch / read error).
    p._env_state_provider = _UnavailableProvider(scope_id)

    block, rolled = _render(p, tmp_path)
    assert rolled is True
    assert "Spatial State" not in block

    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    raw = audit_path.read_text(encoding="utf-8")
    assert "read_port_to_legacy_rollback" in raw
    assert "UNAVAILABLE" in raw or "unavailable" in raw


# ---------------------------------------------------------------------------
# Rollback latches permanently: subsequent calls stay legacy + one audit
# ---------------------------------------------------------------------------


def test_rollback_is_latched_and_single_audit(store, ingestor, scope_factory, tmp_path):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _seed(ingestor, scope_id)
    p = _provider(store, ingestor, scope_id, tmp_path)

    p._env_state_provider = _BrokenProvider(scope_id)

    _, rolled1 = _render(p, tmp_path)
    assert rolled1 is True

    # Even after the provider "recovers", the latch keeps legacy rendering.
    p._env_state_provider = None  # would make query_fn absent
    p._attach_read_port_provider(p._memory_ingestor, p._runtime)
    block2, rolled2 = _render(p, tmp_path)
    assert rolled2 is True
    assert "Spatial State" not in block2

    # Exactly one rollback audit line per process/run.
    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    rollbacks = [l for l in lines if "read_port_to_legacy_rollback" in l]
    assert len(rollbacks) == 1


# ---------------------------------------------------------------------------
# Audit redaction: failure reason must never carry secrets
# ---------------------------------------------------------------------------


def test_rollback_audit_redacts_secret_in_reason(
    store, ingestor, scope_factory, tmp_path
):
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    p = _provider(store, ingestor, scope_id, tmp_path)

    class SecretFailure:
        @property
        def scope_id(self):
            return scope_id

        @property
        def viewer_role(self):
            return "coordinator"

        @property
        def viewer_id(self):
            return "system"

        @property
        def current_dispatch_id(self):
            return None

        def query_environment_state(self, query):
            raise RuntimeError("credential=SECRET_TOKEN123 mailbox_body=PRIVATE999")

    p._env_state_provider = SecretFailure()
    _, rolled = _render(p, tmp_path)
    assert rolled is True

    raw = (tmp_path / "memory_rollout_audit.ndjson").read_text(encoding="utf-8")
    assert "SECRET_TOKEN123" not in raw
    assert "PRIVATE999" not in raw
    assert "[REDACTED:" in raw or "REDACTED" in raw


# ---------------------------------------------------------------------------
# MemoryRolloutController unit behavior
# ---------------------------------------------------------------------------


def test_rollout_controller_latches_once(tmp_path):
    audit = RolloutAuditWriter(tmp_path / "memory_rollout_audit.ndjson")
    ctl = MemoryRolloutController(audit=audit, scope_id="s1")

    assert ctl.active is True
    assert ctl.rollback("boom") is True
    assert ctl.rolled_back is True
    assert ctl.active is False
    assert ctl.reason == "boom"
    # Second call is a no-op → single audit line.
    assert ctl.rollback("boom again") is False

    lines = (
        (tmp_path / "memory_rollout_audit.ndjson")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 1
    assert "read_port_to_legacy_rollback" in lines[0]
    assert lines[0].count("read_port_to_legacy_rollback") == 1


def test_rollout_controller_without_audit_is_noop(tmp_path):
    ctl = MemoryRolloutController(audit=None, scope_id="s1")
    assert ctl.rollback("x") is True
    assert not (tmp_path / "memory_rollout_audit.ndjson").exists()


class _BrokenProvider:
    """A read-port provider that raises on query."""

    def __init__(self, scope_id: str):
        self.scope_id = scope_id
        self.viewer_role = "coordinator"
        self.viewer_id = "system"
        self.current_dispatch_id = None

    def query_environment_state(self, query):
        raise RuntimeError("canonical memory unavailable")


class _UnavailableProvider:
    """A read-port provider that returns an UNAVAILABLE view."""

    def __init__(self, scope_id: str):
        self.scope_id = scope_id
        self.viewer_role = "coordinator"
        self.viewer_id = "system"
        self.current_dispatch_id = None

    def query_environment_state(self, query):
        return EnvironmentStateView(Freshness.UNAVAILABLE, reason="scope_mismatch")

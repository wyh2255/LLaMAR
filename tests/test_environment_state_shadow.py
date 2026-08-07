"""Phase 4 — live shadow-compare production wiring.

``memory_read_mode="shadow"`` keeps the legacy pinned Context as the LLM source
but the coordinator additionally builds the canonical provider view for the same
scope / principal and compares the two projections, recording non-allowlist
diffs in ``memory_rollout_audit.ndjson`` at the run log and exposing an
inspectable report/counter.  These tests prove the *live* path — not just the
framework functions — actually invokes the compare and records both clean and
non-allowlist outcomes, without touching the canonical DB and without breaking
pre-LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from a2a.coordinator.memory.contracts import (
    MemoryConfig,
    NormalizedProjectionInputV1,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore
from a2a.coordinator.mission_runtime import PhysicalDispatch, PhysicalState
from Agent.environment_state import (
    EnvironmentStateView,
    Freshness,
)
from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider


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


def _input(
    scope_id,
    *,
    event_id,
    domain,
    entity_id,
    field_name,
    value: Any,
    env_step=8,
    actor_id="alice",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id=actor_id,
        provenance="worker_sensor_tool",
        domain=domain,
        entity_id=entity_id,
        entity_type="agent" if domain == "embodied" else "fire",
        field_name=field_name,
        value=value,
    )


class FakeRuntime:
    def __init__(self, dispatches: list[PhysicalDispatch]):
        self.context_id = "ctx-1"
        self._dispatches = {d.dispatch_id: d for d in dispatches}
        self._control_revisions = {d.dispatch_id: 1 for d in dispatches}

    @property
    def dispatches(self) -> dict[str, PhysicalDispatch]:
        return self._dispatches

    def get_dispatch(self, dispatch_id: str) -> PhysicalDispatch | None:
        return self._dispatches.get(dispatch_id)

    def control_revision_of(self, dispatch_id: str) -> int:
        return self._control_revisions.get(dispatch_id, 0)

    class _Manager:
        epoch = 0

    _manager = _Manager()


def _seed_canonical(ingestor, scope_factory) -> str:
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_fire",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
            ),
            _input(
                scope_id,
                event_id="evt_pos",
                domain="embodied",
                entity_id="Alice",
                field_name="position",
                value=[3, 4, 0],
            ),
            _input(
                scope_id,
                event_id="evt_inv",
                domain="embodied",
                entity_id="Alice",
                field_name="inventory",
                value=["Water"],
            ),
        ]
    )
    return scope_id


def _build_shadow_provider(
    store, ingestor, scope_id, tmp_path, *, semantic_map=None
) -> SARCoordinatorStateProvider:
    """Build a SARCoordinatorStateProvider wired for live shadow compare."""
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
    provider = SARCoordinatorStateProvider(
        barrier=None,
        semantic_map=semantic_map,
        state_mode="semantic",
        log_dir=str(tmp_path),
        memory_read_mode="shadow",
    )
    provider.set_memory_ingestor(ingestor)
    provider.set_runtime(runtime)
    # Ensure the canonical read-port provider is attached (built on set_runtime).
    assert getattr(provider, "_env_state_provider", None) is not None
    return provider


def _legacy_semantic_map(*, fire_intensity: str, note: str = "") -> Any:
    """Build a SemanticMapStore whose legacy payload diverges from canonical
    (or matches it) for FireA intensity."""
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[],
        deposits=[],
        agents=[{"agent_id": "Alice"}],
        rules={"Chemical": "Sand", "Non-chemical": "Water"},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="Extinguish all fires",
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="alice",
            step=8,
            object_type="fire",
            name="FireA",
            position=(1, 2, 0),
            attributes={"intensity": fire_intensity, "note": note},
            confidence=1.0,
        )
    )
    return m


# ---------------------------------------------------------------------------
# Live shadow path invokes the compare (clean outcome)
# ---------------------------------------------------------------------------


def test_shadow_mode_live_path_runs_compare_and_records_clean(
    ingestor, store, scope_factory, tmp_path
):
    # No canonical seed and no semantic map seed → both projections empty, so
    # the live shadow compare is structurally clean (zero non-allowlist diffs).
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _build_shadow_provider(store, ingestor, scope_id, tmp_path)

    state = provider.snapshot()
    assert state is not None

    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    assert report["scope_id"] == scope_id
    assert report["last_clean"] is True
    assert report["non_allowlist_diff_count"] == 0
    assert report["clean_runs"] >= 1


def test_shadow_mode_compare_runs_once_per_env_step(
    ingestor, store, scope_factory, tmp_path
):
    scope_id = _seed_canonical(ingestor, scope_factory)
    provider = _build_shadow_provider(store, ingestor, scope_id, tmp_path)

    provider.snapshot()
    provider.snapshot()
    report = provider.shadow_compare_report()
    # Same env step → only one compare run (no audit spam).
    assert report["runs"] == 1

    # Advance the barrier step so the next snapshot re-runs the compare.
    provider._barrier = None
    provider._last_version = -1
    provider._shadow_compare_runs_per_step = set()
    provider.snapshot()
    assert provider.shadow_compare_report()["runs"] == 2


# ---------------------------------------------------------------------------
# Live shadow path records a non-allowlist diff in memory_rollout_audit
# ---------------------------------------------------------------------------


def test_shadow_mode_live_path_records_non_allowlist_diff_to_audit(
    ingestor, store, scope_factory, tmp_path
):
    scope_id = _seed_canonical(ingestor, scope_factory)
    # Legacy semantic map reports FireA intensity "Low" while canonical says High
    # → a non-allowlist domain diff the live shadow path must record.
    legacy_map = _legacy_semantic_map(fire_intensity="Low")
    provider = _build_shadow_provider(
        store, ingestor, scope_id, tmp_path, semantic_map=legacy_map
    )

    provider.snapshot()
    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    assert report["last_clean"] is False
    assert report["non_allowlist_diff_count"] >= 1
    assert report["last_audit_written"] >= 1

    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert any("intensity" in line for line in lines)

    # Canonical DB is untouched by the shadow compare (read-only evidence).
    assert store.revision_of(scope_id) == 1  # single accepted projection bundle
    assert store.temporal_event_count(scope_id) == 3


def test_shadow_mode_audit_redacts_secrets_and_mailbox(
    ingestor, store, scope_factory, tmp_path
):
    from sar_orch.environment_state_provider import (
        RolloutAuditWriter,
        ShadowCompareResult,
        ShadowDiff,
    )

    # A diff whose values carry a secret + raw mailbox body must be redacted in
    # the audit file — never persisted verbatim.
    audit_path = tmp_path / "memory_rollout_audit.ndjson"
    writer = RolloutAuditWriter(audit_path)
    result = ShadowCompareResult(
        diffs=[
            ShadowDiff(
                path=".spatial_state.FireA.fields.note",
                legacy={"note": "credential=SECRET123 mailbox_body=PRIVATEMAIL999"},
                read_port={"note": "credential=SECRET123 mailbox_body=PRIVATEMAIL999"},
                allowed=False,
            )
        ]
    )
    written = writer.record_diff(scope_id="s1", result=result, mode="shadow")
    assert written == 1
    raw = audit_path.read_text(encoding="utf-8")
    assert "SECRET123" not in raw
    assert "PRIVATEMAIL999" not in raw
    assert "[REDACTED:" in raw


def test_shadow_mode_compare_never_breaks_pre_llm(
    ingestor, store, scope_factory, tmp_path
):
    scope_id = _seed_canonical(ingestor, scope_factory)
    provider = _build_shadow_provider(store, ingestor, scope_id, tmp_path)

    # A broken canonical provider must not raise out of snapshot().
    class Broken:
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
            raise RuntimeError("canonical memory unavailable")

    provider._env_state_provider = Broken()

    state = provider.snapshot()
    assert state is not None
    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    assert report["last_clean"] is False
    assert "shadow_compare_error" in report["last_reason"]
    # Legacy Context source still works (snapshot returned a valid state).
    assert state.stale is False


def test_shadow_mode_report_empty_when_not_configured(tmp_path):
    provider = SARCoordinatorStateProvider(
        barrier=None,
        state_mode="semantic",
        memory_read_mode="legacy",
    )
    report = provider.shadow_compare_report()
    assert report["runs"] == 0
    assert "not_configured" in report["last_reason"]


# ---------------------------------------------------------------------------
# ShadowCompareService is invoked from the live path (unit-level proof)
# ---------------------------------------------------------------------------


def test_shadow_compare_service_clean_and_non_allowlist():
    from sar_orch.environment_state_provider import ShadowCompareService

    service = ShadowCompareService(audit_path=None)

    legacy = {
        "scope_id": "s1",
        "payload": {
            "semantic_summary": {
                "known_dynamic_objects": {
                    "fires": [
                        {
                            "name": "FireA",
                            "object_type": "fire",
                            "attributes": {"intensity": "High"},
                        }
                    ],
                    "persons": [],
                }
            },
            "team_status_summary": {},
            "physical_dispatches_view": [],
        },
    }
    # Clean canonical view: FireA intensity High.
    clean_view = EnvironmentStateView(
        Freshness.FRESH,
        source_revision=1,
        sections={
            "spatial_state": {
                "FireA": {
                    "entity_type": "fire",
                    "fields": {"intensity": {"value": "High"}},
                }
            },
            "embodied_state": {},
            "task_execution_state": [],
            "freshness": {"scope_id": "s1"},
        },
    )
    result = service.run(legacy_view=legacy, read_port_view=clean_view, scope_id="s1")
    assert result.clean
    assert service.report.clean_runs == 1
    assert service.report.non_allowlist_diff_count == 0

    # Divergent canonical view: intensity Low.
    divergent_view = EnvironmentStateView(
        Freshness.FRESH,
        source_revision=2,
        sections={
            "spatial_state": {
                "FireA": {
                    "entity_type": "fire",
                    "fields": {"intensity": {"value": "Low"}},
                }
            },
            "embodied_state": {},
            "task_execution_state": [],
            "freshness": {"scope_id": "s1"},
        },
    )
    result2 = service.run(
        legacy_view=legacy, read_port_view=divergent_view, scope_id="s1"
    )
    assert not result2.clean
    assert service.report.runs == 2
    assert service.report.non_allowlist_diff_count >= 1
    assert any("intensity" in p for p in service.report.last_diff_paths)


def test_shadow_mode_aligned_representative_run_is_clean(
    ingestor, store, scope_factory, tmp_path
):
    """A representative run where legacy semantic map and canonical Memory both
    derive from the SAME worker evidence (FireA pos+intensity, Alice
    pos+inventory) reports a CLEAN run through the live shadow path — proving
    H2 zero non-allowlist diffs is achievable, not just framework presence."""
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="e1",
                domain="spatial",
                entity_id="FireA",
                field_name="position",
                value=[1, 2, 0],
            ),
            _input(
                scope_id,
                event_id="e2",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
            ),
            _input(
                scope_id,
                event_id="e3",
                domain="embodied",
                entity_id="Alice",
                field_name="position",
                value=[3, 4, 0],
            ),
            _input(
                scope_id,
                event_id="e4",
                domain="embodied",
                entity_id="Alice",
                field_name="inventory",
                value=["Water"],
            ),
        ]
    )

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[],
        deposits=[],
        agents=[{"agent_id": "Alice"}],
        rules={},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="x",
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="alice",
            step=8,
            object_type="fire",
            name="FireA",
            position=(1, 2, 0),
            attributes={"intensity": "High"},
            confidence=1.0,
        )
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="alice",
            step=8,
            object_type="agent",
            name="Alice",
            position=(3, 4, 0),
            confidence=1.0,
        )
    )
    m.agents["Alice"].inventory = {"Water": 1}

    provider = _build_shadow_provider(
        store, ingestor, scope_id, tmp_path, semantic_map=m
    )
    provider.snapshot()

    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    assert report["last_clean"] is True
    assert report["non_allowlist_diff_count"] == 0
    assert report["clean_runs"] >= 1
    # No non-allowlist diff → no audit line written.
    assert not (tmp_path / "memory_rollout_audit.ndjson").exists()
    # Canonical DB untouched by the shadow compare.
    assert store.revision_of(scope_id) == 1

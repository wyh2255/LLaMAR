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


# ---------------------------------------------------------------------------
# H2: normalize_legacy_view matches the same worker-evidence projection
# ---------------------------------------------------------------------------

_SCENE1_FIRE_CELLS = [
    (
        "CaldorFire_0",
        (2, 2, 0),
        {"intensity": "High", "fire_type": "A", "parent_fire": "CaldorFire"},
    ),
    (
        "CaldorFire_1",
        (3, 2, 0),
        {"intensity": "High", "fire_type": "A", "parent_fire": "CaldorFire"},
    ),
]


def _scene1_observations() -> list[dict[str, Any]]:
    """Scene-1 worker evidence: Alice observes fire cells, a trapped person,
    a reservoir, a deposit, and her own embodied state."""
    obs: list[dict[str, Any]] = []
    for name, pos, attrs in _SCENE1_FIRE_CELLS:
        obs.append(
            {
                "reporter": "Alice",
                "step": 8,
                "object_type": "fire",
                "name": name,
                "position": list(pos),
                "attributes": dict(attrs),
                "confidence": 1.0,
            }
        )
    obs.append(
        {
            "reporter": "Alice",
            "step": 8,
            "object_type": "person",
            "name": "LostPersonTimmy",
            "position": [8, 22, 0],
            "attributes": {"status": "trapped", "load": 2},
            "confidence": 1.0,
        }
    )
    obs.append(
        {
            "reporter": "Alice",
            "step": 8,
            "object_type": "reservoir",
            "name": "ReservoirUtah",
            "position": [15, 5, 0],
            "attributes": {"resource_type": "A"},
            "confidence": 1.0,
        }
    )
    obs.append(
        {
            "reporter": "Alice",
            "step": 8,
            "object_type": "deposit",
            "name": "DepositFacility",
            "position": [23, 12, 0],
            "attributes": {"inventory": "Sand:5"},
            "confidence": 1.0,
        }
    )
    obs.append(
        {
            "reporter": "Alice",
            "step": 8,
            "object_type": "agent",
            "name": "Alice",
            "position": [9, 7, 0],
            "attributes": {"inventory": ["Water"]},
            "confidence": 1.0,
        }
    )
    return obs


def _observation_to_projection_inputs(scope_id: str, obs: dict[str, Any]):
    """Mirror ``_normalize_observation_projection_inputs`` so the canonical
    projection is derived from the SAME worker evidence as the legacy map."""
    obj_type = str(obs["object_type"])
    name = str(obs["name"])
    domain = "embodied" if obj_type == "agent" else "spatial"
    entity_type = "agent" if obj_type == "agent" else obj_type
    step = obs.get("step")
    actor_id = str(obs.get("reporter") or "unknown")
    attrs = dict(obs.get("attributes") or {})
    inputs = []

    def _claim(field_name, value):
        return NormalizedProjectionInputV1(
            scope_id=scope_id,
            event_id=f"cb:{name}:{field_name}:{step}",
            sequence=0,
            env_step=step,
            actor_id=actor_id,
            provenance="worker_sensor_tool",
            domain=domain,
            entity_id=name,
            entity_type=entity_type,
            field_name=field_name,
            value=value,
        )

    pos = obs.get("position")
    if pos is not None:
        inputs.append(_claim("position", list(pos)))
    for key, value in list(attrs.items())[:8]:
        if key in ("position", "inventory"):
            continue
        inputs.append(_claim(key, value))
    if obj_type == "agent":
        inventory = attrs.get("inventory")
        if inventory is None:
            inventory = obs.get("inventory")
        if inventory is not None:
            inputs.append(_claim("inventory", inventory))
    return inputs


def _ingest_scene1_canonical(ingestor, scope_id: str) -> None:
    inputs: list[NormalizedProjectionInputV1] = []
    for obs in _scene1_observations():
        inputs.extend(_observation_to_projection_inputs(scope_id, obs))
    ingestor.ingest_projection(inputs)


def _legacy_scene1_semantic_map() -> Any:
    """Build a legacy SemanticMapStore fed the SAME scene-1 worker evidence."""
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[
            {"name": "ReservoirUtah", "position": (15, 5), "resource_type": "A"}
        ],
        deposits=[{"name": "DepositFacility", "position": (23, 12)}],
        # Bob is a static-prior-only agent that is never worker-observed.
        agents=[{"agent_id": "Alice"}, {"agent_id": "Bob"}],
        rules={"A": "Water", "B": "Sand"},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="Extinguish all fires and rescue all persons",
    )
    for obs in _scene1_observations():
        m.ingest_observation(
            ObservationRecord(
                reporter=obs["reporter"],
                step=obs["step"],
                object_type=obs["object_type"],
                name=obs["name"],
                position=tuple(obs["position"]) if obs.get("position") else None,
                attributes=dict(obs["attributes"]),
                confidence=1.0,
            )
        )
    # Legacy map mirrors worker-reported agent inventory the same way the
    # aligned representative test does (agent inventory is not derived from
    # observations in the legacy sink; canonical claims it from the evidence).
    m.agents["Alice"].inventory = {"Water": 1}
    return m


def test_normalize_legacy_view_reconstructs_fire_regions_from_observed_cells():
    """Direct normalizer regression: fire regions expand into one entity per
    observed cell (matching the canonical per-cell projection), worker-derived
    attributes are projected, merge artifacts are excluded, and static-prior-
    only embodied agents are omitted."""
    from sar_orch.environment_state_provider import normalize_legacy_view

    m = _legacy_scene1_semantic_map()
    snap = m.snapshot()
    workers = snap.get("agents", [])
    norm = normalize_legacy_view(
        {
            "scope_id": "s1",
            "payload": {
                "semantic_summary": snap,
                "team_status_summary": {"workers": workers},
                "physical_dispatches_view": [],
            },
        }
    )

    spatial = norm["spatial_state"]
    # Fire region reconstructed per observed cell (not aggregated under the
    # parent_fire name, which canonical never stores as a standalone entity).
    assert "CaldorFire_0" in spatial
    assert "CaldorFire_1" in spatial
    assert "CaldorFire" not in spatial
    assert spatial["CaldorFire_0"]["fields"]["position"]["value"] == [2, 2, 0]
    assert spatial["CaldorFire_0"]["fields"]["parent_fire"]["value"] == "CaldorFire"
    assert spatial["CaldorFire_0"]["fields"]["fire_type"]["value"] == "A"
    assert spatial["CaldorFire_1"]["fields"]["intensity"]["value"] == "High"
    assert "observed_cells" not in spatial["CaldorFire_0"]["fields"]
    assert "sources" not in spatial["CaldorFire_0"]["fields"]

    # Worker-derived attributes for person / reservoir / deposit.
    assert spatial["LostPersonTimmy"]["fields"]["load"]["value"] == 2
    assert spatial["ReservoirUtah"]["fields"]["resource_type"]["value"] == "A"
    assert spatial["DepositFacility"]["fields"]["position"]["value"] == [23, 12, 0]

    # Static-prior-only embodied agent omitted; worker-observed Alice kept.
    embodied = norm["embodied_state"]
    assert "Bob" not in embodied
    assert embodied["Alice"]["fields"]["position"]["value"] == [9, 7, 0]
    assert embodied["Alice"]["fields"]["inventory"]["value"] == ["Water"]


def test_normalize_legacy_view_omits_unobserved_spatial_priors():
    """Reservoirs / deposits are static priors: an unobserved one is omitted so
    the projection matches canonical (which only contains worker-observed
    entities), while an observed one is kept."""
    from sar_orch.environment_state_provider import normalize_legacy_view
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[
            {"name": "ReservoirUtah", "position": (15, 5), "resource_type": "A"},
            {"name": "ReservoirYork", "position": (18, 8), "resource_type": "B"},
        ],
        deposits=[],
        agents=[],
        rules={},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="x",
    )
    # Only ReservoirUtah is worker-observed.
    m.ingest_observation(
        ObservationRecord(
            reporter="Alice",
            step=8,
            object_type="reservoir",
            name="ReservoirUtah",
            position=(15, 5, 0),
            attributes={"resource_type": "A"},
            confidence=1.0,
        )
    )
    snap = m.snapshot()
    norm = normalize_legacy_view(
        {
            "scope_id": "s1",
            "payload": {
                "semantic_summary": snap,
                "team_status_summary": {"workers": []},
                "physical_dispatches_view": [],
            },
        }
    )
    spatial = norm["spatial_state"]
    assert "ReservoirUtah" in spatial
    assert "ReservoirYork" not in spatial


def test_shadow_mode_scene1_observed_evidence_clean_compare(
    ingestor, store, scope_factory, tmp_path
):
    """Live shadow compare over scene-1 observed evidence: feeding the SAME
    observations to the legacy semantic map and the canonical projection yields
    a CLEAN run — proving normalize_legacy_view matches the canonical
    worker-evidence projection (H2 zero non-allowlist diffs achievable)."""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _ingest_scene1_canonical(ingestor, scope_id)
    legacy_map = _legacy_scene1_semantic_map()

    provider = _build_shadow_provider(
        store, ingestor, scope_id, tmp_path, semantic_map=legacy_map
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


# ---------------------------------------------------------------------------
# H1 residual shadow audit: parent/cell differing values + freshness conflicts
# ---------------------------------------------------------------------------


def _legacy_parent_cell_map() -> Any:
    """Legacy map where the parent region AND a cell are directly observed at
    the SAME step with DIFFERING fire_type + independent positions."""
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[],
        deposits=[],
        agents=[],
        rules={},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="x",
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="Alice",
            step=8,
            object_type="fire",
            name="CaldorFire",
            position=(10, 10, 0),
            attributes={
                "average_intensity": "Medium",
                "fire_type": "A",
                "status": "active",
            },
            confidence=1.0,
        )
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="Bob",
            step=8,
            object_type="fire",
            name="CaldorFire_0",
            position=(2, 2, 0),
            attributes={
                "intensity": "High",
                "fire_type": "B",
                "parent_fire": "CaldorFire",
            },
            confidence=1.0,
        )
    )
    return m


def _legacy_parent_cell_observation_inputs(scope_id: str):
    """Canonical inputs for the SAME parent/cell evidence (region Fire claim +
    cell Flammable claim)."""
    inputs = []
    inputs.append(
        NormalizedProjectionInputV1(
            scope_id=scope_id,
            event_id="cb:alice:CaldorFire:8",
            sequence=0,
            env_step=8,
            actor_id="alice",
            provenance="worker_sensor_tool",
            domain="spatial",
            entity_id="CaldorFire",
            entity_type="fire",
            field_name="position",
            value=[10, 10, 0],
        )
    )
    for field, value in (
        ("average_intensity", "Medium"),
        ("fire_type", "A"),
        ("status", "active"),
    ):
        inputs.append(
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id=f"cb:alice:CaldorFire:{field}:8",
                sequence=0,
                env_step=8,
                actor_id="alice",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="CaldorFire",
                entity_type="fire",
                field_name=field,
                value=value,
            )
        )
    for field, value in (
        ("position", [2, 2, 0]),
        ("intensity", "High"),
        ("fire_type", "B"),
        ("parent_fire", "CaldorFire"),
    ):
        inputs.append(
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id=f"cb:bob:CaldorFire_0:{field}:8",
                sequence=0,
                env_step=8,
                actor_id="bob",
                provenance="worker_sensor_tool",
                domain="spatial",
                entity_id="CaldorFire_0",
                entity_type="fire",
                field_name=field,
                value=value,
            )
        )
    return inputs


def test_normalize_legacy_view_preserves_parent_cell_differing_values():
    """Direct normalizer regression: parent/cell differing values are projected
    independently — the region entity carries its own direct claims and the cell
    entity carries its own, so neither clobbers the other (canonical-aligned)."""
    from sar_orch.environment_state_provider import normalize_legacy_view

    m = _legacy_parent_cell_map()
    snap = m.snapshot()
    norm = normalize_legacy_view(
        {
            "scope_id": "s1",
            "payload": {
                "semantic_summary": snap,
                "team_status_summary": {"workers": []},
                "physical_dispatches_view": [],
            },
        }
    )

    spatial = norm["spatial_state"]
    # Parent region: its OWN direct claims (average_intensity + fire_type A),
    # never clobbered by the cell's differing fire_type B.
    region = spatial["CaldorFire"]
    assert region["fields"]["average_intensity"]["value"] == "Medium"
    assert region["fields"]["fire_type"]["value"] == "A"
    assert region["fields"]["position"]["value"] == [10, 10, 0]
    # Cell: its OWN claims (fire_type B, intensity High, parent_fire, position).
    cell = spatial["CaldorFire_0"]
    assert cell["fields"]["intensity"]["value"] == "High"
    assert cell["fields"]["fire_type"]["value"] == "B"
    assert cell["fields"]["parent_fire"]["value"] == "CaldorFire"
    assert cell["fields"]["position"]["value"] == [2, 2, 0]
    # No same-entity conflict: parent/cell differ at the entity boundary.
    assert norm["freshness"]["conflicts"] == []


def test_shadow_mode_parent_cell_differing_values_clean_compare(
    ingestor, store, scope_factory, tmp_path
):
    """Live shadow compare over parent/cell differing values: the same evidence
    fed to the legacy map and canonical Memory projects the parent region and
    its cell as SEPARATE entities with their OWN values — a CLEAN run (no
    clobbering, no false conflict)."""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    ingestor.ingest_projection(_legacy_parent_cell_observation_inputs(scope_id))

    legacy_map = _legacy_parent_cell_map()
    provider = _build_shadow_provider(
        store, ingestor, scope_id, tmp_path, semantic_map=legacy_map
    )
    provider.snapshot()

    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    assert report["last_clean"] is True
    assert report["non_allowlist_diff_count"] == 0
    assert report["clean_runs"] >= 1
    assert not (tmp_path / "memory_rollout_audit.ndjson").exists()

    # Canonical DB untouched by the shadow compare.
    assert store.revision_of(scope_id) == 1


def test_normalize_legacy_view_emits_freshness_conflicts_canonical_shape():
    """Direct normalizer regression: same-step differing claims surface as
    canonical-compatible freshness conflict info (domain / entity_id /
    field_name / value / conflict_candidates) with the C3 retained holder."""
    from sar_orch.environment_state_provider import normalize_legacy_view
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[],
        deposits=[],
        agents=[],
        rules={},
        step_budget={"current_step": 0, "max_steps": 50, "remaining": 50},
        task_objective="x",
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="Alice",
            step=6,
            object_type="person",
            name="Timmy",
            position=(10, 10, 0),
            attributes={"status": "trapped"},
            confidence=1.0,
        )
    )
    m.ingest_observation(
        ObservationRecord(
            reporter="Bob",
            step=6,
            object_type="person",
            name="Timmy",
            position=(10, 10, 0),
            attributes={"status": "rescued"},
            confidence=1.0,
        )
    )

    norm = normalize_legacy_view(
        {
            "scope_id": "s1",
            "payload": {
                "semantic_summary": m.snapshot(),
                "team_status_summary": {"workers": []},
                "physical_dispatches_view": [],
            },
        }
    )

    conflicts = norm["freshness"]["conflicts"]
    assert len(conflicts) == 1
    entry = conflicts[0]
    # Canonical-compatible shape.
    assert set(entry) == {
        "domain",
        "entity_id",
        "field_name",
        "value",
        "conflict_candidates",
    }
    assert entry["domain"] == "spatial"
    assert entry["entity_id"] == "Timmy"
    assert entry["field_name"] == "status"
    # C3: the retained current holder is explicit (Alice's trapped), not the
    # last-write-wins Bob claim.
    assert entry["value"] == "trapped"
    assert entry["conflict_candidates"] == [
        {"value": "trapped"},
        {"value": "rescued"},
    ]
    # The projected person entity keeps the same holder.
    assert norm["spatial_state"]["Timmy"]["fields"]["status"]["value"] == "trapped"


def test_shadow_mode_same_step_cross_worker_conflict_c3_explicit(
    ingestor, store, scope_factory, tmp_path
):
    """Live shadow compare over a same-step cross-worker conflict: both the
    legacy map and canonical Memory record the C3 conflict EXPLICITLY and keep
    the SAME current holder, and the normalizer emits freshness conflict info in
    the canonical shape."""
    from sar_orch.environment_state_provider import normalize_legacy_view
    from sar_orch.map import SemanticMapStore
    from sar_orch.map.store import ObservationRecord

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    # Canonical: Alice reports High at step 8, Bob reports Low at step 8.
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="cb:alice:FireA:position:8",
                domain="spatial",
                entity_id="FireA",
                field_name="position",
                value=[1, 2, 0],
                env_step=8,
                actor_id="alice",
            ),
            _input(
                scope_id,
                event_id="cb:alice:FireA:intensity:8",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="High",
                env_step=8,
                actor_id="alice",
            ),
            _input(
                scope_id,
                event_id="cb:bob:FireA:intensity:8",
                domain="spatial",
                entity_id="FireA",
                field_name="intensity",
                value="Low",
                env_step=8,
                actor_id="bob",
            ),
        ]
    )
    field = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert field["outcome"] == "conflicted"
    assert field["value"] == "High"  # C3: current holder retained in canonical

    # Legacy map fed the SAME evidence.
    m = SemanticMapStore()
    m.init_priors(
        reservoirs=[],
        deposits=[],
        agents=[],
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
            reporter="bob",
            step=8,
            object_type="fire",
            name="FireA",
            position=(1, 2, 0),
            attributes={"intensity": "Low"},
            confidence=1.0,
        )
    )
    fire = m.fires["FireA"]
    assert fire.conflict is True
    assert fire.attributes["intensity"] == "High"  # C3: legacy retains holder

    provider = _build_shadow_provider(
        store, ingestor, scope_id, tmp_path, semantic_map=m
    )
    provider.snapshot()

    report = provider.shadow_compare_report()
    assert report["runs"] >= 1
    # The C3 conflict is surfaced on BOTH sides in the same shape, so the
    # aligned shadow run stays clean (candidate provenance is allowlisted).
    assert report["last_clean"] is True
    assert report["non_allowlist_diff_count"] == 0
    assert report["clean_runs"] >= 1

    # C3 alignment: the normalized legacy conflicts and the canonical conflicts
    # agree on entity / field / retained value.
    snap = m.snapshot()
    norm = normalize_legacy_view(
        {
            "scope_id": scope_id,
            "payload": {
                "semantic_summary": snap,
                "team_status_summary": {"workers": []},
                "physical_dispatches_view": [],
            },
        }
    )
    legacy_conflicts = norm["freshness"]["conflicts"]
    assert len(legacy_conflicts) == 1
    assert legacy_conflicts[0]["entity_id"] == "FireA"
    assert legacy_conflicts[0]["field_name"] == "intensity"
    assert legacy_conflicts[0]["value"] == "High"

    canonical_conflicts = [
        row
        for row in store.projection_fields(scope_id)
        if row["outcome"] == "conflicted"
    ]
    assert len(canonical_conflicts) == 1
    assert canonical_conflicts[0]["entity_id"] == "FireA"
    assert canonical_conflicts[0]["value"] == "High"
    # Canonical DB untouched by the shadow compare.
    assert store.temporal_event_count(scope_id) == 3

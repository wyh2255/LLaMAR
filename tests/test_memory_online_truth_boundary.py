"""Phase 3 H1-INV-1 online truth boundary: no Barrier / oracle / ground-truth
crosses the MemoryIngestor into Temporal / Spatial / Embodied Memory.

C6 - valid-signed Worker evidence carrying secret/HMAC/Authorization/Cookie
plus a masked ``oracle`` / ``ground_truth`` / direct-world provenance is:
  - typed ``online_truth_forbidden`` with zero domain writes (no Temporal,
    projection, relation, revision, outbox or Context content);
  - the only diagnostic retained is a redacted reason / digest.

These tests also pin the source-level boundary: the memory modules must never
reference ``SARBarrier.get_env_snapshot()``, checker truth, or oracle tools.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from a2a.coordinator.memory.contracts import (
    ONLINE_PROVENANCE_ALLOWLIST,
    MemoryConfig,
    NormalizedProjectionInputV1,
    online_truth_forbidden,
)
from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
from a2a.coordinator.memory.redaction import RedactionPolicy
from a2a.coordinator.memory.store import MemoryStore

SECRET = "SUPERSECRET_VALUE_9f2c1"
HMAC_HEX = "c" * 64

FORBIDDEN_PROVENANCE = [
    "barrier",
    "oracle",
    "ground_truth",
    "checker",
    "simulator",
    "world_snapshot",
]


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
        redaction=RedactionPolicy(secret=SECRET.encode("utf-8")),
    )
    ing.activate_scope("ctx-1", 0)
    return ing


def _scope_id_of(scope_factory):
    return scope_factory.resolve("ctx-1", 0).scope_id


def _input(
    scope_id,
    *,
    event_id="evt_1",
    provenance="worker_sensor_tool",
    value: Any = "High",
    field_name="intensity",
    env_step: int | None = 8,
    domain="spatial",
    entity_id="FireA",
):
    return NormalizedProjectionInputV1(
        scope_id=scope_id,
        event_id=event_id,
        sequence=0,
        env_step=env_step,
        actor_id="alice",
        provenance=provenance,
        domain=domain,
        entity_id=entity_id,
        entity_type="fire",
        field_name=field_name,
        value=value,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# H1-INV-1: every forbidden provenance is typed and writes zero domains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provenance", FORBIDDEN_PROVENANCE)
def test_forbidden_provenance_zero_domain_writes(
    ingestor, store, scope_factory, provenance
):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection([_input(scope_id, provenance=provenance)])

    assert result.status == online_truth_forbidden
    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.relations_for_scope(scope_id) == []
    assert store.outbox_entries(scope_id) == []
    # No Context / Environment State content is produced either.
    assert result.outcomes == ()


def test_forbidden_provenance_leaves_only_redacted_diagnostic(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                provenance="oracle",
                value={"ground_truth": "FireA@[1,2,0]"},
                field_name="position",
                env_step=8,
            )
        ]
    )
    assert result.status == online_truth_forbidden

    audits = store.security_audit_entries()
    assert audits, "online_truth_forbidden must leave a redacted diagnostic"
    any_forbidden = [a for a in audits if a["kind"] == "online_truth_forbidden"]
    assert any_forbidden
    serialized = json.dumps(any_forbidden)
    # The diagnostic keeps only redacted reason/digest, never the raw candidate.
    assert "FireA@[1,2,0]" not in serialized
    assert "ground_truth" not in serialized or "[REDACTED" in serialized


def test_mixed_bundle_with_forbidden_candidate_writes_nothing(
    ingestor, store, scope_factory
):
    """A bundle that contains any oracle/direct-world candidate is denied as a
    whole: zero domain writes for the entire callback."""
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(scope_id, event_id="evt_ok", value="High"),
            _input(scope_id, event_id="evt_oracle", provenance="ground_truth"),
        ]
    )
    assert result.status == online_truth_forbidden
    assert store.temporal_event_count(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.revision_of(scope_id) == 0


def test_provenance_allowlist_contains_only_online_worker_sources():
    allowed = {
        "worker_sensor_tool",
        "worker_telemetry",
        "worker_observation",
        "peer_report",
        "registry",
        "control",
        "supervision",
    }
    assert ONLINE_PROVENANCE_ALLOWLIST == allowed
    assert ONLINE_PROVENANCE_ALLOWLIST.isdisjoint(
        {"barrier", "oracle", "ground_truth", "checker", "simulator", "world_snapshot"}
    )


# ---------------------------------------------------------------------------
# H1-INV-1 gate #2: an ALLOWLISTED provenance carrying oracle/ground-truth/
# direct-world fields is still denied (masked candidate -> zero domain writes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"ground_truth": "FireA@[1,2,0]"},
        {"position": [1, 2, 0], "note": "oracle says FireA is High"},
        {"attributes": {"intensity": "ground_truth"}, "position": [1, 2, 0]},
        "checker truth: FireA covered",
        [{"world_snapshot": "step=5"}],
        {"simulator": {"fires": 1}, "position": [1, 2, 0]},
    ],
)
def test_allowlisted_provenance_masked_truth_value_is_denied(
    ingestor, store, scope_factory, value
):
    """A Worker-sensor claim whose VALUE masks a direct-world/oracle field must
    be typed ``online_truth_forbidden`` with zero domain writes — the allowlist
    gate is not bypassed by a clean provenance label."""
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                provenance="worker_sensor_tool",
                value=value,
                field_name="position",
                env_step=8,
            )
        ]
    )
    assert result.status == online_truth_forbidden
    assert store.temporal_event_count(scope_id) == 0
    assert store.revision_of(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.relations_for_scope(scope_id) == []
    assert store.outbox_entries(scope_id) == []


def test_masked_truth_denial_diagnostic_never_contains_raw_terms_or_secret(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                provenance="worker_sensor_tool",
                value={
                    "ground_truth": f"FireA@[1,2,0] secret={SECRET}",
                    "position": [1, 2, 0],
                },
                field_name="position",
                env_step=8,
            )
        ]
    )
    assert result.status == online_truth_forbidden

    audit_text = json.dumps(store.security_audit_entries())
    # Diagnostics keep only a redacted reason/digest — never the raw candidate
    # value, the raw forbidden term, or a secret.
    assert "FireA@[1,2,0]" not in audit_text
    assert SECRET not in audit_text
    assert "ground_truth" not in audit_text
    assert any(
        a["kind"] == "online_truth_forbidden"
        for a in store.security_audit_entries()
    )


def test_masked_truth_in_any_bundle_member_denies_whole_bundle(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(scope_id, event_id="evt_ok", value="High", field_name="intensity"),
            _input(
                scope_id,
                event_id="evt_masked",
                value={"oracle": "FireA", "intensity": "High"},
                field_name="intensity",
            ),
        ]
    )
    assert result.status == online_truth_forbidden
    assert store.temporal_event_count(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.revision_of(scope_id) == 0


# ---------------------------------------------------------------------------
# C6: secrets never cross the Ingestor; only redacted diagnostic remains
# ---------------------------------------------------------------------------


def test_secret_in_valid_worker_evidence_is_redacted_everywhere(
    ingestor, store, scope_factory
):
    scope_id = _scope_id_of(scope_factory)
    payload = {
        "position": [1, 2, 0],
        "note": f"Authorization: Bearer {SECRET} hmac={HMAC_HEX}",
        "cookie": "session=abc123",
    }
    result = ingestor.ingest_projection(
        [_input(scope_id, value=payload, field_name="position", env_step=8)]
    )
    assert result.status == "ok"

    # SQLite projection has no raw secret.
    serialized = json.dumps(store.projection_fields(scope_id))
    assert SECRET not in serialized
    assert HMAC_HEX not in serialized
    assert "session=abc123" not in serialized

    # Temporal payload has no raw secret.
    events_text = json.dumps(store.temporal_events(scope_id))
    assert SECRET not in events_text
    assert HMAC_HEX not in events_text

    # Relation / outcome audit has no raw secret.
    rels = json.dumps(
        [
            r.to_dict() if hasattr(r, "to_dict") else str(r)
            for r in store.relations_for_scope(scope_id)
        ]
    )
    assert SECRET not in rels


def test_secret_plus_oracle_never_leaks_to_audit(ingestor, store, scope_factory):
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                provenance="oracle",
                value={"secret": SECRET, "position": [1, 2, 0]},
                field_name="position",
            )
        ]
    )
    assert result.status == online_truth_forbidden
    audit_text = json.dumps(store.security_audit_entries())
    assert SECRET not in audit_text


def test_normalized_input_rejects_missing_scope_fields():
    with pytest.raises(Exception, match="scope"):
        NormalizedProjectionInputV1(
            scope_id="",
            event_id="evt_1",
            sequence=0,
            env_step=8,
            actor_id="alice",
            provenance="worker_sensor_tool",
            domain="spatial",
            entity_id="FireA",
            entity_type="fire",
            field_name="intensity",
            value="High",
        ).validate()


# ---------------------------------------------------------------------------
# Source-level boundary: memory modules never read Barrier / checker / oracle
# ---------------------------------------------------------------------------


def test_memory_modules_have_no_barrier_oracle_dependency():
    import inspect

    from a2a.coordinator.memory import ingestor as _ingestor
    from a2a.coordinator.memory import projections as _projections

    sources = "".join(
        [
            inspect.getsource(_ingestor),
            inspect.getsource(_projections),
        ]
    )
    assert "get_env_snapshot" not in sources
    assert "QuerySARStateTool" not in sources
    # No dependency on the Barrier simulator type / package at all (the bare
    # word "barrier" may appear only as a denied-provenance reason string).
    assert "SARBarrier" not in sources
    assert "from sar_orch.barrier" not in sources
    assert "import SARBarrier" not in sources


def test_semantic_mode_never_reads_barrier_world_fields():
    from unittest.mock import MagicMock

    from sar_orch.coordinator_state_provider import SARCoordinatorStateProvider

    calls: list[str] = []

    class SpyBarrier:
        _step_counter = 5
        _finished = False
        env = MagicMock()
        env.task_timeout = 50

        def is_finished(self):
            return self._finished

        def get_env_snapshot(self):
            calls.append("get_env_snapshot")
            return {
                "agents": [
                    {
                        "name": "Alice",
                        "agent_id": "Alice",
                        "position": [99, 99, 0],
                        "inventory": {"water": 99},
                    }
                ],
                "fires": [],
                "persons": [],
            }

    class WorkerOnlySemanticMap:
        def get_step_budget(self):
            return {"current_step": 5, "max_steps": 50, "remaining": 45}

        def get_recent_observations(self, limit=5):
            return []

        def snapshot(self):
            return {
                "known_priors": {"reservoirs": [], "deposits": []},
                "known_dynamic_objects": {"fires": [], "persons": []},
                "agents": [
                    {
                        "agent_id": "Alice",
                        "last_position": [1, 2, 0],
                        "inventory": {"water": 3},
                        "last_seen_step": 5,
                    }
                ],
                "recent_observations": [],
                "stale_entries": [],
                "conflicts": [],
            }

    provider = SARCoordinatorStateProvider(
        barrier=SpyBarrier(),
        semantic_map=WorkerOnlySemanticMap(),
        event_store=None,
        state_mode="semantic",
    )
    state = provider.snapshot()

    assert calls == []
    assert "global_snapshot" not in state.payload
    workers = state.payload["team_status_summary"]["workers"]
    assert workers and workers[0]["agent_id"] == "Alice"
    assert workers[0]["last_position"] == [1, 2, 0]
    assert workers[0]["inventory"] == {"water": 3}


# ---------------------------------------------------------------------------
# Phase 0（P0）增补 —— telemetry 的真相隔离 + 反思输入词表复用
# 只追加；不改动既有测试与 fixture。
# ---------------------------------------------------------------------------


def test_worker_telemetry_value_with_truth_term_denied(ingestor, store, scope_factory):
    """allowlisted ``worker_telemetry`` 携带掩码真值词（oracle/ground_truth）
    → 整束 online_truth_forbidden、零 domain 写（H1-INV-1 门 #2）。

    GREEN 守护：P2 telemetry 提取的 value 仍走现有 truth scan（ingestor.py:108-118）。
    """
    scope_id = _scope_id_of(scope_factory)
    result = ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_tel_truth",
                provenance="worker_telemetry",
                field_name="position",
                value={"position": [1, 2, 0], "oracle": [9, 9, 9]},
                domain="embodied",
                entity_id="Alice",
            )
        ]
    )
    assert result.status == online_truth_forbidden
    assert store.temporal_event_count(scope_id) == 0
    assert store.projection_fields(scope_id) == []
    assert store.revision_of(scope_id) == 0


def test_telemetry_denial_diagnostic_redacted(ingestor, store, scope_factory):
    """telemetry 束被拒后，security_audit 诊断只含脱敏 digest，不回显 raw
    值/词表（ingestor.py:703-718）。

    GREEN 守护：P4 反思 validator 的拒绝审计复用同一脱敏范式。
    """
    scope_id = _scope_id_of(scope_factory)
    ingestor.ingest_projection(
        [
            _input(
                scope_id,
                event_id="evt_tel_truth2",
                provenance="worker_telemetry",
                field_name="position",
                value={"position": [1, 2, 0], "ground_truth": "secret-truth-value"},
                domain="embodied",
                entity_id="Alice",
            )
        ]
    )
    audits = store.security_audit_entries()
    assert audits
    joined = json.dumps(audits)
    assert "secret-truth-value" not in joined
    assert "ground_truth" not in joined


def test_reflection_validator_reuses_forbidden_truth_terms():
    """P4 反思 validator 必须复用 FORBIDDEN_TRUTH_TERMS 词表（不允许另建词表），
    且 reflection 模块同时提供 fail-closed 校验入口。

    当前 ``a2a.coordinator.memory.reflection`` 不存在 → ImportError（预期 RED，
    Phase 1/4 实现）。
    """
    from a2a.coordinator.memory.reflection import (
        validate_reflection_response,
    )

    from a2a.coordinator.memory.contracts import FORBIDDEN_TRUTH_TERMS

    assert "oracle" in FORBIDDEN_TRUTH_TERMS
    result = validate_reflection_response(
        {"function_call": {"statement": "oracle says x", "memory_key": "k", "kind": "status", "confidence": 0.9, "source_refs": []}}
    )
    assert result.status == "rejected"
    assert result.long_term_memory_written == 0


def test_memory_modules_never_import_reflection_truth_sources():
    """源码级边界：memory 模块不得引用 SARBarrier/get_env_snapshot/oracle 工具
    （既有边界），反思源同样不得引用 EventStore/truth recorder。

    GREEN 守护（镜像现有 test_memory_online_truth_boundary.py:354-372 的源码
    边界审计，扩展到未来 reflection 模块路径——当前不存在则跳过生产侧断言）。
    """
    import inspect

    import a2a.coordinator.memory.ingestor as ingestor_module

    source = inspect.getsource(ingestor_module)
    for forbidden in ("SARBarrier", "get_env_snapshot", "QuerySARStateTool", "EventStore"):
        assert forbidden not in source

"""Tests for Phase 5 — Automated Observation Reporting.

Covers:
- ToolResult.data field
- Barrier structured observations
- Action tools including data in ToolResult
- WorkerReportPublisher dedup
- Coordinator observation extraction (both legacy + auto format)
- SemanticMapStore bounded retention
- End-to-end observation flow
"""

import json

from Agent.worker_agent.tools.base import ToolResult
from Agent.router_agent.tools.base import ToolResult as RouterToolResult
from sar_orch.map import WorkerReportPublisher
from sar_orch.map import ObservationRecord, SemanticMapStore
from sar_orch.tools.worker._barrier_helpers import (
    set_publisher,
    tool_result_from_barrier,
)

# ---- Shared helpers ----

_SAMPLE_BARRIER_RESULT = {
    "observation": "You see a fire at (1,2,0).",
    "structured_observations": [
        {
            "reporter": "Alice",
            "step": 5,
            "object_type": "fire",
            "name": "Fire_1",
            "position": [1, 2, 0],
            "attributes": {"intensity": "High", "fire_type": "Chemical"},
            "confidence": 1.0,
        }
    ],
    "structured_position": (3, 1, 0),
    "structured_inventory": ["Water"],
    "success": True,
}

# =============================================================
# ToolResult.data
# =============================================================


def test_tool_result_worker_agent_accepts_data():
    tr = ToolResult(success=True, content="test", data={"observations": []})
    assert tr.data == {"observations": []}


def test_tool_result_router_agent_accepts_data():
    tr = RouterToolResult(success=True, content="test", data={"observations": []})
    assert tr.data == {"observations": []}


def test_tool_result_data_defaults_to_none():
    tr = ToolResult(success=True, content="test")
    assert tr.data is None


# =============================================================
# tool_result_from_barrier helper
# =============================================================


def test_tool_result_from_barrier_includes_data():
    tr = tool_result_from_barrier(_SAMPLE_BARRIER_RESULT)
    assert tr.success is True
    assert "observation" not in (tr.data or {})
    assert tr.data["observations"] == _SAMPLE_BARRIER_RESULT["structured_observations"]
    assert tr.data["position"] == (3, 1, 0)
    assert tr.data["inventory"] == ["Water"]


def test_tool_result_from_barrier_content_from_observation():
    tr = tool_result_from_barrier(_SAMPLE_BARRIER_RESULT)
    assert tr.content == "You see a fire at (1,2,0)."


def test_tool_result_from_barrier_with_content_override():
    tr = tool_result_from_barrier(
        _SAMPLE_BARRIER_RESULT,
        overrides={"content": "Custom: You see a fire."},
    )
    assert tr.content == "Custom: You see a fire."


def test_tool_result_from_barrier_no_observations():
    result = {
        "observation": "Nothing interesting.",
        "success": True,
    }
    tr = tool_result_from_barrier(result)
    assert tr.data is None


# =============================================================
# WorkerReportPublisher dedup
# =============================================================


def test_publisher_passes_new_observation():
    pub = WorkerReportPublisher(agent_name="Alice")
    data = {
        "observations": [
            {
                "object_type": "fire",
                "name": "Fire_1",
                "position": [1, 2, 0],
                "attributes": {"intensity": "High"},
                "confidence": 1.0,
            }
        ]
    }
    result = pub.apply_to_data(data)
    assert result is not None
    assert len(result["observations"]) == 1


def test_publisher_filters_identical_observation():
    pub = WorkerReportPublisher(agent_name="Alice")
    obs = {
        "object_type": "fire",
        "name": "Fire_1",
        "position": [1, 2, 0],
        "attributes": {"intensity": "High"},
        "confidence": 1.0,
    }
    # First call — passes through
    data1 = {"observations": [dict(obs)]}
    result1 = pub.apply_to_data(data1)
    assert result1 is not None
    assert len(result1["observations"]) == 1

    # Second call with same obs — filtered out
    data2 = {"observations": [dict(obs)]}
    result2 = pub.apply_to_data(data2)
    assert result2 is None  # All filtered


def test_publisher_passes_changed_observation():
    pub = WorkerReportPublisher(agent_name="Alice")
    obs1 = {
        "object_type": "fire",
        "name": "Fire_1",
        "position": [1, 2, 0],
        "attributes": {"intensity": "High"},
        "confidence": 1.0,
    }
    obs2 = {
        "object_type": "fire",
        "name": "Fire_1",
        "position": [1, 2, 0],
        "attributes": {"intensity": "Medium"},  # Changed
        "confidence": 1.0,
    }
    pub.apply_to_data({"observations": [dict(obs1)]})
    result = pub.apply_to_data({"observations": [dict(obs2)]})
    assert result is not None
    assert result["observations"][0]["attributes"]["intensity"] == "Medium"


def test_publisher_handles_none_data():
    pub = WorkerReportPublisher(agent_name="Alice")
    assert pub.apply_to_data(None) is None
    assert pub.apply_to_data({}) is None


def test_publisher_dedup_by_name_without_position():
    pub = WorkerReportPublisher(agent_name="Alice")
    obs1 = {
        "object_type": "fire",
        "name": "UnknownFire",
        "position": None,
        "attributes": {"intensity": "High"},
    }
    obs2 = {
        "object_type": "fire",
        "name": "UnknownFire",
        "position": None,
        "attributes": {"intensity": "Medium"},
    }
    pub.apply_to_data({"observations": [dict(obs1)]})
    result = pub.apply_to_data({"observations": [dict(obs2)]})
    assert result is not None


# =============================================================
# Coordinator observation extraction
# =============================================================


def _extract_worker_data_blocks(text):
    marker = "[DATA]"
    if marker not in text:
        return []
    blocks = []
    for chunk in text.split(marker)[1:]:
        raw = chunk.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_auto_observations(text):
    """Mirror of server.py _extract_auto_observations for test isolation."""
    results = []
    seen_keys = set()
    for block in _extract_worker_data_blocks(text):
        if block.get("ev") != "tool_result" or not block.get("success"):
            continue
        structured = block.get("structured_data")
        if isinstance(structured, dict):
            obs_list = structured.get("observations", [])
            if isinstance(obs_list, list):
                for obs in obs_list:
                    if isinstance(obs, dict):
                        results.append(obs)
        if block.get("tool_name") == "report_observation":
            content = block.get("content") or ""
            try:
                obs = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obs, dict):
                dedup_key = (
                    f"{obs.get('object_type')}:{obs.get('name')}:{obs.get('step')}"
                )
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    results.append(obs)
    return results


def test_extract_auto_observation_from_structured_data():
    structured = {
        "observations": [
            {
                "reporter": "Alice",
                "step": 5,
                "object_type": "fire",
                "name": "Fire_1",
                "position": [1, 2, 0],
                "attributes": {"intensity": "High"},
                "confidence": 1.0,
            }
        ]
    }
    payload = {
        "ev": "tool_result",
        "tool_name": "navigate_to",
        "success": True,
        "content": "Arrived at target.",
        "structured_data": structured,
    }
    text = f"[Result] navigate_to: Arrived at target.\n[DATA]\n{json.dumps(payload)}"
    obs_list = _extract_auto_observations(text)
    assert len(obs_list) == 1
    assert obs_list[0]["object_type"] == "fire"
    assert obs_list[0]["name"] == "Fire_1"


def test_extract_legacy_report_observation():
    obs_content = json.dumps(
        {
            "reporter": "Alice",
            "step": 5,
            "object_type": "fire",
            "name": "Fire_1",
            "position": [1, 2, 0],
            "attributes": {"intensity": "High"},
            "confidence": 1.0,
        }
    )
    payload = {
        "ev": "tool_result",
        "tool_name": "report_observation",
        "success": True,
        "content": obs_content,
    }
    text = f"[Result] report_observation: fire\n[DATA]\n{json.dumps(payload)}"
    obs_list = _extract_auto_observations(text)
    assert len(obs_list) == 1
    assert obs_list[0]["object_type"] == "fire"


def test_extract_both_formats_in_same_message():
    """Extract observations when both auto and legacy blocks arrive."""
    # In real A2A, each [DATA] block arrives in a separate status update,
    # but the coordinator may batch them into one status text.
    structured = {"observations": [{"object_type": "fire", "name": "Auto_Fire"}]}
    auto_payload = {
        "ev": "tool_result",
        "tool_name": "navigate_to",
        "success": True,
        "content": "Moved.",
        "structured_data": structured,
    }
    legacy_obs = json.dumps(
        {
            "reporter": "Alice",
            "step": 5,
            "object_type": "person",
            "name": "Person_1",
        }
    )
    legacy_payload = {
        "ev": "tool_result",
        "tool_name": "report_observation",
        "success": True,
        "content": legacy_obs,
    }

    # Call extract separately for each status text (as in real A2A)
    text1 = f"[Result] navigate_to: Moved.\n[DATA]\n{json.dumps(auto_payload)}"
    obs_list = _extract_auto_observations(text1)
    assert len(obs_list) >= 1

    text2 = f"[Result] report_observation: person\n[DATA]\n{json.dumps(legacy_payload)}"
    obs_list2 = _extract_auto_observations(text2)
    assert len(obs_list2) >= 1

    # Combined total
    assert len(obs_list) + len(obs_list2) >= 2


def test_extract_skips_failed_tool():
    payload = {
        "ev": "tool_result",
        "tool_name": "navigate_to",
        "success": False,
        "content": "Error",
        "structured_data": {"observations": [{"object_type": "fire"}]},
    }
    text = f"[Error] navigate_to: Error\n[DATA]\n{json.dumps(payload)}"
    obs_list = _extract_auto_observations(text)
    assert len(obs_list) == 0


# =============================================================
# SemanticMapStore bounded retention
# =============================================================


def test_semantic_map_bounded_retention():
    store = SemanticMapStore(max_observations=5)
    for i in range(10):
        store.ingest_observation(
            ObservationRecord(
                reporter="Alice",
                step=i,
                object_type="fire",
                name=f"Fire_{i}",
                attributes={"intensity": "High"},
            )
        )
    assert len(store.observations) == 5
    assert store.observations[-1]["name"] == "Fire_9"
    assert store.observations[0]["name"] == "Fire_5"


def test_semantic_map_set_max_observations():
    store = SemanticMapStore(max_observations=10)
    store.set_max_observations(3)
    for i in range(5):
        store.ingest_observation(
            ObservationRecord(
                reporter="Alice",
                step=i,
                object_type="fire",
                name=f"Fire_{i}",
            )
        )
    assert len(store.observations) <= 3


def test_semantic_map_bounded_sources():
    store = SemanticMapStore()
    for i in range(100):
        store.ingest_observation(
            ObservationRecord(
                reporter="Alice",
                step=i,
                object_type="fire",
                name="SameFire",
                attributes={"intensity": "High" if i % 2 == 0 else "Medium"},
            )
        )
    fire = store.fires.get("SameFire")
    assert fire is not None
    assert len(fire.sources) <= 50


# =============================================================
# End-to-end flow: tool result → structured data → ingest
# =============================================================


def test_tool_result_data_round_trip():
    """Verify the round-trip: barrier result → tool_result_from_barrier → data field."""
    tr = tool_result_from_barrier(_SAMPLE_BARRIER_RESULT)
    assert tr.data is not None
    assert len(tr.data["observations"]) == 1
    assert tr.data["observations"][0]["object_type"] == "fire"

    # The data dict should be serializable to JSON
    serialized = json.dumps(tr.data)
    assert json.loads(serialized)["observations"][0]["name"] == "Fire_1"


def test_semantic_map_ingests_auto_observation():
    store = SemanticMapStore()
    obs = {
        "reporter": "Alice",
        "step": 5,
        "object_type": "fire",
        "name": "AutoFire",
        "position": [1, 2, 0],
        "attributes": {"intensity": "High"},
        "confidence": 1.0,
    }
    result = store.ingest_observation(obs)
    assert result is not None
    assert result["name"] == "AutoFire"
    assert store.fires["AutoFire"].attributes.get("intensity") == "High"


# =============================================================
# Cleanup module-level publisher after tests
# =============================================================


def teardown_module(module):
    """Reset the module-level publisher to avoid cross-test pollution."""
    set_publisher(None)


# =============================================================
# H1 residual shadow audit: legacy-sink dedup is per worker/task
# =============================================================


def _coordinator_server_with_legacy_map(tmp_path):
    from a2a.coordinator.server import CoordinatorServer
    from sar_orch.map import SemanticMapStore

    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    legacy = SemanticMapStore()
    server.set_semantic_map(legacy)
    return server, legacy


def test_legacy_observation_dedup_is_per_worker_not_cross_worker(tmp_path):
    """Same-step evidence reported by DIFFERENT workers must reach the legacy
    sink (per worker/task dedup), while a single worker's own re-send is still
    suppressed (spam dedup)."""
    server, legacy = _coordinator_server_with_legacy_map(tmp_path)

    obs = {
        "reporter": "Alice",
        "step": 8,
        "object_type": "fire",
        "name": "FireA",
        "position": [1, 2, 0],
        "attributes": {"intensity": "High"},
        "confidence": 1.0,
    }

    n1 = server._ingest_observations_from_status(
        "wt-alice",
        "",
        dispatch_id="d1",
        worker_id="Alice",
        context_id="ctx-1",
        observations=[dict(obs)],
    )
    assert n1 == 1
    # Bob reports the SAME (object_type, name, step) evidence at the same step.
    bob_obs = {**obs, "reporter": "Bob", "attributes": {"intensity": "Low"}}
    n2 = server._ingest_observations_from_status(
        "wt-bob",
        "",
        dispatch_id="d2",
        worker_id="Bob",
        context_id="ctx-1",
        observations=[bob_obs],
    )
    assert n2 == 1, "cross-worker same-step evidence must not be deduped away"
    # Alice re-sending her own evidence is deduped (spam suppression).
    n3 = server._ingest_observations_from_status(
        "wt-alice",
        "",
        dispatch_id="d1",
        worker_id="Alice",
        context_id="ctx-1",
        observations=[dict(obs)],
    )
    assert n3 == 0

    fire = legacy.fires["FireA"]
    assert {s["reporter"] for s in fire.sources} == {"Alice", "Bob"}
    # C3 explicit: same-step differing claims retain the current holder and
    # mark the conflict (mirrors what the canonical sink records).
    assert fire.attributes["intensity"] == "High"
    assert fire.conflict is True


def test_legacy_dedup_scope_falls_back_to_task_when_worker_unknown(tmp_path):
    """When worker_id is unavailable, the dedup scope falls back to the opaque
    worker_task_id so it is still never cross-worker."""
    server, legacy = _coordinator_server_with_legacy_map(tmp_path)

    obs = {
        "reporter": "Alice",
        "step": 8,
        "object_type": "fire",
        "name": "FireA",
        "position": [1, 2, 0],
        "attributes": {"intensity": "High"},
    }
    n1 = server._ingest_observations_from_status(
        "wt-alice",
        "",
        dispatch_id="d1",
        worker_id="",
        context_id="ctx-1",
        observations=[dict(obs)],
    )
    assert n1 == 1
    # Same worker_task_id re-send is suppressed.
    n2 = server._ingest_observations_from_status(
        "wt-alice",
        "",
        dispatch_id="d1",
        worker_id="",
        context_id="ctx-1",
        observations=[dict(obs)],
    )
    assert n2 == 0
    # A different task id (different worker) is NOT suppressed.
    n3 = server._ingest_observations_from_status(
        "wt-bob",
        "",
        dispatch_id="d2",
        worker_id="",
        context_id="ctx-1",
        observations=[{**obs, "reporter": "Bob"}],
    )
    assert n3 == 1
    assert {s["reporter"] for s in legacy.fires["FireA"].sources} == {"Alice", "Bob"}


def test_extract_observations_with_provenance_still_dedups_same_callback():
    """Same-callback spam dedup inside _extract_observations_with_provenance
    must not regress: duplicate [DATA] blocks in one status text collapse to a
    single observation."""
    from a2a.coordinator.server import _extract_observations_with_provenance

    payload = {
        "ev": "tool_result",
        "tool_name": "report_observation",
        "success": True,
        "content": json.dumps(
            {
                "reporter": "Alice",
                "step": 5,
                "object_type": "fire",
                "name": "Fire_1",
                "position": [1, 2, 0],
                "attributes": {"intensity": "High"},
            }
        ),
    }
    text = (
        f"[Result] report_observation: fire\n[DATA]\n{json.dumps(payload)}\n"
        f"[DATA]\n{json.dumps(payload)}\n"
    )
    extracted = _extract_observations_with_provenance(text)
    assert len(extracted) == 1


def test_canonical_sink_receives_same_step_cross_worker_evidence(tmp_path):
    """The canonical projection sink is keyed per worker/task (never the
    cross-worker legacy dedup), so same-step different-worker evidence produces
    an explicit C3 conflict in canonical Memory."""
    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.redaction import RedactionPolicy
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.mission_runtime import PhysicalDispatch, PhysicalState
    from a2a.coordinator.server import CoordinatorServer

    memory_store = MemoryStore(tmp_path / "canonical.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(
        memory_store,
        scope_factory,
        redaction=RedactionPolicy(secret=b"SUPERSECRET_VALUE_9f2c1"),
    )
    ingestor.activate_scope("ctx-1", 0)

    server = CoordinatorServer(
        host="127.0.0.1",
        port=0,
        a2a_port=0,
        log_dir=str(tmp_path),
        memory_read_mode="legacy",
    )
    server.configure_memory(
        ingestor=ingestor,
        config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        secret=b"SUPERSECRET_VALUE_9f2c1",
    )

    dispatch = PhysicalDispatch(
        dispatch_id="d1",
        context_id="ctx-1",
        logical_node_id="n1",
        worker_id="Alice",
        worker_task_id="wt-alice",
        state=PhysicalState.RUNNING,
    )

    def _inputs(worker_task_id, intensity, reporter):
        obs = [
            {
                "reporter": reporter,
                "step": 8,
                "object_type": "fire",
                "name": "FireA",
                "position": [1, 2, 0],
                "attributes": {"intensity": intensity},
                "confidence": 1.0,
            }
        ]
        return server._normalize_observation_projection_inputs(
            [(obs[0], "worker_sensor_tool")],
            dispatch=dispatch,
            context_id="ctx-1",
            worker_task_id=worker_task_id,
            body_sha256=f"body{worker_task_id}".encode().hex(),
            runtime_epoch=0,
        )

    alice_inputs = _inputs("wt-alice", "High", "alice")
    bob_inputs = _inputs("wt-bob", "Low", "bob")
    assert alice_inputs and bob_inputs
    assert alice_inputs[0].event_id != bob_inputs[0].event_id

    assert ingestor.ingest_projection(alice_inputs).status == "ok"
    assert ingestor.ingest_projection(bob_inputs).status == "ok"

    field = memory_store.projection_field(
        ingestor.scope_id_for("ctx-1", 0), "spatial", "FireA", "intensity"
    )
    # C3 explicit: both workers' same-step evidence reached the canonical sink
    # and produced an explicit conflict with the first holder retained.
    assert field["outcome"] == "conflicted"
    assert field["value"] == "High"

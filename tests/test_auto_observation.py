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

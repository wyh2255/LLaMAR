"""Phase 5 render-loader compatibility: frozen fixture + real run.

Verifies that ``render_sar_report.loaders`` (``load_coordinator_events``,
``load_semantic_map`` and the frozen ``semantic_map.jsonl`` field/order
verification) reads BOTH a committed frozen fixture and a real experiment run
output — the compatibility promise is schema/field/order/read-path stability,
never "the file merely exists".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures" / "render_sar_report"
_SKILLS = Path(__file__).parent.parent / "skills" / "render-sar-report"
if str(_SKILLS) not in sys.path:
    sys.path.insert(0, str(_SKILLS))

from render_sar_report.loaders import (
    FROZEN_OBSERVATION_KEYS,
    FROZEN_SEMANTIC_MAP_TOP_KEYS,
    load_coordinator_events,
    load_semantic_map,
    verify_semantic_map_fixture,
)

_REAL_RUN = (
    Path(__file__).parent.parent
    / "sar_orch"
    / "results"
    / "read_port_semantic_s1_a2_s42_51e9e39_20260808_113934"
)


@pytest.mark.unit
class TestFrozenFixture:
    def test_frozen_semantic_map_field_and_order(self):
        result = verify_semantic_map_fixture(_FIXTURES / "semantic_map.jsonl")
        assert result["violations"] == []
        assert result["record_count"] >= 1

    def test_frozen_semantic_map_readable(self):
        objects = load_semantic_map(_FIXTURES)
        assert len(objects) == 1
        obj = objects[0]
        assert obj.name == "FireA"
        assert obj.object_type == "fire"
        assert len(obj.observations) == 4

    def test_frozen_semantic_map_key_order_locked(self):
        with (_FIXTURES / "semantic_map.jsonl").open(encoding="utf-8") as fh:
            line = json.loads(next(fh))
        assert tuple(line.keys()) == FROZEN_SEMANTIC_MAP_TOP_KEYS
        assert tuple(line["observation"].keys()) == FROZEN_OBSERVATION_KEYS

    def test_frozen_coordinator_events_readable(self):
        events = load_coordinator_events(_FIXTURES)
        assert len(events) == 3
        assert {e.event_type for e in events} == {"assign_task", "send_message"}
        assert all(e.agent == "Coordinator" for e in events)


@pytest.mark.unit
class TestRealRun:
    @pytest.mark.skipif(
        not _REAL_RUN.is_dir(),
        reason="real run dir is gitignored and absent in a fresh checkout",
    )
    def test_real_run_semantic_map_readable(self):
        objects = load_semantic_map(_REAL_RUN)
        assert len(objects) > 0
        assert all(o.name and o.object_type for o in objects)

    @pytest.mark.skipif(
        not _REAL_RUN.is_dir(),
        reason="real run dir is gitignored and absent in a fresh checkout",
    )
    def test_real_run_coordinator_events_readable(self):
        events = load_coordinator_events(_REAL_RUN)
        assert len(events) > 0

    @pytest.mark.skipif(
        not _REAL_RUN.is_dir(),
        reason="real run dir is gitignored and absent in a fresh checkout",
    )
    def test_real_run_semantic_map_readable_by_fixture_contract(self):
        result = verify_semantic_map_fixture(_REAL_RUN / "semantic_map.jsonl")
        # Field/order violations are fatal for the compatibility promise.
        assert result["violations"] == []
        assert result["record_count"] > 0

"""Regression: CoordinatorContextManager._format_fire crashes when
attributes.regions is a list of dicts.

Reproduced in a real run (sar_orch/results/20260804_143029_eval_diag_smoke/
s4_s42_a2_r1): `', '.join(regions)` in context.py assumed regions was a list
of strings, but sar_orch/map_agent/tools.py:_build_fire_result builds it as
a list of {"name", "position"} dicts, and attributes.regions is otherwise
LLM-authored free-form input via report_observation with no enforced shape.
Either shape must render without raising TypeError.
"""

from __future__ import annotations

import time
from typing import Any

from Agent.router_agent.context import ContextConfig, CoordinatorContextManager
from Agent.router_agent.state_provider import RuntimeState


def _build_context(fires: list[dict[str, Any]]) -> CoordinatorContextManager:
    payload = {
        "step_budget": {"current_step": 1, "max_steps": 50, "remaining": 49},
        "mission_finished": False,
        "recent_changes": [],
        "supervision": {"alerts": [], "unacknowledged_events": []},
        "state_mode": "semantic",
        "semantic_summary": {
            "known_dynamic_objects": {"fires": fires, "persons": []},
            "known_priors": {"reservoirs": [], "deposits": []},
        },
        "team_status_summary": {"workers": []},
    }

    class FixedStateProvider:
        def snapshot(self, context_id=None):
            return RuntimeState(
                version=1, env_step=1, observed_at=time.monotonic(), payload=payload
            )

    config = ContextConfig()
    config.state_mode = "semantic"
    ctx = CoordinatorContextManager(config=config, token_limit=80000)
    ctx._state_provider = FixedStateProvider()
    ctx.refresh_runtime_state()
    return ctx


def test_format_fire_with_dict_regions_does_not_raise():
    fires = [
        {
            "name": "RedFire",
            "position": [11, 4, 0],
            "attributes": {
                "type": "a",
                "intensity": "HIGH",
                "regions": [
                    {"name": "RedFire_region_1", "position": [11, 4, 0]},
                    {"name": "RedFire_region_2", "position": [12, 4, 0]},
                ],
            },
        }
    ]
    ctx = _build_context(fires)
    rendered = ctx._render_memory_block()
    assert "RedFire" in rendered
    assert "RedFire_region_1" in rendered
    assert "RedFire_region_2" in rendered


def test_format_fire_with_string_regions_still_works():
    fires = [
        {
            "name": "CaldorFire",
            "position": [2, 2, 0],
            "attributes": {"type": "a", "regions": ["north", "south"]},
        }
    ]
    ctx = _build_context(fires)
    rendered = ctx._render_memory_block()
    assert "CaldorFire" in rendered
    assert "north" in rendered
    assert "south" in rendered


def test_format_fire_with_scalar_regions_does_not_raise():
    fires = [
        {
            "name": "GreatFire",
            "position": [20, 16, 0],
            "attributes": {"type": "b", "regions": "single-region"},
        }
    ]
    ctx = _build_context(fires)
    rendered = ctx._render_memory_block()
    assert "GreatFire" in rendered

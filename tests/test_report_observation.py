import json

import pytest

from sar_orch.tools.worker.report_observation import ReportObservationTool


@pytest.mark.asyncio
async def test_report_observation_returns_structured_payload_without_pausing():
    tool = ReportObservationTool(
        agent_name="Alice", task_id="alice-task", get_step=lambda: 9
    )

    result = await tool.execute(
        object_type="fire",
        name="CaldorFire_Region_1",
        position=[4, 4, 0],
        attributes={"fire_type": "Chemical", "intensity": "Medium", "status": "active"},
        confidence=1.0,
        note="Visible from current location",
    )

    payload = json.loads(result.content)

    assert result.success is True
    assert payload["reporter"] == "Alice"
    assert payload["source_task_id"] == "alice-task"
    assert payload["step"] == 9
    assert payload["object_type"] == "fire"
    assert payload["attributes"]["fire_type"] == "Chemical"
    assert "INPUT_REQUIRED" not in result.content

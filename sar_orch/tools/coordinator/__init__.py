"""Coordinator tools for SAR orchestration."""

from sar_orch.tools.coordinator.finish_task import FinishTaskTool
from sar_orch.tools.coordinator.query_sar_state import QuerySARStateTool
from sar_orch.tools.coordinator.query_semantic_map import QuerySemanticMapTool
from sar_orch.tools.coordinator.query_team_status import QueryTeamStatusTool
from a2a.builtin_tools.send_message import SendMessageTool

# Diagnosis-loop read tools: registered by sar_orch.diagnosis_loop (not part of
# this package's __all__), imported here so the descriptions overlay below
# covers every SAR coordinator tool whatever the import path.
from sar_orch.tools.coordinator.query_control_journal import QueryControlJournalTool
from sar_orch.tools.coordinator.query_projection import QueryProjectionTool
from sar_orch.tools.coordinator.query_supervision import QuerySupervisionTool
from sar_orch.tools.coordinator.query_temporal_flow import QueryTemporalFlowTool
from sar_orch.tools.descriptions_loader import apply_description_overlays

__all__ = [
    "FinishTaskTool",
    "QuerySARStateTool",
    "QuerySemanticMapTool",
    "QueryTeamStatusTool",
    "SendMessageTool",
]

# reef §5.2 descriptions overlay: sar_orch/tools/descriptions.json replaces the
# inline `description` / `parameters` of every SAR tool class.  Fail-soft — a
# missing file / key keeps the inline values, so the historical behaviour is
# unchanged.  SendMessageTool is a kernel tool, not part of the SAR overlay.
apply_description_overlays(
    [
        FinishTaskTool,
        QuerySARStateTool,
        QuerySemanticMapTool,
        QueryTeamStatusTool,
        QueryControlJournalTool,
        QueryProjectionTool,
        QuerySupervisionTool,
        QueryTemporalFlowTool,
    ]
)

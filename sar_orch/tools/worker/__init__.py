"""SAR worker tools for LLaMAR — exported as SAR_WORKER_TOOLS."""

from sar_orch.tools.worker.navigate_to import NavigateToTool
from sar_orch.tools.worker.move import MoveTool
from sar_orch.tools.worker.explore import ExploreTool
from sar_orch.tools.worker.carry_person import CarryPersonTool
from sar_orch.tools.worker.drop_off_person import DropOffPersonTool
from sar_orch.tools.worker.get_supply import GetSupplyTool
from sar_orch.tools.worker.get_agent_state import GetAgentStateTool
from sar_orch.tools.worker.store_supply import StoreSupplyTool
from sar_orch.tools.worker.use_supply import UseSupplyTool
from sar_orch.tools.worker.clear_inventory import ClearInventoryTool
from sar_orch.tools.worker.finish_task import FinishTaskTool
from sar_orch.tools.worker.no_op import NoOpTool
from sar_orch.tools.worker.report_observation import ReportObservationTool
from a2a.worker.tools.ask_coordinator import AskCoordinatorTool
from a2a.worker.tools.read_mailbox import ReadMailboxTool
from a2a.worker.tools.send_peer_mail import A2ASendMailTool

# Deprecated (superseded by the Map Agent MCP tools) and therefore not in
# SAR_WORKER_TOOLS, but still a SAR tool module: imported so the descriptions
# overlay below stays in sync with sar_orch/tools/descriptions.json.
from sar_orch.tools.worker.query_shared_memory import QuerySharedMemoryTool
from sar_orch.tools.descriptions_loader import apply_description_overlays

SAR_WORKER_TOOLS = [
    NavigateToTool,
    MoveTool,
    ExploreTool,
    CarryPersonTool,
    DropOffPersonTool,
    GetSupplyTool,
    GetAgentStateTool,
    StoreSupplyTool,
    UseSupplyTool,
    ClearInventoryTool,
    ReportObservationTool,
    NoOpTool,
    FinishTaskTool,
    AskCoordinatorTool,
    ReadMailboxTool,
    A2ASendMailTool,
]

# reef §5.2 descriptions overlay: sar_orch/tools/descriptions.json replaces the
# inline `description` / `parameters` of every SAR tool class.  Fail-soft — a
# missing file / key keeps the inline values, so the historical behaviour is
# unchanged.  Only classes defined under sar_orch/tools/ are overlay targets;
# the kernel tools registered here (AskCoordinator / ReadMailbox / A2ASendMail)
# are passed through untouched.
_SAR_TOOL_CLASSES = [
    cls for cls in [*SAR_WORKER_TOOLS, QuerySharedMemoryTool]
    if cls.__module__.startswith("sar_orch.tools.")
]
apply_description_overlays(_SAR_TOOL_CLASSES)

__all__ = [
    "SAR_WORKER_TOOLS",
    "NavigateToTool",
    "MoveTool",
    "ExploreTool",
    "CarryPersonTool",
    "DropOffPersonTool",
    "GetSupplyTool",
    "GetAgentStateTool",
    "StoreSupplyTool",
    "UseSupplyTool",
    "ClearInventoryTool",
    "ReportObservationTool",
    "NoOpTool",
    "FinishTaskTool",
    "AskCoordinatorTool",
    "ReadMailboxTool",
    "A2ASendMailTool",
]

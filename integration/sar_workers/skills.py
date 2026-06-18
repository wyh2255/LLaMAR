"""SAR Skill definitions — marries tools into coordinator-discoverable capabilities."""
import sys
from pathlib import Path

# Import Skill — bypass a2a_lib/__init__.py to avoid ROS deps
_a2a_lib_dir = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib/a2a_lib")
if str(_a2a_lib_dir) not in sys.path:
    sys.path.insert(0, str(_a2a_lib_dir))

from skill import Skill
from integration.sar_workers.tools import (
    navigate_to, move, explore,
    carry_person, drop_off_person,
    get_supply, store_supply, use_supply, clear_inventory,
    no_op,
)


FIREFIGHTING_SKILL = Skill(
    name="firefighting",
    description="Extinguish fires by navigating to them and using appropriate supplies (water for non-chemical, sand for chemical)",
    tools=["navigate_to", "move", "get_supply", "use_supply", "clear_inventory"],
    examples=[
        "Navigate to WaterSource_1, get water, then go to GreatFire_Region_1 and use the water on it",
        "Get sand from SandReservoir_1, navigate to ChemicalFire_Region_1, use sand on it",
    ],
)

RESCUE_SKILL = Skill(
    name="rescue",
    description="Rescue trapped persons by carrying them (requires 2+ agents) and dropping them at a deposit",
    tools=["navigate_to", "move", "carry_person", "drop_off_person"],
    examples=[
        "Go to LostTimmy, carry them, then drop them off at Deposit_1",
        "Navigate to LostTimmy and wait for another agent before carrying",
    ],
)

SUPPLY_CHAIN_SKILL = Skill(
    name="supply_chain",
    description="Manage supplies — collect from reservoirs, store at deposits for other agents",
    tools=["navigate_to", "move", "get_supply", "store_supply", "clear_inventory"],
    examples=[
        "Get water from WaterSource_1 and store it at Deposit_1 for the firefighter",
    ],
)

EXPLORATION_SKILL = Skill(
    name="exploration",
    description="Explore unknown areas to discover fires, persons, or resources",
    tools=["move", "explore"],
    examples=[
        "Explore the area to find undiscovered fires",
    ],
)

SAR_SKILLS = [FIREFIGHTING_SKILL, RESCUE_SKILL, SUPPLY_CHAIN_SKILL, EXPLORATION_SKILL]

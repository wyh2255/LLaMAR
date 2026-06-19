"""
SAR Skill definitions — marries tools into coordinator-discoverable capabilities.

SAR 技能定义 — 将底层工具（tools）封装为协调器（coordinator）可发现、可调用的技能单元。
每个技能代表一种独立的任务能力（灭火、救援、物资供应、探索），包含完成该能力所需的工具集合。
"""
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


# 灭火技能：导航到火场并使用合适的灭火物资（水用于普通火灾，沙用于化学火灾）
# 包含工具：导航、移动、获取物资、使用物资、清空背包
FIREFIGHTING_SKILL = Skill(
    name="firefighting",
    description="Extinguish fires by navigating to them and using appropriate supplies (water for non-chemical, sand for chemical)",
    tools=["navigate_to", "move", "get_supply", "use_supply", "clear_inventory"],
    examples=[
        "Navigate to WaterSource_1, get water, then go to GreatFire_Region_1 and use the water on it",
        "Get sand from SandReservoir_1, navigate to ChemicalFire_Region_1, use sand on it",
    ],
)

# 救援技能：搬运被困人员（需要 2 个及以上智能体协作）并送到存放点放下
# 包含工具：导航、移动、搬运人员、放下人员
RESCUE_SKILL = Skill(
    name="rescue",
    description="Rescue trapped persons by carrying them (requires 2+ agents) and dropping them at a deposit",
    tools=["navigate_to", "move", "carry_person", "drop_off_person"],
    examples=[
        "Go to LostTimmy, carry them, then drop them off at Deposit_1",
        "Navigate to LostTimmy and wait for another agent before carrying",
    ],
)

# 供应链技能：管理物资供应 — 从资源点收集物资，存入存放点为其他智能体备用
# 包含工具：导航、移动、获取物资、存储物资、清空背包
SUPPLY_CHAIN_SKILL = Skill(
    name="supply_chain",
    description="Manage supplies — collect from reservoirs, store at deposits for other agents",
    tools=["navigate_to", "move", "get_supply", "store_supply", "clear_inventory"],
    examples=[
        "Get water from WaterSource_1 and store it at Deposit_1 for the firefighter",
    ],
)

# 探索技能：探索未知区域以发现火源、被困人员或资源点
# 包含工具：移动、探索
EXPLORATION_SKILL = Skill(
    name="exploration",
    description="Explore unknown areas to discover fires, persons, or resources",
    tools=["move", "explore"],
    examples=[
        "Explore the area to find undiscovered fires",
    ],
)

# SAR 全部技能列表：提供给协调器注册所有可用技能
# 协调器据此了解每个智能体（agent）具备的能力，从而合理分配子任务
SAR_SKILLS = [FIREFIGHTING_SKILL, RESCUE_SKILL, SUPPLY_CHAIN_SKILL, EXPLORATION_SKILL]

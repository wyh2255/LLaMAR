"""SAR domain @tool functions — each formats a LLaMAR action string and submits via barrier.
SAR 领域 @tool 函数 —— 每个函数格式化 LLaMAR 动作字符串并通过屏障提交。

All tools follow the same pattern:
所有工具遵循相同模式：
  1. Format LLaMAR action string
     格式化 LLaMAR 动作字符串
  2. await node._barrier.submit_action(node._agent_idx, action)
     通过屏障提交动作
  3. Return the observation text for the LLM to reason about
     返回观测文本供 LLM 推理使用
"""
from __future__ import annotations

from integration._maros_compat import tool


# ── Movement ────────────────────────────────────────────────────────────────
# ── 移动 ────────────────────────────────────────────────────────────────────


@tool(name="navigate_to", description="Navigate to an object by its ID. Use this to move toward any visible object.")
async def navigate_to(node, target_id: str) -> str:
    """Navigate to the specified object in the SAR grid.
    导航到 SAR 网格中指定的物体。

    Args:
        target_id: ID of the target object (e.g., "WaterSource_1", "GreatFire_Region_1")
                   目标物体的 ID（例如 "WaterSource_1", "GreatFire_Region_1"）
    """
    # 格式化 NavigateTo 动作字符串并提交
    action = f"NavigateTo({target_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="move", description="Move one step in a cardinal direction or diagonal.")
async def move(node, direction: str) -> str:
    """Move the agent one step in the specified direction.
    将智能体向指定方向移动一步。

    Args:
        direction: Direction to move — Up, Down, Left, Right, UpLeft, UpRight, DownLeft, DownRight, Center
                   移动方向 —— Up, Down, Left, Right, UpLeft, UpRight, DownLeft, DownRight, Center
    """
    # 格式化 Move 动作字符串并提交
    action = f"Move({direction})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="explore", description="Explore unknown surrounding area. Multiple steps at once.")
async def explore(node) -> str:
    """Explore the surrounding area by moving in multiple directions.
    通过向多个方向移动来探索周围区域。

    Use this when you cannot see objects you expect nearby or need to discover new fires/people.
    当你看不到预期的附近物体或需要发现新的火灾/人员时使用此工具。
    """
    # 格式化 Explore 动作字符串并提交
    action = "Explore()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Person Rescue ───────────────────────────────────────────────────────────
# ── 人员救援 ────────────────────────────────────────────────────────────────


@tool(name="carry_person", description="Pick up a trapped person. At least 2 agents must carry simultaneously to succeed.")
async def carry_person(node, person_id: str) -> str:
    """Pick up a trapped person. Requires coordination with other agents.
    拾起被困人员。需要与其他智能体协调配合。

    Args:
        person_id: ID of the person to carry (e.g., "LostTimmy")
                   要搬运的人员 ID（例如 "LostTimmy"）
    """
    # 格式化 Carry 动作字符串并提交（至少需要 2 个智能体同时执行）
    action = f"Carry({person_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="drop_off_person", description="Drop a carried person at a safe deposit location.")
async def drop_off_person(node, person_id: str, deposit_id: str) -> str:
    """Drop a carried person at a deposit. All carrying agents must be at the deposit.
    将被搬运的人员放到存放点。所有搬运智能体必须都在存放点位置。

    Args:
        person_id: ID of the person being carried
                   被搬运的人员 ID
        deposit_id: ID of the deposit to drop them at
                   投放存放点的 ID
    """
    # 格式化 DropOff 动作字符串并提交（所有搬运者必须在同一位置）
    action = f"DropOff({deposit_id}, {person_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Supply Management ───────────────────────────────────────────────────────
# ── 物资管理 ────────────────────────────────────────────────────────────────


@tool(name="get_supply", description="Collect firefighting supplies from a reservoir or deposit.")
async def get_supply(node, source_id: str, supply_type: str) -> str:
    """Get firefighting supply from a source.
    从资源源获取消防物资。

    Args:
        source_id: ID of the reservoir or deposit to get supply from
                   要获取物资的资源源或存放点的 ID
        supply_type: Type of supply — "Water" or "Sand"
                    物资类型 —— "Water" 或 "Sand"
    """
    # 根据来源类型格式化不同参数的 GetSupply 动作
    if "reservoir" in source_id.lower():
        # 从资源源获取：不需要指定物资类型
        action = f"GetSupply({source_id})"
    else:
        # 从存放点获取：需要指定物资类型
        action = f"GetSupply({source_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="store_supply", description="Store your current supplies at a deposit.")
async def store_supply(node, deposit_id: str) -> str:
    """Store all carried supplies at a deposit.
    将当前携带的所有物资存储到存放点。

    Args:
        deposit_id: ID of the deposit to store supplies at
                   存储物资的存放点 ID
    """
    # 格式化 StoreSupply 动作字符串并提交
    action = f"StoreSupply({deposit_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="use_supply", description="Use firefighting supply on a fire to extinguish it.")
async def use_supply(node, fire_id: str, supply_type: str) -> str:
    """Use carried supply on a fire to reduce its intensity.
    使用携带的物资灭火，降低火势强度。

    Args:
        fire_id: ID of the fire to extinguish (use _Region suffix, e.g., "GreatFire_Region_1")
                 要扑灭的火灾 ID（使用 _Region 后缀，例如 "GreatFire_Region_1"）
        supply_type: Type of supply to use — "Water" or "Sand"
                    使用的物资类型 —— "Water" 或 "Sand"
    """
    # 格式化 UseSupply 动作字符串并提交
    action = f"UseSupply({fire_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="clear_inventory", description="Clear your entire inventory (drop all carried items).")
async def clear_inventory(node) -> str:
    """Drop all items from your inventory.
    丢弃库存中所有物品。

    清空当前智能体携带的所有物资。
    """
    # 格式化 ClearInventory 动作字符串并提交
    action = "ClearInventory()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Meta ────────────────────────────────────────────────────────────────────
# ── 元操作 ──────────────────────────────────────────────────────────────────


@tool(name="no_op", description="Do nothing this step. Use when waiting for other agents or when your task is complete.")
async def no_op(node) -> str:
    """Take no action this step.
    当前步骤不执行任何操作。

    在等待其他智能体或任务完成时使用。
    """
    # 格式化 NoOp 动作字符串并提交（无具体动作）
    action = "NoOp"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Tool registry ───────────────────────────────────────────────────────────
# ── 工具注册表 ──────────────────────────────────────────────────────────────

# 所有 SAR 工具的注册列表，供 MARoS 框架加载使用
SAR_TOOLS = [
    navigate_to,      # 导航到指定物体
    move,             # 向指定方向移动一步
    explore,          # 探索周围未知区域
    carry_person,     # 拾起被困人员（需多智能体协作）
    drop_off_person,  # 将被搬运人员投放至存放点
    get_supply,       # 从资源源或存放点获取消防物资
    store_supply,     # 将物资存储到存放点
    use_supply,       # 使用物资灭火
    clear_inventory,  # 清空库存
    no_op,            # 无操作（等待或完成任务时使用）
]

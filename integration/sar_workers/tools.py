"""SAR domain @tool functions — each formats a LLaMAR action string and submits via barrier.

All tools follow the same pattern:
  1. Format LLaMAR action string
  2. await node._barrier.submit_action(node._agent_idx, action)
  3. Return the observation text for the LLM to reason about
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import MARoS @tool decorator — bypass a2a_lib/__init__.py to avoid ROS deps
_a2a_lib_dir = Path("/home/wyh/daily_work/MARoS/maros_ws/a2a_lib/a2a_lib")
if str(_a2a_lib_dir) not in sys.path:
    sys.path.insert(0, str(_a2a_lib_dir))

import tool_decorator
tool = tool_decorator.tool


# ── Movement ────────────────────────────────────────────────────────────────


@tool(name="navigate_to", description="Navigate to an object by its ID. Use this to move toward any visible object.")
async def navigate_to(node, target_id: str) -> str:
    """Navigate to the specified object in the SAR grid.

    Args:
        target_id: ID of the target object (e.g., "WaterSource_1", "GreatFire_Region_1")
    """
    action = f"NavigateTo({target_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="move", description="Move one step in a cardinal direction or diagonal.")
async def move(node, direction: str) -> str:
    """Move the agent one step in the specified direction.

    Args:
        direction: Direction to move — Up, Down, Left, Right, UpLeft, UpRight, DownLeft, DownRight, Center
    """
    action = f"Move({direction})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="explore", description="Explore unknown surrounding area. Multiple steps at once.")
async def explore(node) -> str:
    """Explore the surrounding area by moving in multiple directions.

    Use this when you cannot see objects you expect nearby or need to discover new fires/people.
    """
    action = "Explore()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Person Rescue ───────────────────────────────────────────────────────────


@tool(name="carry_person", description="Pick up a trapped person. At least 2 agents must carry simultaneously to succeed.")
async def carry_person(node, person_id: str) -> str:
    """Pick up a trapped person. Requires coordination with other agents.

    Args:
        person_id: ID of the person to carry (e.g., "LostTimmy")
    """
    action = f"Carry({person_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="drop_off_person", description="Drop a carried person at a safe deposit location.")
async def drop_off_person(node, person_id: str, deposit_id: str) -> str:
    """Drop a carried person at a deposit. All carrying agents must be at the deposit.

    Args:
        person_id: ID of the person being carried
        deposit_id: ID of the deposit to drop them at
    """
    action = f"DropOff({person_id}, {deposit_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Supply Management ───────────────────────────────────────────────────────


@tool(name="get_supply", description="Collect firefighting supplies from a reservoir or deposit.")
async def get_supply(node, source_id: str, supply_type: str) -> str:
    """Get firefighting supply from a source.

    Args:
        source_id: ID of the reservoir or deposit to get supply from
        supply_type: Type of supply — "Water" or "Sand"
    """
    action = f"GetSupply({source_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="store_supply", description="Store your current supplies at a deposit.")
async def store_supply(node, deposit_id: str) -> str:
    """Store all carried supplies at a deposit.

    Args:
        deposit_id: ID of the deposit to store supplies at
    """
    action = f"StoreSupply({deposit_id})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="use_supply", description="Use firefighting supply on a fire to extinguish it.")
async def use_supply(node, fire_id: str, supply_type: str) -> str:
    """Use carried supply on a fire to reduce its intensity.

    Args:
        fire_id: ID of the fire to extinguish (use _Region suffix, e.g., "GreatFire_Region_1")
        supply_type: Type of supply to use — "Water" or "Sand"
    """
    action = f"UseSupply({fire_id}, {supply_type})"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


@tool(name="clear_inventory", description="Clear your entire inventory (drop all carried items).")
async def clear_inventory(node) -> str:
    """Drop all items from your inventory."""
    action = "ClearInventory()"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Meta ────────────────────────────────────────────────────────────────────


@tool(name="no_op", description="Do nothing this step. Use when waiting for other agents or when your task is complete.")
async def no_op(node) -> str:
    """Take no action this step."""
    action = "NoOp"
    result = await node._barrier.submit_action(node._agent_idx, action)
    return result["observation"]


# ── Tool registry ───────────────────────────────────────────────────────────


SAR_TOOLS = [
    navigate_to,
    move,
    explore,
    carry_person,
    drop_off_person,
    get_supply,
    store_supply,
    use_supply,
    clear_inventory,
    no_op,
]

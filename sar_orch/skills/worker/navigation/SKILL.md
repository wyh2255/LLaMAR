---
name: navigation
description: Efficient navigation patterns — teleport confirmation, multi-step path planning, and position verification.
---

# Skill: Navigation

How to navigate the grid environment efficiently.

## NavigateTo Is Teleport

`navigate_to(target)` moves you instantly to the target position. **One call is enough.** Do NOT call it multiple times for the same target.

## Verifying Your Position

After navigating:
- Your position is automatically shown in the Context Memory block's Current State section
- Use `get_agent_state()` only if the Context Memory seems stale or you need more detail
- Position is displayed as `(x, y, z)` — format always matches

## Multi-Step Planning

When you have a task with multiple destinations, plan the shortest path:
1. Check your current position from Context Memory
2. Check target positions from Context Memory
3. Navigate in logical order: closest first, then farther

## Common Mistakes

- ❌ Navigating to a fire center instead of the specific region — always use the full region name
- ❌ Navigating to a person who's already being carried — check Context Memory for rescue status
- ❌ Teleporting to a reservoir and immediately teleporting again before collecting — collect first!
- ✅ After `navigate_to`, always read the observation. It confirms arrival at `(x, y, z)`.

---
name: fire-suppression
description: Systematic approach for allocating agents to suppress fires efficiently, considering fire types, reservoir proximity, and spread dynamics.
---

# Skill: Fire Suppression

A structured strategy for the coordinator to systematically suppress fires across the environment.

## Phase 1: Assess

Before dispatching firefighting tasks, identify:
- **Fire types**: Chemical fires need **Sand**; Non-chemical fires need **Water**. The supply type MUST match the fire type — a wrong type does nothing and still wastes the unit and the step.
- **Fire regions**: Each fire has multiple regions (e.g. CaldorFire_Region_1, Region_2). ALL regions must be extinguished.
- **Intensity**: `none` → `low` → `medium` → `high`. At `medium`, fires spread to neighbors.
- **Fire sources**: Regions 1, 2, … are fire sources. Extinguish sources first to stop progression.

## Phase 2: Prioritize

Rank fires by urgency (this ranking is for fires only — for whether a located rescue should interrupt firefighting, see "Assignment order — containment > rescue > mop-up" in the coordinator system prompt):
1. **Uncontained fires** (medium/high intensity) — they spread and become unrecoverable. Priority #1, always, including over a located rescue.
2. **Fire sources** (Region_1, Region_2) — must be extinguished before all regions can be cleared. Priority #2.
3. **Contained fires still burning** (low intensity, not yet `none`) — mop-up. Lowest priority; yields to a located rescue.

## Phase 3: Assign

For each fire, decide:
- **Chemical fire** → assign agents to collect **Sand** from Sand reservoir
- **Non-chemical fire** → assign agents to collect **Water** from Water reservoir (Sand does NOT work on non-chemical fires)
- **Closest reservoir** → check agent positions from Environment State, assign the nearest agent
- **Full load per trip** — tell agents to fill all 3 inventory slots; each burning cell costs ~1 unit per intensity notch, and every extra reservoir round-trip costs 4+ steps while the fire re-intensifies

Give each agent a complete action chain:
```
NavigateTo(Reservoir_X) → GetSupply(Reservoir_X) → NavigateTo(Fire_Region_N) → UseSupply(Fire_Region_N) → [repeat if more supplies needed] → NoOp()
```

## Phase 4: Monitor

After dispatching:
- Check task results. If `FAILED` (e.g. agent couldn't find the region), re-dispatch with corrected coordinates.
- If fire intensity hasn't dropped, check: wrong supply type? Agent at wrong position?
- **Verify with the average intensity**: a fire is out ONLY when its average intensity reads `none` in Context Memory / worker observations. Never trust a worker's "extinguished" report alone — a worker sees only its local surroundings. If the average is `low` or higher, keep an agent assigned to that fire.
- Re-prioritize as new fires are discovered or existing fires escalate.

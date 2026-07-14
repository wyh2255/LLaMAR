---
name: exploration
description: Efficient grid search patterns to maximize coverage with limited step budget, with early-exit conditions when no new objects are found.
---

# Skill: Exploration

Strategy for efficient grid exploration when fire/person locations are unknown.

## Exploration Pattern

The grid is a 2D plane with z=0. Goal: discover fires, persons, reservoirs, and deposits.

### Option A: Sweep Pattern (recommended for small grids)
Divide the grid into lanes. Assign each agent a lane. Workers call `explore()` which reveals their surroundings. Coverage overlaps are wasteful — assign non-overlapping zones.

### Option B: Hotspot-first (when partial info exists)
If some fires/persons are known but incomplete, dispatch explore agents to:
- Known fire-adjacent cells (fires may have spread)
- Cells that haven't been visited yet, prioritized by distance from known objects

## Early-Exit Condition

A worker's exploration subtask is complete when:
- The worker calls `explore` for 3 consecutive steps and discovers **no new** fires, persons, reservoirs, or deposits.
- The worker has covered its assigned zone.

When ALL exploration tasks for all agents complete with no new discoveries → transition to firefighting/rescue.

## Step Budget Awareness

- **Early exploration** (steps 1-10): Explore aggressively. Every unknown cell is a potential fire.
- **Mid mission** (steps 11-30): Balance exploration with firefighting. Dispatch 1 explorer, rest fight fires.
- **Late mission** (steps 30+): Skip exploration unless step budget is generous. Focus on extinguishing known fires.
- **Very late** (<10 steps remaining): Only explore if you suspect critical undiscovered fires/persons. Otherwise, maximize known-object handling.

## Reporting

When a worker discovers a new fire or person via `report_observation`, update your mental map. Re-prioritize based on what was found.

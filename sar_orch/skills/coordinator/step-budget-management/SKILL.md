---
name: step-budget-management
description: Adaptive prioritization strategy when remaining steps are tight — when to shift from exploration to firefighting to rescue, and how to allocate agents per phase.
---

# Skill: Step Budget Management

How to allocate the remaining step budget across the mission phases.

## Phase Transition Thresholds

| Remaining Steps | Primary Focus | Agent Allocation |
|----------------|---------------|-----------------|
| 50-35 | Full exploration + firefighting | 1-2 explore, rest fight fires |
| 35-20 | Firefighting + rescue prep | All on firefighting, scout for rescue routes |
| 20-10 | Rescue + mop-up fires | Rescue persons, extinguish remaining fires |
| <10 | Emergency mode | Rescue persons if any remain; suppress spreading fires only |

## Resource Estimation

Each fire region takes 2 steps per agent (NavigateTo + UseSupply) if the agent already has supplies, or 4 steps (NavigateTo Reservoir → GetSupply → NavigateTo Fire → UseSupply).

A person rescue takes at minimum:
- 2 steps per carrier (NavigateTo + CarryPerson) if at person
- + 2 steps (NavigateTo Deposit + DropOffPerson)
- = 4 steps per carrier, with 2+ carriers = 8+ total agent-steps

## When Budget Is Tight (<20 steps)

1. **Stop all exploration** — only explore if you have zero fires/persons known
2. **Reuse agents with existing supplies** — don't waste steps sending them back to reservoir
3. **Cancel long-running explore tasks** immediately
4. **If persons exist and fires are under control**, dedicate ALL agents to rescue
5. **If fires are spreading** (medium+ intensity near persons), prioritize fire suppression

## Emergency Mode (<10 steps)

- Do NOT send agents to reservoirs — they won't have time to return
- Use whatever supplies agents already have
- If a person is at the deposit but carriers haven't dropped off, ensure EVERY carrier calls `drop_off_person()` — dispatch the explicit instruction; calls are cumulative and do not need to be in the same step
- Accept partial completion — putting out source fires is better than nothing

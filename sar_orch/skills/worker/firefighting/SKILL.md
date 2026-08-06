---
name: firefighting
description: How to fight fires effectively — correct supply selection, fire region identification, intensity management, and resource conservation.
---

# Skill: Firefighting

How to execute firefighting tasks efficiently as a worker agent.

## Fire Type Identification

When you receive a firefighting task, check:
- **Chemical fire** → needs **Sand** from a Sand reservoir
- **Non-chemical fire** → can use **Water** (preferred) or **Sand**
- If unsure about the type, check your surroundings in Environment State or use `get_agent_state()`

## Supply Collection

1. Navigate to the correct reservoir type (check reservoir contents with get_agent_state or Environment State)
2. Call `get_supply(reservoir_id="...")` — collects 1 unit per call
3. Your inventory can hold up to 3 slots. Don't over-collect unless you need multiple units.

## Fire Region Navigation

Each fire has named regions (e.g. `CaldorFire_Region_1`, `CaldorFire_Region_2`). Navigate to the **specific region**, not the fire name:

- ✅ `navigate_to(target="CaldorFire_Region_1")` — correct
- ❌ `navigate_to(target="CaldorFire")` — wrong

## Using Supplies

1. Navigate to the fire region cell
2. Call `use_supply()` — consumes 1 supply unit, lowers intensity by one notch
3. Repeat if the fire isn't out yet (check observation text)
4. If you run out of supplies, go back to reservoir and get more

## Efficiency Tips

- Use `get_agent_state()` to confirm your position and inventory when unsure
- Don't call `navigate_to` twice for the same target — it's instant teleport, one call is enough
- If intensity doesn't decrease after use_supply, you may have the wrong supply type
- Fire sources (Region_1, Region_2, …) must be extinguished before other regions

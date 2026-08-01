---
name: firefighting
description: How to fight fires effectively — correct supply selection, fire region identification, intensity management, and resource conservation.
---

# Skill: Firefighting

How to execute firefighting tasks efficiently as a worker agent.

## Fire Type Identification

When you receive a firefighting task, check:
- **Chemical fire** → needs **Sand** from a Sand reservoir
- **Non-chemical fire** → needs **Water** from a Water reservoir
- **The supply type MUST match the fire type** — a wrong type does nothing and still consumes the unit and the step. If `use_supply` reports failure or intensity doesn't drop, your supply type is wrong.
- If unsure about the type, check your surroundings in Context Memory or use `get_agent_state()`

## Supply Collection

1. Navigate to the correct reservoir type (check reservoir contents with get_agent_state or Context Memory)
2. Call `get_supply(reservoir_id="...")` — collects 1 unit per call
3. Size your load to the fire: each burning cell needs ~1 unit per intensity notch (low≈1, medium≈2, high≈3). Default to filling all 3 slots — an unused unit costs nothing, but an extra reservoir round-trip costs 4+ steps and the fire re-intensifies while you travel.

## Fire Region Navigation

Each fire has named regions (e.g. `CaldorFire_Region_1`, `CaldorFire_Region_2`). Navigate to the **specific region**, not the fire name:

- ✅ `navigate_to(target="CaldorFire_Region_1")` — correct
- ❌ `navigate_to(target="CaldorFire")` — wrong

## Using Supplies

1. Navigate to the fire region cell
2. Call `use_supply()` — consumes 1 supply unit, lowers intensity by one notch
3. Repeat if the fire isn't out yet (check observation text)
4. **Stay on the fire ground** until every region you can see reads `none` OR your matching supply runs out. Fires re-intensify and spread while you walk away — leaving early means paying double later.
5. **Verify with the average**: a fire is extinguished only when its **average intensity is `none`** in your global observation. If the average is `low`+, keep fighting or `report_observation()` that it is still burning. Never declare extinguished from a local view.
6. If you run out of supplies, go back to reservoir and get more

## Efficiency Tips

- Use `get_agent_state()` to confirm your position and inventory when unsure
- Don't call `navigate_to` twice for the same target — it's instant teleport, one call is enough
- If intensity doesn't decrease after use_supply, you may have the wrong supply type
- Fire sources (Region_1, Region_2, …) must be extinguished before other regions

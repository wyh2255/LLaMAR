---
name: person-rescue-worker
description: Worker-specific person rescue procedure — carrying, synchronized drop-off, and inventory management during rescue.
---

# Skill: Person Rescue (Worker)

How to execute person rescue tasks as a worker.

## Before Rescue

1. Check your inventory in Context Memory
2. If you have supplies, either use them on a nearby fire first or simply keep them — **`carry_person` automatically drops your resources, so never waste a step on `clear_inventory` before a carry**
3. Navigate to the person's position

## Carrying a Person

1. Navigate to the person's exact position: `navigate_to(target="PersonName")` or specific coordinates
2. Call `carry_person(person_id="...")`
3. Your inventory now shows `['Person']` — all other resources dropped
4. **Do not call `carry_person` twice** — one call is enough
5. **One carry alone does NOT lift the person** — a person is only lifted once 2+ assigned carriers have each called `carry_person()`. If your carry succeeded, your part of the lift is done; proceed to the deposit.

## Coordinated Drop-Off

The person is rescued when ALL carriers are at the deposit AND every carrier has called `drop_off_person()` at least once. **The calls do NOT need to happen in the same step** — the environment counts each carrier's call cumulatively and completes the rescue as soon as the last carrier calls it.

1. Navigate to the deposit: `navigate_to(target="Deposit_0")`
2. **The moment you arrive, call `drop_off_person(person_id="...")`** — do NOT wait for the other carriers
3. If the result is not yet a success, the other carrier has not called it yet. Do NOT `no_op()` to wait: call `report_observation()` ("ready to drop at deposit"), then call `drop_off_person()` again next step
4. Once ALL carriers have called it, the rescue completes automatically

## Checking Partner Status

- Use Context Memory to see if other agents are carrying the same person
- **Never use `no_op()` to wait for a partner** — waiting burns the shared step budget and helps no one. Take your own action (carry / navigate / drop_off) immediately; the environment completes the joint action when everyone has done their part
- Do NOT call `ask_coordinator` to ask "should I drop off" — if you are carrying and at the deposit, dropping off IS the correct action

## After Rescue

After successful drop-off:
- Your inventory is now empty
- Call `finish_task(success=True, summary="Rescued person X", task_description="Rescue task")`
- Wait for the coordinator's next assignment

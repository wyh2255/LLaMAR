---
name: person-rescue-worker
description: Worker-specific person rescue procedure — carrying, synchronized drop-off, and inventory management during rescue.
---

# Skill: Person Rescue (Worker)

How to execute person rescue tasks as a worker.

## Before Rescue

1. Check your inventory in Context Memory
2. If you have supplies → either use them on a nearby fire first, or clear inventory
3. Navigate to the person's position

## Carrying a Person

1. Navigate to the person's exact position: `navigate_to(target="PersonName")` or specific coordinates
2. Call `carry_person(person_id="...")` 
3. Your inventory now shows `['Person']` — all other resources dropped
4. **Do not call `carry_person` twice** — one call is enough

## Coordinated Drop-Off

The person is rescued when ALL carriers are at the deposit AND ALL call `drop_off_person()` in the same step:
1. Navigate to the deposit: `navigate_to(target="Deposit_0")`
2. Wait for other carriers — call `no_op()` if you arrive first
3. Check Context Memory for other carriers' statuses
4. Once all carriers are at the deposit, call `drop_off_person(person_id="...")`

## Checking Partner Status

- Use Context Memory to see if other agents are carrying the same person
- If the other carrier is still navigating, use `no_op()` to wait
- Do NOT call `ask_coordinator` to ask "should I drop off" — coordinate via the deposit and simultaneous action

## After Rescue

After successful drop-off:
- Your inventory is now empty
- Call `finish_task(success=True, summary="Rescued person X", task_description="Rescue task")`
- Wait for the coordinator's next assignment

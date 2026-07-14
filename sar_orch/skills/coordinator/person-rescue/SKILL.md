---
name: person-rescue
description: Step-by-step coordination for multi-agent person rescue, including simultaneous carry, synchronized drop-off, and fire-risk assessment.
---

# Skill: Person Rescue

Multi-agent rescue coordination. Persons require **2+ robots** carrying simultaneously.

## Step 1: Assess Rescue Risk

Before initiating rescue:
- Is the person **near a fire**? If yes, suppress the fire first.
- Check Context Memory for person position and fire positions.
- If person is in a safe location, proceed directly.

## Step 2: Assign Carriers

Select 2+ agents closest to the person position. Give each a complete chain:

```
NavigateTo(Person_Position) → CarryPerson() → NavigateTo(Deposit_Position) → DropOffPerson() → NoOp()
```

- Assign **all carriers in the same round** so they arrive and carry simultaneously.
- Use different task_ids for each carrier (e.g. `alice-rescue-person1`, `bob-rescue-person1`).

## Step 3: Monitor Carry Phase

After dispatch:
- `COMPLETED` → agent carried and dropped off. If only one carrier completed, re-dispatch another.
- `FAILED` → check why. Common causes: wrong position, person already rescued, single carry attempt.
- If only some carriers completed, dispatch replacement agents.

## Step 4: Verify Rescue

The person is rescued only when:
1. All carriers were at the deposit simultaneously
2. ALL performed `DropOffPerson()` in the same step
3. Check Context Memory persons list — rescued persons should show `rescued=True` or be removed.

## Safety Rules

- NEVER let an agent carry a person through a fire region — fire damage may affect the person.
- If step budget is tight (<10 steps remaining), prioritize rescue if persons are at risk from spreading fires.
- After rescue, mark the person as cleared in your mental model even if the system tracks it.

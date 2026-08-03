---
name: person-rescue
description: Step-by-step coordination for multi-agent person rescue, including paired carry, cumulative drop-off completion, and fire-risk assessment.
---

# Skill: Person Rescue

Multi-agent rescue coordination. A person is only lifted once **2+ robots have each called `carry_person()`** on it.

## Step 1: Assess Rescue Risk

Before initiating rescue, follow the priority order (containment > rescue > mop-up — see the coordinator system prompt):
- Is the person **near an uncontained fire** (medium/high intensity)? If yes, contain that fire first — do not carry a person through or next to it.
- Is the person near a fire that is already **contained** (below medium)? Rescue proceeds now; the contained fire can wait.
- Check Context Memory for person position and fire positions.
- If person is in a safe location, proceed directly.

## Step 2: Assign Carriers

Select 2+ agents closest to the person position. Give each a complete chain:

```
NavigateTo(Person_Position) → CarryPerson() → NavigateTo(Deposit_Position) → DropOffPerson() → NoOp()
```

- Assign **all carriers in the same round** so they start moving together.
- Use different task_ids for each carrier (e.g. `alice-rescue-person1`, `bob-rescue-person1`).

## Step 3: Monitor Carry Phase

After dispatch:
- `COMPLETED` only means the worker finished its assigned chain — it does NOT prove the person was rescued. **Verify the rescue in Context Memory: the person must show `rescued=True` or disappear from the persons list.**
- `FAILED` → check why. Common causes: wrong position, person already rescued, single carry attempt.
- If only some carriers completed, dispatch replacement agents.

## Step 4: Close the Drop-Off (most common failure)

Drop-off is **cumulative, not simultaneous**: every carrier must be at the deposit and call `drop_off_person()` once; the rescue completes as soon as the last carrier calls it. Calls do NOT need to be in the same step.

- If carriers report "at deposit" / "carrying" but the person is still in the persons list, a carrier has NOT called `drop_off_person()` yet. **Immediately dispatch an explicit instruction to each carrier: "Call `drop_off_person()` now — do not wait for the other carrier."**
- Do NOT just poll `query_task_events` while carriers idle at the deposit — that is how rescues die at the step cap.
- If a carrier is stuck navigating or its task ended without dropping off, re-dispatch the drop-off instruction to it directly.

## Safety Rules

- NEVER let an agent carry a person through a fire region — fire damage may affect the person.
- If step budget is tight (<10 steps remaining), prioritize rescue if persons are at risk from spreading fires.
- After rescue, mark the person as cleared in your mental model even if the system tracks it.

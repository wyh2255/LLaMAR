---
name: observation-reporting
description: When and how to report observations — fire/person discoveries, status changes, and exploration findings.
---

# Skill: Observation Reporting

How to use `report_observation` effectively.

## When to Report

Report observations when you discover something **new or changed**:
- New fire spotted (not previously reported by any agent)
- New person found
- Fire intensity changed (e.g. low → medium)
- Person rescued status changed
- Reservoir or deposit discovered
- Fire fully extinguished

## How to Report

Call `report_observation()` with structured JSON fields. Example:
```json
{
  "object_type": "fire",
  "name": "CaldorFire_Region_3",
  "position": [5, 4, 0],
  "properties": {"intensity": "medium", "type": "chemical"},
  "note": "Discovered during exploration sweep"
}
```

## What NOT to Report

- ❌ Your own position or inventory — that's auto-tracked
- ❌ Every step you take — only meaningful discoveries
- ❌ Duplicate reports — check if the object was already reported
- ❌ "I'm done" — use `finish_task` for completion, not report_observation

## Report Is Non-Blocking

`report_observation` does NOT consume an environment step. It sends the observation to the coordinator via push notification. **Continue your task immediately after calling it.** Do NOT wait for a reply.

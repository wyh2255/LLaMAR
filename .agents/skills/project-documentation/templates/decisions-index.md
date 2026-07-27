---
doc_id: decisions-index
doc_type: decision-index
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "accepted-ADRs"
audience: [maintainers, reviewers]
---

# Architecture Decisions

This index points to one ADR per consequential decision. It is not a running
chat log and it does not replace the current architecture document.

## Decision status

- `proposed`: under review; not current policy.
- `accepted`: current decision unless superseded.
- `rejected`: retained for rationale; do not implement.
- `superseded`: replaced by a later ADR.

## Index

| ID | Decision | Status | Date | Superseded by | Related code/docs |
|---|---|---|---|---|---|
| ADR-0001 | `<short title>` | proposed/accepted/rejected/superseded | YYYY-MM-DD | — | `<paths>` |

## Authoring rules

- Create one file per consequential decision.
- Include alternatives, consequences, migration/reversal, and a revisit trigger.
- Link the accepted ADR from the current architecture or runbook when it
  changes an observable contract.
- Do not use an ADR to record an implementation detail with no durable trade-off.

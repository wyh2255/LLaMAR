---
doc_id: change-<YYYY-MM-DD>-<slug>
doc_type: change-plan
status: draft
owner: "<team-or-person>"
created: "YYYY-MM-DD"
updated: "YYYY-MM-DD"
authority: "approved-change-intent"
audience: [implementers, reviewers]
---

# Change: <short title>

## Intent

What user or system outcome should change? Define the observable result.

## Scope

### In scope

- `<file/module/API/state/document>`

### Out of scope

- `<explicit non-goal>`

## Current-state evidence

- `<path:symbol>` — `<current behavior>`
- `<test/config/command>` — `<corroborating evidence>`
- Unknowns: `<unknown and planned check>`

## Contract and invariants

- `<must remain true>`
- `<new or changed input/output/state contract>`
- `<failure, timeout, retry, cancellation, or security invariant>`

## Implementation map

| Phase | File/symbol | Change | Dependency | Exit condition |
|---|---|---|---|---|
| 1 | `<path:symbol>` | `<change>` | `<dependency>` | `<testable condition>` |

## Data, migration, and compatibility

- Persistent data/schema impact: `<none or detail>`
- Migration/backfill: `<procedure or none>`
- Backward compatibility: `<rule>`
- Rollback: `<procedure>`

## Verification plan

| Check | Command or assertion | Positive case | Negative/boundary case | Status |
|---|---|---|---|---|
| `<check>` | `<exact command/assertion>` | `<expected>` | `<expected failure/guard>` | not_run |

## Documentation impact

- `update | none`
- Targets: `<project/project.md / project/system_docs/architecture.md / project/runbook.md / ADR / none>`
- Reason: `<contract, boundary, operational, decision, or local-only>`

## Risks and open questions

- `<risk>` — mitigation: `<mitigation>`
- `<question>` — owner/check: `<owner and next check>`

## Review and status history

| Date | Status | Evidence or decision |
|---|---|---|
| YYYY-MM-DD | draft | `<initial record>` |

A change plan is not current implementation truth until the implementation is
verified and the affected canonical documents are updated.

---
doc_id: ADR-<NNNN>
doc_type: architecture-decision
status: draft
decision_status: proposed
owner: "<team-or-person>"
created: "YYYY-MM-DD"
updated: "YYYY-MM-DD"
authority: "decision-record"
---

# ADR-<NNNN>: <Decision title>

## Context

What problem, constraint, or trade-off requires a decision? Include the scope
and the facts that are stable enough to support the decision.

## Decision

State the selected option precisely. Include ownership, boundary, and any
normative invariants. Do not hide important conditions in a diagram only.

## Alternatives considered

| Alternative | Why considered | Why not selected |
|---|---|---|
| `<option>` | `<benefit or fit>` | `<cost, risk, or constraint>` |

## Consequences

### Benefits

- `<benefit>`

### Costs and risks

- `<cost/risk>`

### Operational or migration impact

- `<impact>`

## Compatibility and migration

- Existing behavior: `<behavior>`
- Migration steps: `<steps or NONE>`
- Rollback/reversal: `<procedure or NONE>`
- Data/API compatibility: `<rule>`

## Revisit trigger

Reconsider this decision when `<measurable condition, dependency change, or
new evidence>`.

## Evidence and links

- `<path:symbol/test/benchmark>` — `<what it supports>`
- Architecture: [`../system_docs/architecture.md`](../system_docs/architecture.md)

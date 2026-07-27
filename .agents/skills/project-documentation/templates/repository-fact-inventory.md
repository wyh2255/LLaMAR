---
doc_id: repository-fact-inventory
doc_type: working-reconnaissance
status: scratch
owner: "<agent-or-person>"
created: "YYYY-MM-DD"
authority: "source-inspection"
---

# Repository Fact Inventory

> Working artifact for initializing documentation in an unfamiliar repository.
> Keep it ignored or delete it after canonical documents are written unless the
> project explicitly wants to retain the audit trail.

## Repository boundary

- Root: `<absolute path>`
- Branch/revision: `<branch and revision>`
- Baseline dirty paths: `<paths or NONE>`
- Applicable instructions: `<paths>`
- Documentation scope: `<scope>`
- Explicit non-goals: `<non-goals>`

## Facts

| Claim | Source/evidence | Evidence class | Confidence | Destination |
|---|---|---|---|---|
| `<exact claim>` | `<path:line/symbol/command>` | implementation/config/test/runtime/recommendation | confirmed/partial/contradicted/unknown | project/architecture/runbook/ADR/change/none |

## Entry-point traces

### `<surface: CLI/API/library/worker>`

```text
<entry> → <registration> → <handler> → <core> → <storage/provider> → <result>
```

- Durable side effect: `<effect or NONE>`
- Error/failure boundary: `<boundary>`
- Cleanup/lifecycle owner: `<owner>`

## Persistence and state

- Canonical store: `<path/service or NONE>`
- Schema/migration owner: `<owner or NONE>`
- Identifier namespaces: `<list>`
- State/status values: `<list or UNKNOWN>`
- Transaction/locking/lease rule: `<rule or UNKNOWN>`

## Verification surface

| Purpose | Command/test | Read or executed | Result | Artifact |
|---|---|---|---|---|
| import/help/unit/integration/lint/build | `<exact command>` | read/executed | pass/fail/blocked/not_run | `<path or NONE>` |

## Existing-document classification

| Path | Primary role | Authority | Status | Action |
|---|---|---|---|---|
| `<path>` | current/architecture/operations/decision/change/evidence/history/agent/generated/scratch | `<authority>` | current/stale/draft/obsolete | retain/update/merge/link/archive/investigate |

## Unknowns and next checks

- `UNKNOWN`: `<question>` — next check: `<source or command>`
- `NOT_RUN`: `<command>` — blocker/reason: `<reason>`

---
doc_id: docs-index
doc_type: navigation
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "documentation-map"
---

# Documentation

<!-- Keep this file short. It is a navigation map and source-of-truth policy,
not a duplicate project summary. -->

## Read by goal

- Project documentation map: [`project/README.md`](project/README.md)
- Understand the project: [`project/project.md`](project/project.md)
- Understand the current system: [`project/system_docs/architecture.md`](project/system_docs/architecture.md)
- Follow data, event, control, and failure direction: [`project/system_docs/flow.md`](project/system_docs/flow.md)
- Run, test, deploy, or recover it: [`project/runbook.md`](project/runbook.md)
- Understand a consequential choice: [`project/decisions/README.md`](project/decisions/README.md)
- Understand an active change: [`project/changes/README.md`](project/changes/README.md)

## Source-of-truth map

| Topic | Canonical source | Not authoritative |
|---|---|---|
| Purpose, scope, users, constraints | [`project/project.md`](project/project.md) | daily notes, old plans |
| Current components and state ownership | [`project/system_docs/architecture.md`](project/system_docs/architecture.md) | generated diagrams, stale specs |
| Data/event/control direction and failure paths | [`project/system_docs/flow.md`](project/system_docs/flow.md) | unlabeled diagrams, stale specs |
| Setup, commands, configuration, recovery | [`project/runbook.md`](project/runbook.md) | copied shell history |
| Accepted design choices | [`project/decisions/`](project/decisions/) | chat messages, rejected plans |
| Proposed changes | [`project/changes/`](project/changes/) | current architecture until verified |
| One-run test/review evidence | ignored run directory or CI artifacts | current-truth docs |

## Lifecycle rules

- `current` documents describe verified present behavior.
- `draft` and `active` change documents describe intent, not implementation.
- `superseded` and `archived` documents remain for traceability and cannot
  override the current source.
- Commands are labeled `verified`, `not_run`, `blocked`, or `stale`.
- Sensitive values never appear in committed documentation.

## Maintenance trigger

Update the canonical document when a change affects its topic. Do not add a
phase diary or test transcript here merely because a run completed.

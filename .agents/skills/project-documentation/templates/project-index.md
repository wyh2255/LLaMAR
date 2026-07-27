---
doc_id: project-docs-index
doc_type: project-documentation-index
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "project-documentation-map"
audience: [maintainers, reviewers, operators]
---

# Project Documentation

This directory contains the durable project documentation set. Its three
primary sibling directories are:

- [`system_docs/`](system_docs/README.md) — current system structure, contracts,
  and directional flows;
- [`decisions/`](decisions/README.md) — consequential design choices;
- [`changes/`](changes/README.md) — active and recently verified changes.

## Read by goal

- Project purpose and scope: [`project.md`](project.md)
- Current system structure: [`system_docs/architecture.md`](system_docs/architecture.md)
- Data, event, control, and failure direction: [`system_docs/flow.md`](system_docs/flow.md)
- Setup, operations, and recovery: [`runbook.md`](runbook.md)
- Design rationale: [`decisions/README.md`](decisions/README.md)
- Active change intent: [`changes/README.md`](changes/README.md)

## Authority rule

`system_docs/` describes current behavior. `decisions/` explains why a
consequential choice was made. `changes/` describes proposed or recently
verified work. A change document does not override `system_docs/` until the
implementation is verified and the current docs are reconciled.

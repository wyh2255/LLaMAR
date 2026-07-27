---
doc_id: system-docs-index
doc_type: system-documentation-index
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "source-code-and-tests"
audience: [maintainers, reviewers, operators]
---

# System Documentation

These documents describe the current system structure. They are implementation
references, not daily diaries, change proposals, or one-run evidence.

- [`architecture.md`](architecture.md) — boundaries, components, ownership,
  state, persistence, and contracts;
- [`flow.md`](flow.md) — directional data/event/control flows, transformations,
  correlation fields, feedback, and failure paths.

## Writing rule

Keep component topology and state ownership in `architecture.md`. Keep
producer-to-consumer direction and edge semantics in `flow.md`. Do not maintain
two independent descriptions of the same flow.

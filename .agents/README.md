# Repository Agent Contracts

`.agents/` is the repository-owned, platform-neutral source of truth for reusable agent roles and their output contracts. It is intentionally not tied to Hermes, Claude Code, Codex, OpenCode, Cursor, or any one orchestration system.

## Available contracts

| Path | Purpose |
|---|---|
| [`fresh-reviewer.md`](fresh-reviewer.md) | Independent Fresh Reviewer role: audits a frozen Git range and emits the review/doc-impact artifact set. |
| [`context-prefix-stability.md`](context-prefix-stability.md) | Context prefix-stability contract: prefix-zone immutability, tail-only volatile content, and the `prune_policy` switch (P1 cache optimization). |
| [`delivery-secretary.md`](delivery-secretary.md) | Read-mostly Delivery Secretary role: consumes frozen evidence and writes a human-facing, self-contained HTML Change Acceptance Brief. |
| [`templates/change-acceptance-brief.html`](templates/change-acceptance-brief.html) | Primary six-layer CAB HTML template: target, boundary, contract, critical flow, risk, and evidence. |
| [`templates/change-acceptance-brief.md`](templates/change-acceptance-brief.md) | Legacy plain-text CAB template; use only when Markdown is explicitly requested. |
| [`templates/fresh-review-input.json`](templates/fresh-review-input.json) | Frozen Git range and evidence envelope for an independent review. |
| [`templates/final-review.json`](templates/final-review.json) | Machine-readable `APPROVE | REJECT` review gate. |
| [`templates/fresh-review-report.md`](templates/fresh-review-report.md) | Full human/audit review report template. |
| [`templates/doc-impact.json`](templates/doc-impact.json) | Durable-document impact decision and precise update request. |

## Cross-agent use

Not every Code Agent auto-discovers `.agents/`. The caller must explicitly load the relevant role and template paths, then provide a frozen evidence input package.

```text
Fresh Reviewer: read .agents/fresh-reviewer.md plus its four templates, then review an explicit BASE..HEAD range.
Delivery Secretary: read .agents/delivery-secretary.md plus the HTML CAB template, then consume the final review artifact set.
```

Platform-specific wrappers may exist later, but they must be thin adapters that point here. Do not copy the role instructions into multiple agent-specific files: that creates policy drift.

## Workflow boundary

```text
Builder → Verifier → Fresh Reviewer → Delivery Secretary → Human → commit/merge
```

The Fresh Reviewer owns the documentation-impact decision but does not edit persistent documentation. A Builder or Documentation Author applies required durable updates, a Verifier checks them, and a new Fresh Reviewer reviews the new frozen range before the Delivery Secretary receives an approval set.

## Artifact placement

Role contracts and templates under `.agents/` are intended to be tracked. Per-change review evidence belongs under `.agents/runs/<change-id>/` and is ignored by `.agents/.gitignore`; callers may choose another ignored output root but must pass explicit artifact paths through the input envelopes.


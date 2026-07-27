---
name: controlled-feature-delivery
description: Use when delivering a multi-file, cross-module, multi-phase, or high-risk API, state, concurrency, or persistence change that needs human-gated acceptance.
日期: 2026-07-24
文档类型: 项目技能
文档概述: 用确定性状态与证据门禁交付可暂停、可恢复、可审计的功能改动。
---

# Controlled Feature Delivery

## Activation Policy

This workflow is opt-in, not a default requirement for every coding task. Use it
when the user explicitly requests controlled delivery, or when the change is
multi-phase, cross-module, or high-risk, especially for API, state, concurrency,
persistence, security, migration, audit, or multi-agent work.

Do not initialize a run for a question, read-only analysis, a small single-file
fix, or ordinary exploratory work unless the user explicitly opts in. When the
risk is unclear, ask before creating a manifest or run. Once selected, follow
the complete gate sequence; do not partially apply the workflow.

When selected, use the workflow when implementation complexity can exceed a
human's ability to review it continuously.

1. Draft an acceptance intent and a manifest, then obtain a fresh, read-only
   Plan Review before starting implementation.
2. Work on exactly one phase at a time. A Builder's report is not completion:
   the parent rereads the diff, records its review, and runs the approved check.
3. Use `scripts/changeflow.py` for each gate. It owns run state and command
   evidence; it never invokes an LLM, commits, pushes, or edits agent config.
4. Run integration checks after every phase is verified. Decide `doc-impact`
   before final review. When it is `update`, limit the Doc Writer to manifest
   targets and have documentation verified.
5. Close every Major before final review. Give a fresh Final Reviewer the
   manifest, target diff, verification records, candidate brief, and explicitly
   referenced files only. It returns exactly `APPROVE` or `REJECT`; Blockers and
   Majors must be resolved, not deferred.
6. Stop in `HUMAN_REVIEW`. The human reads the acceptance brief and explicitly
   authorizes any commit. Merge and push remain separate user decisions.

Keep the plan and run evidence under the ignored local run directory; do not add
execution logs to canonical documentation. Report progress as implementation,
verification, or ready to commit, with the active gate and evidence path.

## Integrated Roles and Templates

The skill includes two platform-neutral roles from the repository agent guidance:

- `agents/fresh-reviewer.md` independently audits a frozen Git range, produces
  the authoritative final verdict, and owns the documentation-impact decision.
- `agents/delivery-secretary.md` compiles frozen evidence into a human-facing
  Change Acceptance Brief (CAB); it does not implement, verify, review, commit,
  merge, or push.

Use the role-specific templates rather than relying on chat summaries:

- Reviewer input: `templates/reviewer/fresh-review-input.json`
- Reviewer gate: `templates/reviewer/final-review.json`
- Reviewer report: `templates/reviewer/fresh-review-report.md`
- Documentation impact: `templates/reviewer/doc-impact.json`
- CAB HTML output: `templates/delivery/change-acceptance-brief.html`
- CAB Markdown fallback: `templates/delivery/change-acceptance-brief.md`

The Fresh Reviewer receives the frozen base/head range, approved intent,
verification records, documentation context, risks, and output paths. It must
write all three review artifacts and return only `APPROVE` or `REJECT` as the
machine verdict. A changed or unfrozen range, missing or stale evidence, an open
Blocker/Major, or pending required documentation produces `REJECT`.

The Delivery Secretary receives the frozen review package, verification facts,
smoke result, risk/rollback facts, and documentation-impact artifact. It first
validates the packet, then renders a standalone HTML CAB. Its status is derived
fail-closed: only frozen scope, complete fresh evidence, Fresh Review
`APPROVE`, and zero Blockers/Majors may produce `可提交`. It must disclose
`not_run`, `blocked`, `stale`, `N/A`, and `fresh_for_head=false` items separately.

Both roles are read-mostly and may write only their requested artifacts. They
must not modify implementation, tests, plans, durable documentation, Git state,
configuration, dependencies, deployment, or release state.

## Agent-Neutral Runner Contract

The ledger is agent-neutral. `smoke-skill-load --runner <name>` dispatches a
runner adapter; `opencode` is the first implementation. A runner must emit a
machine-readable skill-tool event containing the resolved skill directory and
the SHA-256 digest of the loaded skill tree. The command records both values and
`record-smoke` accepts only a matching `PASS`. No runner may substitute a chat
claim for the resolved path and digest.

Read `references/workflow-gates.md` for operational gates, the state machine
for recovery, `references/reviewer-contract.md` before delegating review, and
`references/documentation-impact-policy.md` before writing persistent
documentation. When using the integrated roles, also read the corresponding
role guide and templates under `agents/` and `templates/`.

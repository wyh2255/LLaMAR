---
日期: 2026-07-24
文档类型: 状态机参考
文档概述: 受控功能交付的允许状态转换与失败关闭原则。
---

# State Machine

The ledger state contains `schema_version`, `change_id`, `state`, `revision`,
`active_phase`, `resume_state`, `owner`, `updated_at`, and
`history_tail_hash`. The state tail must equal the last immutable history record
hash; a missing field or mismatch fails closed.

| Source state | Event | Actor | Guard | Destination | Idempotency |
|---|---|---|---|---|---|
| NEW | `record-plan-review` | parent | fresh approved plan review | PLAN_APPROVED | reject repeat |
| PLAN_APPROVED / PHASE_N_VERIFIED | `phase-start` | parent | next phase and allowlist baseline | PHASE_N_IMPLEMENTING | reject repeat |
| PHASE_N_IMPLEMENTING | `phase-parent-review` | parent | fresh approved phase review | PHASE_N_PARENT_REVIEWED | reject repeat |
| PHASE_N_PARENT_REVIEWED | `phase-verify` | parent | complete required checks PASS; exact scope, managed tree, and workspace evidence are fresh | PHASE_N_VERIFIED or INTEGRATION_VERIFYING | reject repeat |
| INTEGRATION_VERIFYING | `integration-verify` | parent | complete integration checks PASS, exact scope/tree/workspace evidence is fresh, and required smoke PASS | INTEGRATION_VERIFIED | reject repeat |
| INTEGRATION_VERIFIED | `doc-impact` | parent | decision is `update` or `none`; targets allowed | DOC_IMPACT_DECIDED or DOCS_VERIFIED | reject repeat |
| DOC_IMPACT_DECIDED | `docs-verify` | parent | approved fresh documentation evidence | DOCS_VERIFIED | reject repeat |
| DOCS_VERIFIED | `final-review` | parent | all Majors closed, bound review, sealed brief | FINAL_REVIEWING, then FINAL_REVIEW_APPROVED and HUMAN_REVIEW | reject repeat |
| HUMAN_REVIEW | `human-approve` | human session | scope is `commit`; final evidence fresh | HUMAN_APPROVED | reject repeat |
| HUMAN_REVIEW | `human-reject` | human session | commit scope and non-empty reason | BLOCKED with resume state | reject repeat |
| any non-terminal | `block` | parent | run is not terminal; preserve resume state | BLOCKED | reject terminal repeat |
| BLOCKED / INTERRUPTED | `resume` | parent | saved fingerprint and digest remain fresh | saved `resume_state` | reject if stale |
| any non-terminal | `interrupt` | parent | run is not terminal | INTERRUPTED with resume state | reject terminal repeat |
| BLOCKED / INTERRUPTED | `reconcile --outcome clean` | parent | reconciliation evidence is current | saved resume state | reject outside recovery |
| BLOCKED / INTERRUPTED | `reconcile --outcome adopt` | parent | adopted evidence is current and bounded | saved resume state | reject outside recovery |
| BLOCKED / INTERRUPTED | `reconcile --outcome repair` | parent | unknown or unsafe side effect | REPAIRING | reject outside recovery |
| REPAIRING | `repair-complete --target <state>` | parent | target equals recorded repair target | recorded target | reject mismatch |
| HUMAN_APPROVED | `prepare-commit` | parent | final tree and index are unchanged | COMMIT_PREPARED | reject repeat |
| COMMIT_PREPARED | `record-commit` | parent | current HEAD matches prepared tree | COMMITTED | terminal, reject repeat |

Plan/review rejection, stale evidence, malformed state, invalid path, unknown
transition, and revision conflict fail closed. `COMMITTED` never transitions
back. Recovery events are recorded in history and never truncate or rewrite its
tail. Evidence also fails closed for incomplete check sets, missing logs,
allowlist violations, forbidden paths, symlinks, renames, deletions, or mode
changes outside the declared scope. Verification logs redact basic secret
forms and environment metadata never contains environment values. Mutating
commands use compare-and-set `expected_revision`; only one concurrent writer
can commit a given revision.

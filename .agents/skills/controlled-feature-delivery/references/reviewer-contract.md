---
日期: 2026-07-24
文档类型: 审查者契约
文档概述: Plan 与 Final Reviewer 的输入、输出和独立性要求。
---

# Reviewer Contract

The reviewer is fresh and did not implement the phase. It is read-only: no
writing, staging, committing, network side effects, or broad repository scan.

Plan Review input is the plan, manifest, base/status, and plan-referenced files.
Final Review input is the approved manifest, target diff, verification records,
candidate acceptance brief, and files explicitly referenced by the brief.

Return machine-readable JSON with `verdict` exactly `APPROVE` or `REJECT`,
reviewer run and delegation identifiers, checked evidence, blockers, majors,
optional minors, and a one-sentence summary. A Major has `target_phase` and a
verifiable acceptance condition. Reject only when a Blocker exists.

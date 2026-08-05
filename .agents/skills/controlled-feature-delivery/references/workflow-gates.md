---
日期: 2026-07-24
文档类型: 工作流门禁
文档概述: 阶段串行、证据新鲜度和人工批准的操作要求。
---

# Workflow Gates

- Freeze the plan and manifest before the fresh Plan Review.
- Start only the next unverified phase. Parent review and fresh approved checks
  are required before the next phase.
- Run only exact manifest check specifications through `run-check`.
- Re-run checks after managed code or documentation changes; prior evidence is
  stale when its managed-tree digest differs.
- When the manifest requires smoke, the configured runner must produce a PASS
  artifact proving the resolved skill path and digest. A blocked smoke is
  evidence of a block, never a passing substitute.
- Do not call `human-approve` unless the user has explicitly approved commit
  scope in the current session. The command is a ledger entry, not identity.

---
title: Fresh Review Report
description: 完整、独立、可追溯的变更审查报告；机器门禁以 final-review.json 为准。
version: 1.0
---

# <变更标题> — Fresh Review Report

**审查模式：** `<impact_discovery | final_gate>`
**结论：** `<APPROVE | REJECT>`
**审查范围：** `<base_sha>..<head_sha>`
**Diff SHA-256：** `<digest>`
**范围冻结：** `<true | false>`
**Reviewer：** `<run/session ID；model/provider；独立性声明>`

## 1. 独立审查结论

<用 2–4 句说明：是否满足目标、最主要的 gate、不能从本审查推断的事项。>

## 2. 实际审查范围与证据

| 类别 | 实际读取 / 执行的内容 | 证据路径或 `path:line` | 限制 |
|---|---|---|---|
| Git 范围 | `<BASE..HEAD / diff digest>` | `<path>` | `<none 或限制>` |
| 需求 / 非目标 | `<plan sections>` | `<path>` | `<none 或限制>` |
| 源码与调用链 | `<changed files + one-hop seams>` | `<path:line>` | `<none 或限制>` |
| 验证记录 | `<checks assessed>` | `<path>` | `<not rerun / rerun facts>` |
| 持久文档 | `<docs inspected>` | `<path>` | `<none 或限制>` |

## 3. 需求与边界追踪

| ID | 审查结论 | Diff / 源码证据 | 验证证据 | 备注 |
|---|---|---|---|---|
| `REQ-1` | `<met | not_met | not_assessed>` | `<...>` | `<...>` | `<...>` |

- 非目标 / allowlist 结论：`<未越界 | 发现越界；证据>`

## 4. 发现（按严重度）

### Blocker

`none` 或逐项列出：`ID — 标题；证据；影响；最小修复/验收条件`。

### Major

`none` 或逐项列出：`ID — 标题；证据；影响；最小修复/验收条件`。

### Minor / 已记录技术债

`none` 或逐项列出：`ID — 标题；证据；影响；建议；是否阻塞本次 gate`。

## 5. 契约、关键流程与验证审计

- 契约 / 不变量：`<reviewed conclusion + evidence>`
- 主路径：`<trigger → owner/state → observable outcome；evidence>`
- 关键异常路径：`<failure/cancel/recovery conclusion；evidence>`
- 验证充分性：`<pass/fail/not_run/blocked/stale；哪些要求被哪个 artifact 覆盖>`
- 审查限制：`<none 或未能独立证明的事项>`

## 6. 持久化文档影响裁决

**决定：** `<update | none>`
**状态：** `<pending | satisfied | not_applicable>`
**机器产物：** `<doc-impact.json path>`

| 目标文档 | 类型 / 动作 | 必须记录的长期事实 | 源码证据 | 最终验收条件 |
|---|---|---|---|---|
| `<path>` | `<kind / create|update|supersede>` | `<fact>` | `<path:line>` | `<criterion>` |

若决定为 `none`：说明为什么当前系统地图、流程、契约、ADR、runbook 和 reference 均不需要更新；不能只写“没有改文档”。

## 7. Gate 与交接

- `final-review.json`：`<path>`
- `doc-impact.json`：`<path>`
- Delivery Secretary eligibility：`<true | false>`
- 下一步：`<交给 Delivery Secretary | 修复 Blocker/Major | 先更新持久文档并进行新的 Fresh Review>`

---

## 作者约束

- 本报告是完整审查证据，不是 30–60 行的人类 CAB。
- 所有 Blocker/Major 必须有可定位证据和可执行的关闭条件。
- `APPROVE_WITH_CHANGES` 不是有效结论；存在未关闭 Blocker/Major 或 pending 的必需文档时必须为 `REJECT`。

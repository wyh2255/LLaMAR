---
title: Change Acceptance Brief
description: 面向人类变更负责人的六层验收简报模板；用于提交或合并前的最终决策，不替代完整计划、测试日志或机器审查产物。
version: 1.0
audience: 人类变更负责人
main_body_budget: 30-60 行，优先保证一屏可作决定
---

# Change Acceptance Brief（CAB）模板

## 用途与边界

- **用途**：让人只读这一份简报，就能决定“批准 commit / 返回修复 / 接受明确风险”。
- **对象**：相对冻结基线的一个自洽 change，而不是整个项目的架构回顾。
- **不是**：文件逐项清单、subagent 工作日记、完整测试输出、重复的 system docs。
- **来源规则**：只允许引用实际 diff、已批准的目标/计划、验证记录、真实 smoke 产物、文档影响记录和 fresh review；不能根据 Builder 或 Reviewer 的口头自报补全事实。

## 状态与表达规则

- 状态只能是：`实施中`、`验证中`、`需返修`、`阻塞`、`待风险接受`、`可提交`、`已提交`。
- “可提交”必须同时满足：范围已冻结；所有必做验证有新鲜证据；Fresh Review 为 `APPROVE`；Blocker/Major 为 0；没有未披露的 required check。
- “已提交”只在真实 commit 已产生并记录 commit SHA 后使用；“可提交”不等于“已提交”。
- 每个关键结论标注其性质：`[事实]`（可直接由证据观察）、`[判断]`（基于事实的技术结论）、`[建议]`（需要人作出的决定）。
- 若某项不适用，保留该项并写 `N/A — <原因>`；若证据缺失，写 `未验证` 或 `阻塞`，不得省略或用“整体通过”掩盖。
- 不得把“所有已执行检查 fresh”写成“所有检查/证据 fresh”；任何 `not_run`、`blocked`、`stale`、`N/A` 或 `fresh_for_head=false` 项必须在头部或证据层单独披露。

---

# <变更标题> — Change Acceptance Brief

**状态：** `<实施中 | 验证中 | 需返修 | 阻塞 | 待风险接受 | 可提交 | 已提交>`  
**需要你决定：** `<批准 commit | 返回修复 | 接受明确风险 | 无（仅状态通知）>`  
**验收范围：** `<base commit>..<head commit 或 frozen worktree fingerprint>`  
**生成时间 / 证据新鲜度：** `<timestamp；已执行检查是否均针对 HEAD 新鲜；未运行/阻塞/N/A 检查逐项列出；最终编辑先后：是/否>`

## 一句话结论

`[判断] <本次是否实现目标、是否已满足进入 commit/merge 的门槛，以及最重要的限制。>`

## 1. 目标层：为什么做，做到什么程度

- `[事实] 目标：` <要解决的用户/系统问题及预期结果>
- `[事实] 成功条件：` <已批准、可验证的验收条件>
- `[事实] 非目标：` <本次明确不做的事情>
- `[判断] 结果：` <逐句说明目标/成功条件是否满足；未满足则写明差距>

## 2. 边界层：责任和状态归属改变了什么

- `[事实] 入口 → 可观察结果：` `<入口/触发> → <关键模块> → <结果/输出>`
- `[事实] Owner / 模块边界：` `<新增、转移或保持不变的状态/决策 owner>`
- `[事实] 未变化或范围外：` `<未触碰的模块、兼容边界、禁止的旁路>`

## 3. 契约层：哪些规则改变或必须保持

- `[事实] 变化的契约：` `<API / schema / message / 返回值 / 状态转换；before → after>`
- `[事实] 必须保持的不变量：` `<不可被破坏的规则及其 authority>`
- `[事实] 兼容性与失败语义：` `<旧调用方、错误/降级、迁移或 N/A>`

## 4. 关键流程层：实际怎么跑

- `[事实] 主路径：` `<触发 → 关键调用/事件 → canonical state/owner → 可观察结果>`
- `[事实] 关键异常路径：` `<失败 / 超时 / 取消 / 回滚如何收敛；或 N/A>`
- `[判断] 流程影响：` `<为何该流程足以实现目标；是否仍有未覆盖支路>`

## 5. 风险层：还可能在哪里出问题

- `[事实] 最大剩余风险：` `<风险、影响、状态：已缓解 | 已接受 | 未验证 | 外部阻塞>`
- `[事实] 发现信号：` `<测试、指标、日志、告警或用户症状>`
- `[事实] 回滚 / 恢复：` `<feature flag、revert、数据恢复；不可逆时明确写出>`
- `[建议] 风险决策：` `<无需额外接受 | 需要接受的具体风险及理由>`

## 6. 证据层：为什么可以相信上述结论

| 验收主张 | 状态 | 实际证据 | 覆盖范围 / 新鲜度 |
|---|---|---|---|
| `<目标、契约或关键流程主张>` | `<通过 | 失败 | 未验证 | 阻塞>` | `<命令 + 实际结果，或 artifact 路径>` | `<base/head、场景、时间>` |
| `<…>` | `<…>` | `<…>` | `<…>` |
| `<…>` | `<…>` | `<…>` | `<…>` |

- `[事实] 未验证 / 失败 / 外部阻塞：` `<none 或逐项列出；不得隐藏在总体结论中>`
- `[事实] Fresh Review：` `<final-review.json verdict；reviewer/model/provider/run ID；base/head/diff digest；Blocker=x，Major=y；完整报告路径>`
- `[事实] 文档影响：` `<update | none；pending | satisfied | not_applicable；doc-impact.json 路径；目标文档与原因>`
- `[事实] 完整证据：` `plan=<path>；verification=<path>；review=<path>；smoke=<path 或 N/A>`

---

## 生成输入包（给 Delivery Secretary / 报告生成 Agent）

在生成简报前，调用方必须提供或让 Agent 读取以下**冻结输入包**；缺任一必需项时，简报状态必须降为 `验证中`、`阻塞` 或 `需返修`：

1. **Change identity**：标题、需求链接/摘要、base/head（或 worktree fingerprint）、当前 commit/dirty 状态、报告生成时间。
2. **目标与范围**：已批准 plan 中的目标、成功条件、非目标、允许/禁止的文件或模块范围。
3. **变更事实**：canonical diff、受影响入口/owner/契约/关键流程，以及与基线相比的语义增量；必须可定位到 `path:line` 或 diff hunk。
4. **验证记录**：每个检查的命令、exit code、真实结果、执行时间、覆盖范围、日志/产物路径；要明确区分 focused、integration/E2E、lint/build、broader suite。
5. **真实 smoke / 运行证据**：场景、输入、结果、关键指标和 artifact；若未跑，提供未跑原因与授权/环境 blocker。
6. **Fresh Review**：独立 reviewer 的 `final-review.json`、完整 `fresh-review-report.md`、Blocker/Major/Minor、审查范围、base/head/diff digest、reviewer 身份及其证据路径。
7. **风险与回滚**：剩余风险、检测信号、回退步骤、已接受风险的批准记录（若有）。
8. **文档影响**：`doc-impact.json` 的 `update | none` 决定、`pending | satisfied | not_applicable` 状态、目标路径与原因；不得把 phase 日记当作长期系统文档。

## 生成 Agent 的硬约束

1. 这是**证据编排与人类可读性**职责，不是重新实现、重新验证或代替 Fresh Review 的职责。
2. 先读取输入包并检查路径/字段存在，再写报告；无法验证路径存在时明确标为 `未验证`。
3. 不把“测试进程退出 0”“Reviewer 自报完成”或“代码看起来合理”扩写为未被证明的行为结论。
4. 对每条目标、契约、流程和风险结论至少给出一项可追溯证据；一份证据不足以覆盖的结论必须拆开。
5. 主体控制在 30–60 行；把文件逐项说明、全量日志、所有测试名和长推理留在 evidence 路径，不复制进主体。
6. 生成后将 Brief 中的状态、关键 claim 和 evidence path 与输入包交叉核对；Brief 不得与 `final-review.json`、`fresh-review-report.md`、`doc-impact.json`、验证记录或最终聊天摘要矛盾。

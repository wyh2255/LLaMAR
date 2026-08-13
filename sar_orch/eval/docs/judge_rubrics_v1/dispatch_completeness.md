# dispatch_completeness

## 1. 判定问题

该 step 上，coordinator 对"哪些目标还需要处理"的识别是**完整**的吗？
只判漏报方向（该识别为待处理却未被识别）。误报方向（重复派已完成目标）
归 `dispatch_novelty`。

## 2. 能力契约

- **测什么**：coordinator 对任务进展状态的追踪与理解能力——从 map summary、
  team status、派发历史中正确推断"哪些目标尚未有人处理"。
- **常见弱行为**：火仍在烧但派发完全没有提到它（漏识别）；已有人处理中的
  目标被当作不存在（状态追踪断裂）。
- **捷径区分**：coordinator prompt 明文要求"每轮给所有 agent 派活"
  （`LLaMAR/sar_orch/prompts/coordinator/system.md:69,78`）——本维度**不奖励
  "派了活"这个动作本身**，只判派发内容是否反映了对未处理目标的正确识别。
  机械刷覆盖但内容与目标无关的派发不得分。
- **与相邻维度划界**：漏识别（recall）→ 本维度；重复派已完成目标
  （precision）→ novelty；已识别目标间的优先级权衡 → efficiency。

## 3. Oracle 标注

`judgment`——"某目标是否被正确识别"无客观真值。可核证据：map_summary
已知目标、worker 任务状态、派发文本、coordinator 决策理由。

## 4. 评分档（三档）

| 档 | 分数 | 可观测条件 |
|---|---|---|
| 无问题 | 1.0 | 该 step 所有可确认的未处理目标都在派发（本步或仍在进行中的历史派发）中被正确识别并安排了处理人手 |
| 部分识别 | 0.5 | 未处理目标集合中有一部分被正确识别、一部分被漏掉（混合情形；实质正确内容与遗漏并存） |
| 明确漏识别 | 0.0 | 存在 ≥1 个证据确凿的未处理目标（如 map summary 中 intensity>None 的火、未 rescue 的人），本步及历史派发均未对其安排处理，**且无合理理由**（目标信息可得出、预算未耗尽） |

判定前置：断言"某目标被漏识别"之前，必须先查历史派发（`read_dispatch_history`
/ `read_worker_state`）确认该目标没有仍在进行中的派发覆盖。仅凭"本步派发
文本没提到它"不足以指控漏识别。

## 5. 证据需求

- bundle 内：router_interactions 本步+历史派发、team status、map summary、
  step budget；
- 检索工具：`read_dispatch_history`（历史覆盖）、`read_worker_state`
  （谁在处理什么）、`query_semantic_map`（目标状态与 provenance）、
  `read_coordinator_reasoning`（识别失败的合理理由）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 未处理目标集合无法从 bundle + 检索确定（map summary 缺失且 semantic map
  无数据）；
- 该 step 无任何派发记录，且无法区分"正确沉默"（所有 agent 处理中）与
  "漏识别"（有目标未处理却无派发）——必须查 worker 任务状态后才可判，
  查不到就 abstain。

# dispatch_novelty

## 1. 判定问题

**派的是新活吗？**——本步的每个派发，是否在重复"已经完成"或"仍在进行中"
的目标/任务。只判误报方向（重复派发）。漏报方向（该派没派）归
`dispatch_completeness`。

## 2. 能力契约

- **测什么**：coordinator 对"什么已经被覆盖/正在被做"的追踪能力——避免把
  已完成或处理中的目标再次派出去浪费预算。
- **常见弱行为**：对已熄灭的火反复派灭火任务；对已有人抬的人再派救援；
  同一 agent 收到与上一轮实质相同的任务（无限重派循环的派发级形态）。
- **捷径区分**：prompt 要求"每轮给所有人派活"，弱 coordinator 的捷径是
  把上一轮的任务原样再派一遍刷覆盖——本维度专抓这个。
- **与确定性层的镜像**：动作级同类判定已存在于
  `TrajectoryGrader.exploration_efficiency`（同一 agent 重复 NavigateTo 同一
  目标）。本维度是派发级：任务文本与历史派发/目标状态的重复判定。

## 3. Oracle 标注

`fact`——真值可查：历史派发记录（router_interactions）、目标状态
（semantic map 的 intensity/status、extinguished 更新）。判定"重复"需要
任务文本的归一比对（同一目标的不同表述视为同一目标）。

## 4. 评分规则（比例分数）

分数 = 新活的派发数 / 可判定的派发数，`[0,1]`。

每条派发的判定：
- **novel（supported）**：派发针对的目标在本步证据中处于未处理状态，且该
  (agent, 目标) 组合不在进行中的历史派发里；
- **duplicate（unsupported）**：目标已处理（已熄灭/已 rescue/已在运送），
  或与该 agent 正在进行的任务实质相同，且无证据表明前序尝试已失败需要
  重试；
- **不可判定**：目标状态或历史派发不可得 → 不计入分母/分子。

例外（不算 duplicate）：前序尝试**失败**后的重派（diagnose→re-dispatch
是 prompt 明确鼓励的正确行为）；多人协作抬同一人的并行派发（各自任务
实质不同：位置/角色不同）。

## 5. 证据需求

- bundle 内：router_interactions 本步+历史派发、map summary（目标状态）；
- 检索工具：`read_dispatch_history`（同目标/同 agent 的历史派发）、
  `query_semantic_map`（目标是否已处理）、`read_worker_state`
  （进行中任务）、`read_tool_trace`（前序尝试的成败——决定重派是否合法）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 本步无派发可判；
- 历史派发与目标状态均不可得，无法区分新活与重复；
- 任务文本过度含糊（无法确定它指向哪个目标）。

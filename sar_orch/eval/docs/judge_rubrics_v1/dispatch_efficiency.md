# dispatch_efficiency

## 1. 判定问题

**已识别目标之间的优先级权衡值吗？**——给定剩余步数预算，本步派发把资源
投给了该投的目标吗。只判已识别目标之间的排序，不判识别对错（归
completeness/novelty）、不判执行前提（归 feasibility）。

## 2. 能力契约

- **测什么**：coordinator 在预算约束下的权衡能力——先灭火还是先救人、
  先处理高优先级目标还是先做低价值探索、物资准备是否服务于后续高价值
  动作。
- **常见弱行为**：烧着的火在蔓延却把步数花在无限探索已覆盖区域；剩余
  步数见底仍派长距离低价值任务；对 reachable 与 unreachable 目标不做区分
  地投入。
- **捷径区分**：prompt 说"dispatch aggressively"，弱 coordinator 的捷径
  是"aggressively 派低价值活"——本维度判的是投入方向的合理性，不是投入
  的数量。
- **defensible 规则**：以该 step 的证据为准，reasonable coordinator 可能
  做出的选择不算浪费（如灭火前先取物资是准备，不是浪费）。只判客观、
  有证据支撑的预算浪费。

## 3. Oracle 标注

`judgment`——"值不值"无客观真值。可核证据：step budget、目标优先级
（火在烧 > 已覆盖区域）、目标可达性、coordinator 决策理由
（`read_coordinator_reasoning`）。

## 4. 评分档（三档）

| 档 | 分数 | 可观测条件 |
|---|---|---|
| 无问题 | 1.0 | 本步派发与剩余预算、目标优先级、可达性一致（无可证据化的预算浪费） |
| 部分浪费 | 0.5 | 存在低价值投入（如对已覆盖区域继续探索），但同时存在服务于未处理目标的有效投入——投入方向喜忧参半 |
| 明确浪费 | 0.0 | 剩余预算花在证据显示不可达或已完成的目标上，同时存在更高优先级未处理目标（如火在烧却派无限探索），且无合理理由 |

判定前置：指控"浪费"必须同时给出两件证据——(a) 被浪费的投入本身（低价值
目标 × 派发记录），(b) 存在更高优先级的未处理目标。只有其一不构成浪费。

## 5. 证据需求

- bundle 内：step budget、map summary（未处理目标与优先级）、
  router_interactions 本步派发；
- 检索工具：`read_coordinator_reasoning`（决策理由，本维度的主要信号）、
  `read_dispatch_history`（历史工作与已完成目标）、`query_semantic_map`
  （目标状态/可达性）、`read_worker_state`（agent 实际在做什么）；
- 引用格式：`<file>:L<logical_line> sha256=<digest>`。

## 6. Abstain 条件

- 剩余预算不可知（bundle 与检索均无）；
- 目标优先级/可达性无法确定（map summary 与 semantic map 均无数据）；
- 不判战术偏好（阶段选择、agent 分配）——除非有客观预算违规证据。

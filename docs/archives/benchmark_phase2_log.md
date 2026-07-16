---
日期: 2026-06-28
文档类型: 实验日志
文档概述: A2A-SAR Phase 2 单场景验证日志 — 记录每个场景的运行结果、失败原因分析和参数调整
---

# Phase 2 验证日志

## 场景概览

| 场景 | 结果 | 步数 | Coverage | Transport | Token 总量 |
|------|------|------|----------|-----------|-----------|
| Scene 1 | ✅ 完成 | 18 | 100% | 100% | ~255K |
| Scene 2 (35步) | ❌ 超时 | 35 | 100% | 67% | ~529K |
| Scene 2 (重试50步) | ❌ 超时 | 50 | 100% | 60% | ~883K |
| Scene 3 | ❌ 超时 | 35 | 86% | 67% | ~659K |
| Scene 4 | ✅ 完成 | 18 | 100% | 100% | ~267K |
| Scene 5 | ❌ 超时 | 35 | 100% | 71% | ~444K |

## Scene 1 — CaldorFire + GreatFire

- **结果**: ✅ 完成
- **步数**: 18 / 1200
- **Coverage**: 100%
- **Transport**: 100%
- **耗时**: 218.5s
- **Token**: Alice 88K, Bob 47K, Coordinator 120K
- **日志**: `sar_experiment_20260628_013803`

### 分析
2 个 agent 成功完成所有任务：扑灭 CaldorFire 和 GreatFire 全部区域火，并协作救出 LostPersonTimmy。Coordinator 策略有效，Agent 执行力好。

## Scene 2 — EnglandFire + TownFire (首次, 35步)

- **结果**: ❌ 超时
- **步数**: 35 / 35 (上限)
- **Coverage**: 100%
- **Transport**: 67%
- **耗时**: 286.4s
- **Token**: Alice 192K, Bob 142K, Coordinator 195K
- **日志**: `sar_experiment_20260628_014158`

### 失败分析
火势蔓延速度快于 2 个 agent 的灭火效率。TownFire 有多个区域（Region_1, Region_2 等），agent 需要反复导航到 ReservoirWhosville 取水 → 回到火场浇水，但每次只能携带 1 单位水，而每次浇水只能降低 1 级强度。火势在等待期间会重新蔓延。

**根因**: Agent 每次只能携带 1 单位物资，导致灭火需要大量往返行程。步数上限（35）是原文标准，但 2 agent 在此场景不足以完成任务。

## Scene 2 (重试, 50步)

- **结果**: ❌ 超时
- **步数**: 50 / 50 (上限)
- **Coverage**: 100%
- **Transport**: 60%
- **耗时**: 779.1s
- **Token**: Alice 84K, Bob 732K, Coordinator 67K
- **日志**: `sar_experiment_20260628_015135`

### 分析
增加步数到 50 后仍然无法完成，transport rate 反而从 67% 降到 60%。原因是步数增加后火势有更多时间蔓延，导致需要灭的火更多。这是一个系统性问题，不是步数参数能解决的。

注意 Bob token 消耗异常高（732K），远高于其他轮次的 47K-142K。推测 Bob 在某个步骤中出现了 LLM 输出过长或循环调用，需要排查是否因为长 prompt 导致 token 暴增。

**结论**: Scene 2 在 2 agent 配置下，不管步数上限多少，都无法在合理范围内完成。这是论文原文也面临的约束（原 LLaMAR 论文同样设定 35 步上限）。需要更多 agent（3+）或优化灭火策略（如一次性携带多单位物资）。

## Scene 3 — EmberFire + AgniFire (35步)

- **结果**: ❌ 超时
- **步数**: 35 / 35 (上限)
- **Coverage**: 86%
- **Transport**: 67%
- **耗时**: 152.2s
- **Token**: Alice 263K, Bob 276K, Coordinator 120K
- **日志**: `sar_experiment_20260628_020504`

### 失败分析
场景 3 有 3 个初始火区（EmberFire Region_1, AgniFire Region_1/Region_2），都是化学火。Agent 探索不完整（coverage 86%），灭火循环中部分区域未被发现。火势随时间蔓延增加，agent 在 Sand 取水→浇水之间反复往返不够用。

**根因**: Coverage 未满 100% 说明探索策略不够密集；加上每次携带 1 单位 Sand 的往返效率低，35 步不足以覆盖全部火区。

### 调参判断
Transport 67% > 50%，不满足重试条件（失败标准为 Finished=False AND transport < 0.5）。问题本质与 Scene 2 相同 — 单次携带量限制。

## Scene 4 — RedFire (完成 ✅)

- **结果**: ✅ 完成
- **步数**: 18 / 35
- **Coverage**: 100%
- **Transport**: 100%
- **耗时**: 206.3s
- **Token**: Alice 78K, Bob 45K, Coordinator 144K
- **日志**: `sar_experiment_20260628_020758`

### 分析
3 个 RedFire 区域火（None type，Sand 可灭）全部扑灭，成功救出 LostPersonThomas（需 2 agent 协作搬运）。Coordinator 策略高效 — 先让 Alice 取 Sand 灭全部火区，再让两人协作搬运。

Scene 4 是最简单的场景（单火源类型 + 范围小），2 agent 配置表现完美。

## Scene 5 — WildFire + PrairieFire (35步)

- **结果**: ❌ 超时
- **步数**: 35 / 35 (上限)
- **Coverage**: 100%
- **Transport**: 71%
- **耗时**: 302.4s
- **Token**: Alice 157K, Bob 217K, Coordinator 70K
- **日志**: `sar_experiment_20260628_021138`

### 失败分析
全部区域已探索（coverage 100%），火被扑灭，但 LostPersonJacob 救援失败。两个 agent 都到达了被困者位置并执行了 `carry_person`，但在 `drop_off_person` 环节卡住了 — agent 到达 DepositFacility 后反复尝试放下但失败，因为两个人不同步到达。

**根因**: 协作搬运需要两个 agent 同时到达 Deposit 才能放下。Coordinator 分别 dispatch 任务，导致 Alice 和 Bob 到达 Deposit 的时间不同步，放下失败后双方都陷入重试循环。

---

## 共性问题

### 1. 物资携带限制
Agent 每次只能携带 1 单位物资（Sand/Water），灭火需要多次往返。如果每次 `get_supply` 能取多单位，效率会大幅提升。

### 2. 并发运行限制
`experiment.py` 使用固定端口（8080, 8191, 8192），多个实例不能同时运行。`benchmark.py` 已内置 Semaphore 解决此问题。

### 3. Token 消耗
Coordinator 消耗大量 token（约 70K-195K/轮），因为每一步都需要 LLM 推理来派发任务。

### 4. 协作搬运不同步
Scene 5 暴露出协作任务（2 agent 搬运）的同步问题。Coordinator 分别 dispatch 任务给两个 agent，但到达目标时间不同，导致 `drop_off_person` 失败。需要改进任务派发策略，确保两个 agent 同时到达目标位置。

### 5. Bob token 异常 (Scene 2 重试)
Scene 2 重试轮（50步）中 Bob 消耗了 732K token，是正常值的 5-10 倍。可能原因：Bob 在某步陷入死循环，生成了极长的 LLM 对话历史。建议排查 `agent_run_*.log` 确认是否存在 prompt 膨胀问题。

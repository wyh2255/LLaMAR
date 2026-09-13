# Worker 任务间隙 barrier 60s 超时（finish 后无 no_op 心跳）

> **状态**：已定位根因，未修复（待拍板）
> **发现**：2026-08-09（run `memory_acceptance_ff6994c_20260809_032204` 复盘时）
> **类别**：结构性编排缺陷（worker 任务间隙无 barrier 同步机制）
> **关联**：`sar_orch/tools/worker/no_op.py`、`src/Agent/worker_agent/`（任务循环退出逻辑）、
> `sar_orch/coordinator.py`（dispatch 决策链）

## 1. 现象

Bob 在 19:42:41 → 19:43:41 之间出现 60s 工具结果间隔（step 11 的 `use_supply`），
trajectory 显示 step 11 步长 **+65.4s**（wall 125.5→190.9s），`TimeoutAgents=[0]`（Alice 被系统注入 NoOp）。
同期 Bob **无任何 LLM 调用**（`workers/Bob/Bob/b653eefc-….ndjson` 19:42:41-19:43:41 无 llm_request）——
等待发生在工具执行阶段，即 barrier 同步阻塞，非 LLM 慢。

## 2. 时间线（run 内实测）

| 时间 | 事件 |
|---|---|
| 19:42:41 | Bob 提交 step 11 `use_supply` → barrier 等 Alice |
| 19:42:57 | Alice `finish_task`（EnglandFire 灭）→ **worker 循环退出，无动作可提交** |
| 19:43:07 | coordinator 轮 11：观察到蔓延 + EnglandFire 完成（决策链开始） |
| 19:43:28 | coordinator `update_plan`（新增 alice-help-townfire） |
| 19:43:32.9 | coordinator `activate_plan_node` → Alice 收到新任务 |
| 19:43:33 | Alice 新任务 LLM 循环启动 |
| ~19:43:41 | step 11 的 60s 窗口超时 → 系统注入 NoOp → step 推进 |
| 19:43:41 | Bob 收到被阻塞 60s 的 `use_supply` 结果 |

## 3. 根因

1. `no_op` 是 **LLM 主动调用**的工具（`sar_orch/tools/worker/no_op.py`），任务 `finish_task` 后 worker
   任务循环退出，**没有任何机制自动维持 barrier 同步**；
2. coordinator 观察→`update_plan`→`activate_plan_node` 决策链耗时 ~35s（每轮一次 LLM 决策 +
   `query_task_events` 等待，19:43:07 → 19:43:32.9）；
3. Alice 收到新任务后 LLM 循环 15s+ 才产出第一个动作，**错过 step 11 的 60s 窗口** → barrier 超时兜底 →
   同 step 其他 worker（Bob）的并行动作被整体阻塞 60s。

AGENTS.md 所述 "Workers auto-no_op after their main task" 仅覆盖**任务内**场景（main task 完成但 LLM
循环未退出时）；跨任务间隙（finish → 新任务激活）没有同步机制。

## 4. 影响评估

- 本 run：step 11 白等 60s，占总用时 374s 的 16%；600s wall-clock 限制下长 run 可能因此提前撞墙。
- **结构性**：每个"任务完成 → 新任务激活"的间隙都有同等风险；任务越短、间隙越频繁，累计浪费越大。
- 非 prompt 违规、非单点 bug；是任务生命周期与 barrier 步进模型的固有缝隙。

## 5. 修复建议（按性价比）

1. **worker 侧（根治）**：`finish_task` 后保持 LLM 循环 / 自动提交 no_op 心跳，直到收到新任务或 mission
   结束——消除任务间隙的 barrier 超时；
2. **coordinator 侧**：观察到任务完成且有 ready 节点时下一轮立即激活（缩短决策链 10-20s，受 LLM 轮次
   节奏限制）；
3. **参数权衡**：barrier 超时 60s → 30s（风险：LLM 长思考误伤，需实测）。

修复后验证：10 组交叉验证（5 scenes × 2 agents，max_steps=20, seed=42）中检查 trajectory
`TimeoutAgents` 列——任务间隙不再产生超时注入；step 时长无 60s 级尖峰。

## 6. 证据文件

- `sar_orch/results/memory_acceptance_ff6994c_20260809_032204/scene_2_agents_2/trajectory.csv`（step 11：+65.4s，TimeoutAgents=[0]）
- `…/workers/Bob/Bob/b653eefc-….ndjson`（19:42:41→19:43:41 无 LLM 调用，use_supply 结果延迟 60s）
- `…/workers/Alice/Alice/5fa63f81-….ndjson`（19:42:57 finish_task 后无记录）与 `9cfa9fa9-….ndjson`（19:43:33 才启动）
- `…/events.ndjson`（19:43:32.9 activate alice-help-townfire）
- 代码：`sar_orch/tools/worker/no_op.py`（no_op 为 LLM 主动调用）、`sar_orch/worker.py`（任务循环）

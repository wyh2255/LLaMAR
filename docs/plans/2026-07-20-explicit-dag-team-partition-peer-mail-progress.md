# Explicit DAG + Team Partition Peer-Mail — 实施进度台账

**状态：** Phase 0 已提交（`d67b0a2`）；Phase 1 已提交（`bd56f29`）；Phase 2 已提交（`b0104a6`）；Phase 3 已提交（`c0f2c60`）；Phase 4 已提交（`f4da71b`）；Phase 5 已提交（`6c93f15`）；Phase 6 已提交
**权威设计：** `docs/system_docs/route_strategy.md`（目标设计 / 未实现契约）
**详细实施计划：** `.hermes/plans/2026-07-20_163402-explicit-dag-team-partition-peer-mail.md`（被 `.gitignore` 忽略；本台账是可提交的进度记录）

## 实施规则

- 每个 Phase 由独立 subagent 实施；主 agent 负责范围审核、独立测试、计划状态更新和 scoped commit。
- 优先实现框架层确定性约束；prompt 仅描述已由框架强制的行为。
- 每个 Phase 的提交仅包含该 Phase 经审核的源代码、测试和本台账；不纳入既有未提交文件、实验输出、`.env` 或凭据。
- 状态只能在主 agent 独立复核为 GREEN 后更新为“完成”。
- 项目 pytest 默认不收集根目录 `tests/`；所有 Phase 测试必须显式以 `PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/...` 执行。

## 阶段状态

| Phase | 目标 | 实施 subagent | 主 agent 审核 | 状态 | 验证证据 / 备注 |
|---|---|---|---|---|---|
| 0 | 生命周期运行时：admission、ID、canonical status、restart、abort、transition fence | `gpt-5.6-luna` × 3 + Architecture A × 1 + source-contract fix × 1 + smoke root-cause + smoke-2 fix | 独立 fail-closed review APPROVE | 已提交（`d67b0a2`） | focused 165 passed；10 组实际 SAR lifecycle/routing 矩阵全部为零错误。仅验收 Phase 0，不等同于后续 peer-mail / TeamPartition 的最终策略验收。 |
| 1 | MissionGraph 与逻辑/物理双存储 | `grok-4.5` × 2（实施 + fail-closed 返修）；独立只读审计 × 4（预审×2、REJECT、final APPROVE） | 父级重读所有变更文件；真实对象探针；最终独立 fail-closed review APPROVE | 已提交 | 新增纯 `MissionGraph`、runtime-backed facade、opaque one-to-many dispatch；`MissionRuntime` 仍是唯一 PhysicalDispatch authority。RED 9 条后修复 allocation→graph attach 回滚、完整去重 fan-out、重复 activation、并发 exact-once、不可变 spec/防御性查询与 active semantic replacement。113 条 Phase1/Phase0/兼容探针全绿，Ruff/whitespace 通过；仓库广义 pytest 的未触碰环境/fixture 失败已单独取证。 |
| 2 | 显式 `update_plan` 与 Coordinator Context 投影 | `grok-4.5`（实施 + 审计返修） | 主 agent 独立范围审计、digest 返修复核 | 已提交 | `update_plan` 先原子验证完整 MissionGraph，再更新 legacy compatibility view；graph-managed dispatch 拒绝 `undeclared_task` 与 participant 不匹配。Coordinator Context 分开投影/渲染稳定排序的 Mission DAG 与 Physical Dispatch。focused Phase 0-2：81 passed；Ruff、`git diff --check` 通过。 |
| 3 | exclusive TeamPartition、singleton、global epoch、durable transition | `grok-4.5`（实施 + 两轮审计返修） | 主 agent 审计 I4 installed claim、release saga 与 WS_REGISTER 接线 | 已提交 | Coordinator-owned TeamPartition authority；online Worker 注册后建立 singleton。协作 team 的 installed claim 持续到 release，禁止 overlap；release 以单一新 global epoch 走 ACK/compensation/DEGRADED saga。持久化为原子 `0600`，recovery reconcile 保留 epoch 单调性。Phase 0-3 focused：154 passed；Ruff、`git diff --check` 通过。 |
| 4 | `activate_plan_node`、ACK-before-dispatch、awaited fan-out、canonical aggregation | `grok-4.5`（实施 + 审计返修） | 主 agent 审计生产 TeamPartition 注入、失败 rollback、远端 cancel compensation | 已提交 | `activate_plan_node` 仅按逻辑 ID 从 MissionGraph 读取 participants/objective/assignments；双重 DAG/participant gate、预分配 opaque dispatch、TEAM ACK 成功后才 fan-out。Nth acceptance failure 走 remote cancel/CANCEL_PENDING，状态聚合 exact-once；team ID/epoch 投影进 graph。Phase 0-4 focused：194 passed；Ruff、`git diff --check` 通过。 |
| 5 | Worker auth、`/team-status`、generation cache、安全 Context/observability | `grok-4.5`（实施 + 审计返修） | 主 agent 审计 valid-window replay 与 cache revision | 已提交 | `/team-status` 仅从 TeamPartition 投影，使用 worker-bound timestamp+nonce HMAC 与有界 TTL replay store；singleton 无 peers。Worker cache 追踪 team generation/partition revision 并可 transition 强制刷新。安全投影与事件不含 secret/proof/signature。Phase 0-5 focused：262 passed；Ruff、`git diff --check` 通过。 |
| 6 | termination wiring、recovery、deterministic E2E 与 SAR cross-validation | `grok-4.5`（实施 + fail-closed 只读审计）+ `code_agent`（真实运行缺口返修） | 主 agent 独立复跑、真实 SAR smoke ×3（含 peer-mail） | 已提交 | `MissionRuntime.abort()` 为所有 parent termination 唯一 finalizer：freeze、并发 bounded remote cancel、TeamPartition release/reconcile、清理并释放 admission；server shutdown、executor cancel、实验取消、startup recovery 均接入。补齐 SAR 生产 DAG dispatch/TEAM delivery adapters，terminal cancel 幂等与 EventQueue dispatcher 受控关闭。10 条确定性 E2E；scene3/4 agents/10 steps/peer-mail 回归正常 `max_steps_reached`，`no dispatch adapter`、`dispatch_acceptance_failed`、`team_setup_failed`、shutdown/routing 错误均为 0。全仓显式 `tests`：1111 passed，5 个既有凭据/历史 fixture 失败（非本阶段代码）。 |

## 已核实的前置事实（2026-07-20）

1. `CoordinatorAgentExecutor._execute_agentic()` 每次运行会清空共享 `event_store` 并将新的 `TaskStore` 覆盖注入给 `state_provider` / `TaskWatchdog`：`src/a2a/coordinator/agent_executor.py:307-327`。因此 active-mission admission 必须发生在这段可变状态创建之前。
2. callback 目前通过 module-global `_global_future_registry` 和 `_worker_to_dispatch_map` 路由：`src/a2a/coordinator/task_store.py:38-60`、`src/a2a/coordinator/server.py:664-858`。Phase 0 先以 RED 测试锁定“迟到 callback 不得污染新 mission”；后续 Phase 1/4 才能移除全局双命名空间回退。
3. `CoordinatorServer` lifespan 已拥有 sync task、watchdog 与 A2A child server 的启停顺序：`src/a2a/coordinator/server.py:317-386`；`CoordinatorAgentExecutor.cancel()` 目前只取消 TaskQueue / TaskUpdater：`src/a2a/coordinator/agent_executor.py:843-854`。`MissionRuntime.abort()` 的生产接线属于后续 lifecycle implementation，不能用 prompt 代替。
4. 当前 `CoordinatorTeamRegistry` 只能维护一个整体 roster：`src/a2a/coordinator/team_registry.py:67-194`；`CoordinatorSenderService.send_control()` 已能 awaited terminal ACK：`src/a2a/coordinator/sender_service.py:78-116`，可供后续 TeamPartition saga 复用。
5. 已有未跟踪的 `tests/test_task_store_sync.py` 是工作区的外部 contract，当前显式执行结果为 **21 failed**（raw protobuf state 未被 canonicalize）。该文件不属于本 Phase 的可提交范围，保留原样；Phase 1/4 必须将其作为兼容回归检查纳入。
6. 已有兼容回归基线为 **31 passed**：`tests/test_send_message_tool.py tests/test_cancel_task.py tests/test_respond_worker.py`。当前相关生产路径 Ruff 检查通过。

## Phase 0 审核结论与已采纳的范围修正

**独立审核：** `gpt-5.6-luna` / `custom:packyapi`，结论为 **APPROVE WITH CHANGES**。完整审核记录保存在会话产物；其关键代码事实已由主 agent 从当前源码复核。

### 唯一运行时 owner

`CoordinatorServer` 是唯一的 `MissionRuntimeManager` owner：它在 Server 生命周期中创建、恢复及 shutdown 前 abort runtime；通过 `create_coordinator_a2a_server(...)` 构造注入给 `CoordinatorAgentExecutor`，并为真实 `/a2a/push-callback` 路由提供 dispatch/context 查找。`SARCoordinator` 仅经由 `CoordinatorServer` 调用 lifecycle API，不能另存一份 admission/runtime；executor 也不能自行创建 owner。

Phase 0 admission 保证同一 Coordinator 至多一个 active agentic mission，因此现有单字段 `StateProvider` / `TaskWatchdog` 在本阶段只能读取 manager 的 active runtime，不能宣称已支持多 context 投影。Phase 2 才将 `context_id` 显式贯穿 Context hook / provider projection。

### 单一垂直实现切片

Phase 0 实施 subagent 必须在同一变更中完成下列闭环，不能把 producer 与 consumer 分给不同 agent：

1. 先新增可收集的 `tests/test_mission_runtime_lifecycle.py`，逐项记录预期 RED，然后以最小实现转 GREEN。
2. 新建 `src/a2a/coordinator/mission_runtime.py`：`ActiveMissionAdmission`、context-bound runtime、opaque physical dispatch registry、canonical physical-state transition、幂等 `abort()`、原子 `0600` Coordinator control-state persistence，以及仅作持久协议 DTO 的 `PartitionTransition`（不实现 multi-team 业务）。
3. 修改 `TaskStore`、executor、A2A factory、CoordinatorServer、TaskQueue / `DistributedTask`、EventStore、TaskWatchdog、StateProvider 与 SAR shutdown/cancel wiring，使 admission、callback、periodic sync、timeout、explicit cancel、server shutdown 和 experiment teardown 都进入同一 runtime API。
4. callback 必须通过真实 `CoordinatorServer` app 的路由测试，验证 context/dispatch ownership；禁止继续复制 handler。
5. Phase 0 不实现 MissionGraph、`update_plan` graph workflow、prompt 改造、TeamPartition registry 或 team-delivery saga。任何 team transition 仅能作为 runtime 的持久 DTO / fence 接口，不能制造第二个 roster authority。

### Phase 0 GREEN 契约

- 第二个 agentic context 在任何 `EventStore.clear()`、TaskStore/provider/watchdog 替换之前得到 `mission_already_active`。
- logical/user task ID 不能作为物理 lookup fallback；物理 ID 由系统生成且为 `dsp_<uuid>`。
- 真实 push route、periodic sync、cancel/timeout 仅经 canonical transition；未知或 stale context callback 仅记录有界 diagnostic，不能影响 active runtime。
- terminal physical state 不回退；artifact 不隐式改写 terminal state；`tests/test_task_store_sync.py` 作为既有外部 contract 必须转 GREEN 但不可修改/暂存。
- parent abort 是幂等的；已知 nonterminal dispatch 明确进入 cancel/cancel-pending，futures/artifact/callback owner 清理，admission 释放。
- Coordinator control-state 的写入使用 tmp + fsync + rename + `0600`；恢复分配严格更大的 epoch，完成 abort/reconcile 后才允许 admission。
- `DistributedTask.context_id` 与 `TaskQueue.list_by_context()` 成为真实 cancel-path API，不能保留 server 调用不存在方法的状态。

### 主 agent 独立验证（未通过，2026-07-20）

- 实施 subagent 声称的 focused command 已由主 agent 重跑：**103 passed**（有 1 个既有 Starlette deprecation warning）；Phase 0 目标路径 Ruff 也通过。
- 但这些测试没有覆盖真实的 legacy/agentic dispatch 链。主 agent 以 `DispatchTaskTool -> TaskStore(attached runtime) -> router.send_task_async -> MissionRuntimeManager.handle_callback` 直接探针验证，结果为：`tool_success=true`、`runtime_dispatch_count=0`、`legacy_worker_mapping={worker-task-live: logical-live}`、callback=`ignored/unknown_worker_task`。因此正常 dispatch 仍只写 legacy 全局映射，callback 无法进入 canonical physical transition。
- 持久化恢复探针还显示：control state 虽写入一个 `DISPATCHING` dispatch，但新 `MissionRuntimeManager(...).recover()` 后 `post_recovery_dispatch_count=0`。当前恢复只推进 epoch，不会重建或取消已持久的 physical dispatch，未满足 abort/reconcile 契约。
- 结论：禁止 commit；必须补充上述两个 RED 回归，并修复真实 dispatch materialization、callback routing 与 recovery reconciliation，再重跑 Phase 0 全部验证。

### 第二独立审计（拒绝）与返修范围

第二位独立审计 subagent（`gpt-5.6-luna / custom:packyapi`）复现并扩大了主审结论：正常 `DispatchTaskTool` 不创建 runtime dispatch；sync、watchdog、cancel、reply、query 仍混用 logical ID / global mapping；第一份 `SUBMITTED` callback 可能被 `PREPARED` fence 拒绝；重启只推进 epoch；并且 `SARCoordinator.stop()` 可能从调用方 event loop 跨线程直接操作 server-loop Future。审计 verdict 为 `passed=false`。

返修必须在同一 Phase 0 垂直切片内完成，且新增回归覆盖以下生产链：

1. `DispatchTaskTool` 在任何网络 I/O 前创建 `dsp_<uuid>` PhysicalDispatch，并将 router 发送、future、worker task mapping、PlanNode compatibility view 和 tool 返回值全部绑定该 physical ID；runtime attached 时不得再写 module-global future/mapping。
2. callback 的第一份 `SUBMITTED` / `WORKING` 状态能从真实 dispatch 进入 canonical state；sync、watchdog、cancel、reply、query 均只经 physical lookup 与 `apply_physical_status()`。
3. abort 先对远端 known Worker task 发起原生 cancel，canonical 记录为 `CANCEL_PENDING`，只在远端终态确认后写 `CANCELED` / `FAILED`；不能把本地 Future cancel 伪装成远端已取消。
4. restart 从持久 state 恢复 nonterminal physical dispatch，按更大 epoch fence 后 reconcile/cancel；未完成 reconciliation 前不释放 admission。
5. `CoordinatorServer.shutdown()` 必须在 owning server event loop 执行 runtime finalizer；跨线程调用使用 thread-safe handoff，不能直接 set/cancel foreign-loop Future。
6. 允许返修所需的 builtin tool 与 query 适配：`dispatch_task.py`、`send_message.py`、`cancel_task.py`、`respond_worker.py`、`query_task_events.py`，以及相应 focused tests。Phase 5 的 authenticated callback proof 仍为明确 deferred 安全项，不可伪称已完成。

### 主 agent 第二轮独立复核（未通过，2026-07-20）

第二轮实施的 producer 修复已通过独立 focused suite（**113 passed**）和 Ruff，但新增测试仍遗漏两个可复现的 canonical-state / termination 缺口：

1. **状态投影绕过 runtime：** 直接将 runtime dispatch 推进到 `COMPLETED`（不写 EventStore）后，`SARCoordinatorStateProvider` 仍渲染 `UNKNOWN`。已复现输出为 `{"runtime_state":"COMPLETED","provider_state_without_event":"UNKNOWN"}`。因此 periodic sync / runtime canonical state 尚未成为 Context Memory authority。
2. **`CancelledError` 泄漏 admission：** 用真实 `CoordinatorAgentExecutor` 注入一个会抛出 `asyncio.CancelledError` 的 controller，捕获该取消后 `manager.active_context_id` 仍为 `ctx-cancel`。`_execute_agentic()` 只在正常尾部 abort，未以 `finally` 覆盖 cancellation / setup failure。

定向返修必须先为上述两项建立 RED，并保证：provider 和 watchdog 以 runtime dispatch canonical state 为准（EventStore 仅为日志/结果辅助）；executor 在任何 admission 后的退出（包括 `CancelledError`）均执行幂等 abort；并增加“未注册 worker-task 的 abort 不能伪写 CANCELED，必须保持 CANCEL_PENDING”的回归。

### 真实 SAR smoke 复核（拒绝，2026-07-20）

命令：`scene=1 / agents=2 / seed=42 / max_steps=20 / semantic`。进程退出码为 0，但不满足 smoke 验收：`steps=6/20`、`coverage=0`、`transport_rate=0.0667`、`end_reason=coordinator_finished_early`，并在 shutdown 输出 `RuntimeError: athrow(): asynchronous generator is already running`。

已完成的根因链（主 agent artifact 复核 + 独立只读审计一致）：

1. watchdog 将只有探索、没有 coverage/transport delta 的 worker 判为 stale，触发对两个 physical dispatch 的 cancel。
2. physical dispatch 正确进入 `CANCELED`，但 compatibility `PlanNode` 仍为 `running`；runtime sync/cancel 没有统一 logical projection。
3. runtime attached 时 `SendMessageTool._check_worker_busy()` 仍读取 `PlanNode.state`，故持续返回 `worker_busy`。artifact 只有 2 个真实 physical dispatch，但 coordinator 记录 **92 次** `worker_busy` 重试。
4. relay 最终发生 4 次 retry 后的 HTTP 520；executor 把该失败包成普通 final text，A2A task 无 Python exception，experiment 误标 `coordinator_finished_early`，而不是 `framework_error`。
5. shutdown warning 来自 streaming ActiveTask 的 queue/generator 被 SDK producer/consumer finally 与 `shutdown_a2a_active_tasks()` 的二次 unconditional `close()` 并发收尾；双 Uvicorn 同 PID 的两条 finished log 是 outer coordinator + inner A2A server，非重复启动。

按系统调试 rule-of-three，Phase 0 已经历三次实现/返修且每次暴露新的 shared-state/lifecycle authority 缺口。暂停第四次补丁，等待用户选择架构方向。任何恢复实施必须先建立三类 RED：runtime terminal 后 worker busy 释放、LLM terminal failure 传播到 A2A failed status、真实 streaming shutdown 无 concurrent `aclose()` / `athrow`。

### Architecture A 重切（默认推荐方案）

用户选择窗口超时后，按推荐路径继续，**不是第四次局部补丁**。边界重新定义如下：

- runtime attached 时 `MissionRuntime` 是 worker availability、dispatch status、sync/cancel/watchdog/query 的唯一运行时 authority；`PlanNode` 只由一个受控 projection 生成兼容状态，不能再被 busy/cancel reader 当作并行真相。
- `TaskStore.apply_physical_status()` 是唯一 runtime → legacy projection 入口；callback、sync、cancel、watchdog 全部调用它。runtime terminal 状态必须立即释放同 worker 的 future dispatch eligibility。
- 只有 A2A SDK `ActiveTask` 在正常完成路径关闭它的 EventQueues；`shutdown_a2a_active_tasks()` 只负责 cancel+await，超时 fallback 才受控关闭未完成任务，禁止对已完成 ActiveTask 双 close。
- coordinator agent loop 的未恢复 LLM/transport failure 必须让 A2A request 进入 failed（供 `run_experiment` 归类 `framework_error`），不能包装成正常 `task_final` / `coordinator_finished_early`。
- 实施前必须新增 RED：cancel 后同 worker 可重派、runtime callback/sync terminal 的 compatibility projection、真实 streaming request 跨线程 shutdown 无 `athrow`、LLM failure 的 A2A failed propagation。完成后重跑 focused/full tests 和真实 SAR smoke；未通过不提交。

### Architecture A 主审追加拒绝：Agent failure contract 未闭合

Architecture A 的 173-test focused suite、Ruff 与 streaming shutdown regression 均已独立通过；但主 agent 以真实 Router `RunResult` contract 复现了原始 smoke 的未修复根因：

```text
fake controller returns RunResult(content="LLM 调用在 4 次重试后失败 ...", success=None)
→ CoordinatorAgentExecutor emits TASK_STATE_COMPLETED
→ no TASK_STATE_FAILED
```

复现输出：`{"has_failed": false, "has_completed": true}`。根因在 `src/Agent/router_agent/agent.py:522-537`：LLM / RetryExhaustedError 被转换为 `RunResult(success=None)`；而 `CoordinatorAgentExecutor` 只知道 controller 正常 return，按成功调用 `_finish()`。Architecture A 实现仅测试了 controller **raise**，没有测试真实 Agent 的“错误作为 RunResult 返回”语义。

下一步为单一 source-contract 修复（非重新扩 scope）：router/worker 两份 Agent 在 LLM retry/terminal engine error 时返回 `RunResult(success=False)`；executor 对 `success is False and not need_input` 传播 A2A failed；保留 cancel/need-input 与显式正常 `finish_task` 语义。必须同步两份 Agent、先写 RED、再验证真实 returned failure 不会产生 COMPLETED。

### 真实 SAR smoke 复核 2（2026-07-21，仍拒绝）

命令：`scene=1, agents=2, seed=42, max_steps=20, semantic, coordinator=18080`；artifact：`sar_orch/results/20260721_000938_s1_s42_a2`。这次 runtime physical dispatch 真实重派到了 `dispatch-1` 至 `dispatch-12`，未复现旧的 `worker_busy` 风暴，证明单权威 worker availability 修复生效；但 smoke 仍不可作为 Phase 0 验收：

1. **MCP shutdown 跨 loop 泄漏（P1）**：`SARWorker._assemble_tools_async()` 在各 worker 私有 `asyncio.run()` loop 中加载 streamable HTTP MCP；`Agent.worker_agent.tools.mcp_loader` 却把连接放入模块级 `_mcp_connections`，且 `SARWorker.run()` finally 从未调用 cleanup。loop 关闭时 Python 强制关闭 async generator，日志确证 `Attempted to exit cancel scope in a different task`、`aclose(): asynchronous generator is already running` 及 un-retrieved `AsyncClient.aclose()` 的 `Event loop is closed`。这不是此前 A2A EventQueue finalizer 的问题。
2. **伪 mission completion（P1）**：step 18/20 时 barrier `finished=false`，Coordinator LLM 调用 `sar_orch/tools/coordinator/finish_task.py` 并声称无 fire/person；该 tool 无 barrier completion guard，直接 `store.mark_finished(success)`，导致 A2A 正常完成和 `coordinator_finished_early`。必须以 barrier/mission truth guard 拒绝不真实 `finish_task`；prompt 不能作为唯一保障。

下一步仅允许先完成独立 root-cause 审计，再以 local MCP connection ownership + explicit cleanup-before-worker-loop-close、以及 SAR completion guard 的 RED/GREEN 实现。再次 smoke 前不提交。

### 真实 SAR smoke 复核 3（2026-07-21，生命周期通过）

命令：`scene=1, agents=2, seed=42, max_steps=20, semantic, coordinator=19080`；artifact：`sar_orch/results/20260721_005150_s1_s42_a2`。真实进程日志全量扫描为零：`RuntimeError`、`Event loop is closed`、`asynchronous generator`、`Attempted to exit cancel scope`、`Task exception was never retrieved`、`worker_busy`、`unknown_task_id`、`task_not_routable_yet` 全部为 0。

- 10 个实际 subtask 被分派；worker terminal 后重派正常；
- MCP session DELETE 在 worker shutdown 后、coordinator shutdown 前完成，没有 cross-loop finalization；
- `finish_task` 被调用 4 次，其中 2 次被 truth guard 拒绝；artifact 文本在真实 Coordinator context/query 中出现 24 次，`No completed task results available yet` 为 0；
- 环境在 `20/20` 正常触顶，得到 `end_reason=max_steps_reached`（`finished=false, coverage=0` 属 SAR 策略/任务效果，不构成 Phase 0 生命周期失败）。

提交前按既定框架层验证要求运行 `5 scenes × {2,3} agents × seed42 × max_steps20` 的 10 组交叉矩阵，要求上述三类 routing error 均为 0 且无 lifecycle shutdown exception。

### 10 组交叉验证（2026-07-21，FAIL-CLOSED）

首次矩阵得到 6/10 clean；三个 3-agent 运行各有 3 次 `worker_busy`，但取证证明为真实 active physical dispatch 尚未 terminal 时 Coordinator 直接 re-assign（并非旧 PlanNode 误报）；另一个 scene5/2-agent 将显式 `finish_task(success=false)` 误分类为 `framework_error`。权威 `route_strategy.md:755` 仍要求 `worker_busy=0`，不能降低门禁。

- completion 语义：单独修复 typed terminal mission failure，明确区分 engine failure；
- worker reassign：在未得到交互选择时采用推荐的**方案 A**：`assign_task` 遇 active physical dispatch 自动 remote-cancel，只有拿到 terminal confirmation 后才 dispatch replacement；未确认时保留 `CANCEL_PENDING`，不伪造 dispatch。这保持 worker single-active invariant 与 `worker_busy=0` 门禁，代价是明确的 last-write-wins preemption。

待以上两项 RED/GREEN 后，使用相同 10 组矩阵重新验证；旧矩阵 artifact 保留作负例证据。

### Phase 0 最终验收（2026-07-21，GREEN）

首次方案 A 的 auto-cancel replacement 在真实 scene1/3-agent 触发 44 次 cancel、51 次 assign、1134 秒仅 19 step 的抢占风暴，已拒绝；最终采用 context-owned、每 worker 一槽、last-write-wins 的 deferred assignment。活跃 worker 收到新任务时返回 `queued=true` 而不伪造 dispatch；canonical terminal 后在 owner loop 通过既有 `DispatchTaskTool` 激活最新 payload；`abort*` transition 与 store close 均禁止 late activation。

独立只读主审 verdict：**APPROVE**。主 agent 验证如下：

- focused Phase 0 契约：**165 passed**；Phase 0 改动路径 Ruff、`git diff --check` 通过；
- `pytest tests -q`：**879 passed, 1 skipped, 5 failed**。5 项均为既有外部条件：2 个 sandbox tests 缺测试凭据，3 个 report renderer tests 缺历史 `sar_orch/results/20260716_172653_s1_s42_a2` fixture；未修改测试、凭据或 fixture 以伪造通过；
- robust 真实矩阵：`scenes=1..5 × agents={2,3} × seed=42 × max_steps=20 × semantic`，每组独立 port range、timeout 时 kill process group；**10/10** 均 `steps=20`、`end_reason=max_steps_reached`，并且 `RuntimeError`、`Event loop is closed`、async-generator/cancel-scope、`worker_busy`、`unknown_task_id`、`task_not_routable_yet`、`replacement_cancel_pending`、`mission_already_active`、bind conflict 均为 **0**。日志：`/tmp/phase0-cross-validation-robust-20260721`；
- 该矩阵平均 coverage=0.461、transport_rate=0.395，属于 SAR agent 策略效果指标，不以 `finished=true` 作为 Phase 0 lifecycle gate。

这完成 Phase 0 的 runtime/lifecycle 契约验收；`route_strategy.md` 中 2/4-agent + peer-mail 的最终策略门禁仍属于 Phase 3–6 范围，未在本 Phase 宣称完成。

## Commit 纪律

每个 Phase 完成后：

1. `git status --short` 确认不把已有工作区变更纳入。
2. 只 stage 当前 Phase 经审核路径和本台账。
3. `git diff --cached --check`、目标 Ruff、显式 pytest 全部通过后提交。
4. 提交消息格式：`feat(<scope>): Phase N — <中文短描述>`。

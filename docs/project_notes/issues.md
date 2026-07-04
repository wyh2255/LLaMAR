# Issues

Work log with dates and status.

## Format

```markdown
### YYYY-MM-DD - Brief Description
- **Status**: Completed / In Progress / Blocked
- **Description**: 1-2 line summary
- **Notes**: (optional) important context
```

---

### 2026-06-27 - feat: my_a2a 框架迁移到 LLaMAR
- **Status**: Completed
- **Description**: 将 my_a2a 通用 Coordinator-Worker 多智能体编排框架完整复制到 LLaMAR/src/，消除外部 PYTHONPATH 依赖
- **Notes**: 分支 `feat/copy-my-a2a-framework`，保留旧 `integration/` 目录作为参考

### 2026-06-27 - feat: SAR 编排层 (sar_orch/)
- **Status**: Completed
- **Description**: 新建 sar_orch/ 目录，基于 my_a2a 框架构建 SAR 多智能体搜救任务编排系统
- **Notes**:
  - SARBarrier: 封装 SAREnv 的多智能体同步屏障（asyncio.Event + 30s 超时）
  - 10 个 Worker SAR 工具（navigate_to ~ no_op），继承 Tool 基类
  - 1 个 Coordinator 工具（query_sar_state）
  - SARCoordinator + SARWorker 包装类
  - experiment.py 实验运行器 + logger.py CSV 日志

### 2026-06-27 - feat: SAR 实时地图可视化
- **Status**: Completed
- **Description**: 新增 `/ui/map` + `/map/state` SSE 端点，实现 SAR 网格彩色地图实时渲染
- **Notes**:
  - map.html: 彩色网格表格 + 侧边栏（指标/agent库存/fire详情/person状态）
  - `/map/state` SSE: 每 500ms 推送 `{step, finished, coverage, transport_rate, snapshot}`
  - 序列化：`json.dumps(snapshot, default=lambda o: o.get())` 将 Coordinate 转为 `[x,y,z]`
-  全流程实测验证通过：2 agent 成功灭火（CaldorFire_Region_1 + _2: High→None）
-  为 `create_server()` 添加 `extra_tools` 参数，`CoordinatorServer` 添加 `set_barrier()` API

### 2026-06-27 - feat: 完整日志链路打通（agent_interactions + router_interactions）
- **Status**: Completed
- **Description**: 通过 step_callback 机制将 ExperimentLogger 接入 agent 执行链路和 coordinator 派发链路
- **Notes**:
  - **agent_interactions.csv**: 通过 Worker 端 `step_callback` 捕获 `tool_start` + `tool_result` 事件，记录 Step/Agent/ToolName/ToolArgs/Action/Observation/LLMOutput。修复框架 `AgentAdapter` 中外部回调未 await 的问题（回调改为 sync）
  - **router_interactions.csv**: 通过 Coordinator 端 `router_step_callback` 捕获 `dispatch_task` 的 `tool_start` 事件，记录 Step/Subtask/AssignedTo
  - **trajectory.csv 去重**: 添加 `_last_step_logged` 跟踪，防止同一 step 重复写入
  - **框架改动**: `create_server()` / `CoordinatorServer` / `create_coordinator_a2a_server()` / `CoordinatorAgentExecutor` 新增 `router_step_callback` 参数，向后兼容（默认 None）
-   **trajectory.csv 实时写入**: `submit_task()` 改为 fire-and-forget（`asyncio.create_task`），poll loop 立即运行，不再被 A2A 阻塞

### 2026-06-27 - fix: NavigateTo 死循环 + 新增 GPS 工具
- **Status**: Completed
- **Description**: 修复 Agent 反复调用 NavigateTo 的 bug（返回信息缺少"已到达"信号），新增 `get_agent_state` GPS 工具
- **Notes**:
  - **NavigateTo 增强**: 返回前缀 `"You have arrived at {target_id}."` + 坐标，LLM 不再误以为"还在路上"
  - **新增 `get_agent_state`**: 不消耗 step 的 GPS 工具，返回坐标/库存/step/observation
  - **system prompt 更新**: 强调 NavigateTo 是瞬间传送，提醒用 GPS 确认位置
-   **验证**: 修复前 Coverage 33% 且卡死，修复后 Coverage 100% / Transport 47%
-   **剩余问题**: 120s timeout 不够，Coordinator 编排策略需要优化（Agent 探索完没时间灭火）

### 2026-06-27 - fix: 移除文件系统工具 + 全流程验证通过
- **Status**: Completed
- **Description**: 从 SARWorker 彻底移除文件系统工具（bash/read_file/write_file），调整 timeout 参数，成功跑通完整灭火+救人链路
- **Notes**:
  - **include_base_tools**: AgentAdapter 新增参数，SARWorker 设为 False，LLM 不再看到文件工具
  - **STEP_TIMEOUT 30s → 15s**: 消除 barrier 同步等待假象，步均耗时从 30s 降至 13s
  - **timeout 对齐**: task_timeout=1200, orchestration_timeout=1200
  - **全流程验证**:
    - 场景: scene 1, 2 agents, seed 42
    - 结果: ✅ Finished=True, 18 steps, 233s, Coverage=1.00, Transport=1.00
    - 灭火: CaldorFire_Region_1/2 (Sand) + GreatFire_Region_1 (Water) 全部熄灭
    - 救人: Alice+Bob 同时 Carry→NavigateTo→同时 DropOff，Transport=1.00

### 2026-06-27 - feat: Agent Token 消耗统计 + 线程安全 + 异常兜底
- **Status**: Completed
- **Description**: 为每个 agent（Worker + Coordinator）添加每步 LLM token 消耗统计，日志线程安全，增量 summary 兜底进程异常退出
- **Notes**:
  - **Agent 框架**: 新增 `api_prompt_tokens` / `api_completion_tokens` + 3 个 `cumulative_*` 累计字段（含 summarization 路径）
  - **回调透传**: `llm_response` 回调传递 `usage=response.usage`，Worker/Coordinator 各自提取并调 `log_token_usage()`
  - **token_usage.csv**: `Step, Agent, PromptTokens, CompletionTokens, TotalTokens`，每次 LLM 调用一行
  - **线程安全**: `ExperimentLogger` 全部写操作加 `threading.Lock`，多 worker 线程并发安全
  - **增量兜底**: `flush_summary()` 每 poll step 后调用，覆盖写入 summary.csv，进程被 kill 也不丢数据
  - **summary.csv 扩展**: 动态 per-agent token 累计列（如 `AliceTotalTokens`, `CoordinatorTotalTokens`）
  - **验证**: 33 次 LLM 调用全量入 CSV；Grand Total 161,605 tokens；上下文从 1.7k 膨胀到 9.5k 清晰可见

### 2026-06-29 - feat: Benchmark Phase 3 批量运行（已完成）
- **Status**: In Progress
- **Description**: 完成 benchmark 批量运行代码（子进程隔离、并发控制、重试机制），但实际运行时 16 个 Scene 1 运行全部超时 900s
- **Notes**:
  - 代码完成：`benchmark.py`（子进程）+ `aggregate.py`（聚合）+ `monitor_benchmark.sh`（守护监控）
  - 实际运行结果：**0/16 成功，全部超时 900s**
  - 根因分析：Agent 只在 Step 0 调了 `get_agent_state()`（GPS，不消耗 step），之后 LLM 返回文本而非工具调用，Agent 循环终止，barrier 步进永不推进
  - 辅助问题：Coordinator prompt 退化（派"探索报告"任务）、`run_metrics.json` 被 kill 时丢失、seed 矩阵不含已验证的 seed=42

### 2026-07-02 - feat: Agent 上下文管理机制改造 + SAR 冒烟验证
- **Status**: In Progress
- **Description**: 实现 ContextManager（三层记忆策略 none/summary/hybrid）+ hooks + finish_task 工具，涉及 20 个文件的新增/修改。SAR 冒烟实验通过（scene=1, agents=2, seed=42, 5/5 steps, Coverage=0.3333, Transport=0.2667, 36.1s），但存在 5 个待修复问题。
- **Notes**:
  - 新增文件: `src/Agent/{worker_agent,router_agent}/context.py`、`hooks.py`、`sar_orch/tools/{worker,coordinator}/finish_task.py`
  - 验证成功: Coordinator 正确派发两条子任务（Alice→ReservoirUtah, Bob→ReservoirYork），Worker 执行链正常
  - **已修复**: `RunResult` 未从 schema 包导出
  - **待修复**: (1) shutdown SIGABRT 崩溃（daemon asyncio event loop 清理顺序），(2) `finish_task` 在 prompt 中不可见，(3) `agent_adapter.py` E402 ruff 违规，(4) `ContextManager.assemble()` 隐式 system prompt 假设，(5) 同 `context_id` 会话复用未在长流程中验证
  - 设计文档: `docs/plans/agent_context_management_plan.md`

### 2026-07-01 - fix: SAR 实验结构性修复（4 项 + 跨线程同步 + prompt）
- **Status**: Completed
- **Description**: 修复 sar_orch 实验系统的 5 类结构性问题，确保 prompt 测试阶段数据可信
- **Notes**:
  - **Commit 93d2541** — 结构性修复:
    - barrier: 超时 NoOp 标记 (`_last_timeout_agents`)、`stop()` 唤醒 worker、`submit_action` 检查 finished
    - logger: trajectory.csv 新增 `TimeoutAgents` 列
    - query_sar_state: 返回 step/max_steps/finished
    - experiment: poll loop 加 `a2a_task.done()` 退出 + 600s wall-clock 超时
  - **Commit 17d8f0f** — 跨线程同步修复:
    - asyncio.Event/Lock → threading.Event/Lock（worker 线程跨事件循环安全）
    - `_execute_step` 改 sync + `expected_step` guard 防重复执行
    - 验证: `--scene 2 --agents 4 --seed 42 --max-steps 3` → TimeoutAgents 正确显示 [2,3] 和 [1,2,3]
  - **Commit ad5d691** — prompt: coordinator 全 agent 分发 + 任务平衡
    - 新增 `Step Mechanics (CRITICAL)` 节，解释 barrier 机制
    - 要求每轮给所有在线 agent 分发任务
    - 验证: 步数 9→19，覆盖率 83%→100%，超时步 89%→37%
  - **Commit c358eab** — feat: worker auto-NoOp
    - no_op 工具返回 finished/step 状态
    - Worker 完成主任务后自动 no_op，5 次上限后返回
    - Coordinator 不再需手动填充 NoOp
  - **Commit d5758b4** — prompt: 强调最长动作链
    - 防止 auto-NoOp 让 coordinator 变懒给短任务
  - **交互流程确认**: 无结构性问题——coordinator→dispatch→worker→barrier(threading)→env→obs 全链路正确

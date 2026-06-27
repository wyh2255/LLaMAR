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

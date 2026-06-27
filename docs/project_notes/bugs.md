# Bugs

Bug log with dates, root causes, solutions, and prevention notes.

## Format

```markdown
### YYYY-MM-DD - Brief Bug Description
- **Issue**: What went wrong
- **Root Cause**: Why it happened
- **Solution**: How it was fixed
- **Prevention**: How to avoid in the future
```

---

### 2026-06-27 - LLaMAR 迁移踩坑 6 连击
- **Issue**: my_a2a 框架迁移到 LLaMAR 后，6 个集成问题导致全流程失败
- **Root Cause & Solution**:
  1. **Tool.execute() 必须返回 ToolResult**：SAR 工具返回 `str`，Agent 框架期望 `.success` 属性 → 改为返回 `ToolResult(success=True, content=...)`
  2. **Worker 启动顺序**：Worker 在 Coordinator 之前启动 → `ConnectionRefusedError` → 改为先启动 Coordinator
  3. **Worker ID 与 AgentRegistry 不匹配**：Worker 注册为 `sar-alice` 但 dispatch 用 `Alice` → 改为 `worker_id=name`
  4. **API Key 环境变量未设置**：Worker 创建时 `OPENAI_API_KEY` 未在环境中 → 在 `start()` 前用 `load_env_file()` 读取 .env 并设置
  5. **Coordinator 工具加载方式**：`tools_dir` 扫描要求 `tool` 模块属性，但 `QuerySARStateTool` 需要 `barrier` 运行时注入 → 为 `create_server()` 添加 `extra_tools` 参数
  6. **Worker prompt 未禁止 bash 工具**：Agent 调用 bash 探索文件系统而非 SAR 工具 → prompt 添加 `## Important Rules` 禁止
- **Prevention**: 迁移框架到新项目时，注意 (a) 启动顺序依赖，(b) ID 命名一致性，(c) 运行时注入 vs 静态加载的区别，(d) 系统 prompt 中明确工具的适用范围

### 2026-06-27 - AgentAdapter 外部 step_callback 未 await
- **Issue**: Worker 的 step_callback（写 agent_interactions.csv）从未执行
- **Root Cause**: `AgentAdapter._step_handler` 调用 `self._step_callback(type_=type_, **data)` 但未 `await`，async 回调的协程对象直接被丢弃
- **Solution**: 将 worker 的 step_callback 改为 sync 函数（`exp_logger.log_agent_interaction()` 是 sync 调用）
- **Prevention**: 框架层如需同时支持 sync/async 回调，应在调用前检查 `iscoroutinefunction(cb)` 并决定是否 await

### 2026-06-27 - trajectory.csv 在同一 step 重复写入
- **Issue**: poll loop 每 2s 调用一次 `log_step()`，同一 barrier step 被反复写入 CSV
- **Root Cause**: poll loop 没有跟踪上一个已记录的 step 编号
- **Solution**: 添加 `_last_step_logged` 变量，只在 `metrics["steps"] > _last_step_logged` 时写入
- **Prevention**: 轮询模式必须避免重复写入，可跟踪"最后记录点"或用哨兵值

### 2026-06-27 - submit_task() 阻塞导致 poll loop 无法启动
- **Issue**: `trajectory.csv` 和 `summary.csv` 在实验中从未生成
- **Root Cause**: `coordinator.submit_task()`（A2A JSON-RPC）阻塞直到编排完成，poll loop 在此期间不运行
- **Solution**: 改为 `asyncio.create_task()` 后台运行 poll loop 立即启动与 A2A 并行
- **Prevention**: 阻塞式 API 调用应先拆分为 fire-and-forget + 独立状态轮询

### 2026-06-27 - Agent 被 LLM 误导去探索文件系统
- **Issue**: SAR Worker LLM 调用 `bash` / `read_file` 命令探索 workspace 目录而非使用 SAR 工具（navigate_to/move/explore）
- **Root Cause**: Worker system prompt 未明确禁止文件系统工具，Agent 框架默认加载了 BashTool/FileTool
- **Solution**: prompt 添加 `## Important Rules` 禁止 bash/file 工具，声明"你是 SAR grid world 中的机器人，不是文件系统 agent"
- **Prevention**: 领域专用 agent 的 prompt 必须显式声明工具范围限制

### 2026-06-27 - NavigateTo 返回无"已到达"信号，Agent 陷入死循环
- **Issue**: Agent 反复调用 `navigate_to(ReservoirOmaha)` 3+ 次，从不进入 GetSupply。observation 坐标已变但 LLM 认为"还在路上"。
- **Root Cause**: `NavigateToTool.execute()` 返回的是下一轮原始 observation，没有明确告知 LLM "你已到达目的地"。LLM 看到全局列表中仍有目标对象，误以为导航未完成。同时 `get_position()` 返回 tuple（如 `(4, 19, 0)`）而非 Coordinate 对象，导致尝试提取 `.x` 属性时崩溃。
- **Solution**:
  1. NavigateTo 返回前加 `"You have arrived at {target_id}.\nYour position: ({x}, {y}, {z}).\n{obs}"` 前缀
  2. 新增 `get_agent_state` 工具（GPS），直接从 barrier 读状态，不消耗 step，返回 `[GPS] Position: (x, y, z) | Inventory: {…} | Step: N`
  3. system prompt 强调 `navigate_to` 是瞬间传送、`get_agent_state()` 可用于确认位置
  4. 使用 `pos[0]`/`pos[1]`/`pos[2]` 而非 `pos.x`/`pos.y`/`pos.z`
- **Prevention**: 工具返回给 LLM 的内容必须包含明确的"已完成"信号，不能依赖 LLM 自行从 observation 推断。涉及坐标属性访问时先确认返回类型是 tuple 还是对象。

### 2026-06-27 - Agent 虽禁用但仍可见文件系统工具，最终调用 bash
- **Issue**: 即使 prompt 说"不要用 bash"，Agent 在卡住（人质协同失败）后仍回退到 `bash` / `ls` / `find` 探索文件系统
- **Root Cause**: `AgentAdapter._build_agent()` 硬编码了 `ReadTool()` / `WriteTool()` / `BashTool()` 在工具列表中。LLM 看到这些工具就会尝试调用，prompt 禁令只是软约束
- **Solution**: 添加 `include_base_tools=False` 参数到 `AgentAdapter` 和 `create_worker_a2a_server()`；SARWorker 传入 `False`，彻底从工具列表移除文件系统工具
- **Prevention**: 领域专用 agent 应从框架层面移除不适用的工具，不能只靠 prompt 约束。框架应提供 `include_base_tools` 开关

### 2026-06-27 - Barrier STEP_TIMEOUT 30s 导致 A2A 编排"开销大"的错觉
- **Issue**: 实验报告显示"步均耗时 ~30s"，被误认为是 A2A 编排开销大。实际灭火+救人全流程只需 233s/18 步
- **Root Cause**: `SARBarrier.STEP_TIMEOUT=30s`。当两个 agent 执行不同任务时，快的 agent 提交 action 后要等慢的 agent，等待 15-30s 后才触发 NoOp 自动填充。这 15-30s 被计入"步耗时"中
- **Solution**: 将 `STEP_TIMEOUT` 从 30s 降到 15s。agent LLM 调用通常只需 ~2s，15s 足够。调整后步均耗时降至 ~13s
- **Prevention**: barrier 超时要根据实际 agent LLM 响应时间来设置，而不是随意选大值。agent 提交 action 的时间可通过日志中 LLM 调用耗时来校准

### 2026-06-27 - Coordinator orchestration_timeout 比 barrier task_timeout 短导致无人派活
- **Issue**: Barrier 900s 但 Coordinator 600s 就超时了，剩下 300s agent 无人派活，原地空转
- **Root Cause**: `coordinator.py` 中 `orchestration_timeout=600` 硬编码，与 scene 的 `task_timeout` 不匹配
- **Solution**: 将 `orchestration_timeout` 提升到 1200s，与 barrier timeout（1200s）对齐
- **Prevention**: Coordinator 的 orchestration_timeout 应 >= barrier 的 task_timeout，或从同一个配置源读取

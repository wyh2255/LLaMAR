# Decisions

Architectural Decision Records (ADRs) with context, trade-offs, and consequences.

## Format

```markdown
### ADR-XXX: Decision Title (YYYY-MM-DD)
**Context:** Why / what problem
**Decision:** What was chosen
**Alternatives Considered:** Option → Why rejected
**Consequences:** Benefits / Trade-offs
```

---

### ADR-001: 将 my_a2a 源码复制到 LLaMAR 而非 PYTHONPATH 引用 (2026-06-27)

**Context:**
- LLaMAR 原本通过 `PYTHONPATH` 引用外部 `MARoS/my_a2a/src` 获得编排能力
- 外部依赖耦合导致版本不一致、路径脆弱、独立部署困难

**Decision:**
- 将 my_a2a src/ 完整复制到 LLaMAR/src/（a2a/ + Agent/）
- LLaMAR 独立维护 git 历史，my_a2a 只作模板参考

**Alternatives Considered:**
- 继续 PYTHONPATH 引用 → 拒绝：耦合太紧，路径易变
- pip editable install → 拒绝：需要同时维护两个仓库
- git submodule → 拒绝：submodule 管理复杂，不符合独立交付目标

**Consequences:**
- ✅ LLaMAR 可以独立开发、独立部署
- ✅ 不依赖 MARoS 项目存在
- ⚠️ my_a2a 框架更新时需要手动同步到 LLaMAR/src/

### ADR-002: SAR 编排层作为独立包 sar_orch/ (2026-06-27)

**Context:**
- 需要基于 my_a2a 框架构建 SAR 专用编排逻辑
- 已有 `integration/` 目录是旧的 MARoS 集成（基于 OpenHarness），不能直接复用

**Decision:**
- 新建 `sar_orch/` 独立包，包含 barrier、coordinator、worker、tools、prompts、experiment
- 复用 my_a2a 框架的 A2A server/Tool 基类，但业务逻辑全新编写
- 保留 `integration/` 作为参考但不依赖

**Alternatives Considered:**
- 修改 integration/ 直接使用 my_a2a → 拒绝：旧代码依赖 MARoS 太多，重构成本高
- 直接在 experiment.py 中内联所有逻辑 → 拒绝：无法复用、难以测试

**Consequences:**
- ✅ 代码清晰分层：框架层(src/) + 编排层(sar_orch/) + 环境层(SAR/)
- ⚠️ sar_orch/ 和 integration/ 有重复概念（SARBarrier 等），需注意差异

### ADR-003: 使用 extra_tools 而非 tools_dir 注入 Coordinator 工具 (2026-06-27)

**Context:**
- `QuerySARStateTool` 需要运行时注入 `barrier` 实例
- `tools_dir` 模式扫描目录中的 `.py` 文件，要求暴露模块级 `tool` 属性（工厂模式在 import 时创建）

**Decision:**
- 为 `create_server()` 和 `RouterAgent.__init__()` 添加 `extra_tools` 参数
- SARCoordinator 在运行时创建 `QuerySARStateTool(barrier=...)` 并通过 `extra_tools` 传入

**Alternatives Considered:**
- 模块级 `tool` 变量 + 后续 patch barrier → 拒绝：时序脆弱，逻辑分散
- 从 barrier 读取全局单例 → 拒绝：引入隐式全局状态

**Consequences:**
- ✅ 框架提供两种工具注入方式：`tools_dir`（静态）和 `extra_tools`（运行时）
- ✅ 通用模式：任何需要运行时依赖的工具都可以通过 `extra_tools` 注入

### ADR-004: 地图可视化用 HTML Table + SSE 而非 matplotlib/WebSocket (2026-06-27)

**Context:**
- 需要在实验运行时实时查看 SAR 网格地图状态
- SAR 已有 matplotlib 渲染（`SAR/utils.py:render()`），但需要独立窗口

**Decision:**
- 用纯 HTML Table（`<td>` 带背景色）渲染网格，不依赖 matplotlib 生成图片
- 用 SSE（Server-Sent Events）推送 JSON 状态到前端，前端 500ms 自动刷新
- `/map/state` 端点调用 `barrier.get_env_snapshot()` 并序列化为 JSON

**Alternatives Considered:**
- matplotlib 生成 PNG → 前端轮询图片 → 拒绝：带宽大、延迟高、需要磁盘 IO
- WebSocket 双向推送 → 拒绝：SSE 更简单，只需单向推送，无需额外依赖
- Canvas/WebGL 渲染 → 拒绝：过度复杂，表格渲染足以显示 <500 个格子

**Consequences:**
- ✅ 零额外依赖（不加载 numpy/matplotlib 到浏览器）
- ✅ SSE 复用现有 `/logs/{task_id}/stream` 模式
- ✅ 22×22 网格渲染 <100ms，适合实时刷新
- ⚠️ 不适合渲染 >1000 格子的地图（如 100×100 网格）

### ADR-005: 使用 step_callback 机制将 ExperimentLogger 接入 Agent 执行链路 (2026-06-27)

**Context:**
- ExperimentLogger 的 `agent_interactions.csv` 和 `router_interactions.csv` 一直为空，因为没有任何代码调用对应的方法
- Worker Agent 的 `run()` 已有 `step_callback` 钩子，在每次 llm_response / tool_start / tool_result 时触发
- Coordinator 的 RouterAgent 同样有 `step_callback` 机制（`_emit_agentic_event`）

**Decision:**
- Worker 端：在 `SARWorker.start()` 中创建 `step_callback` 闭包，捕获 `tool_start`（记录 tool_name/args）+ `tool_result`（记录 observation），合并后调用 `exp_logger.log_agent_interaction()`
- Coordinator 端：为 `create_server()` → `CoordinatorAgentExecutor` 添加 `router_step_callback` 参数，透传到 RouterAgent 的 `step_callback`，在 `dispatch_task` 的 `tool_start` 事件中调用 `exp_logger.log_router_interaction()`

**Alternatives Considered:**
- 在 `SARBarrier._execute_step()` 中统一记录 → 拒绝：拿不到 tool_name/tool_args 和 LLM output
- 在 poll loop 中轮询 barrier 数据 → 拒绝：信息有限，且与 step_callback 重复
- 修改 Agent 框架代码直接引用 ExperimentLogger → 拒绝：框架不应依赖上层日志实现

**Consequences:**
- ✅ agent_interactions.csv 包含完整的 tool_name/tool_args/action/observation/LLMOutput
- ✅ router_interactions.csv 包含完整的 subtask 描述和指派对象
- ✅ 框架改动最小，只新增 `router_step_callback` 参数（向后兼容）
- ⚠️ Worker step_callback 需要 sync（AgentAdapter._step_handler 未 await 外部回调）

### ADR-006: A2A 任务以 fire-and-forget 方式提交，poll loop 与编排并行 (2026-06-27)

**Context:**
- `experiment.py` 中 `coordinator.submit_task()` 通过 HTTP POST 调用 A2A JSON-RPC
- A2A 编排（LLM 调用 + 工具执行）耗时数分钟，导致 poll loop 被阻塞
- `trajectory.csv` 和 `summary.csv` 在 poll loop 中写入，因此只在编排完成后才生成
- 实验被 kill 时这两个文件丢失

**Decision:**
- `submit_task()` 改为 `asyncio.create_task()` 后台运行
- poll loop 立即启动，与 A2A 编排并行运行
- barrier 完成后若 A2A 任务仍在运行，则 cancel 清理

**Alternatives Considered:**
- 在 SARBarrier._execute_step() 中直接写 trajectory → 拒绝：barrier 不应依赖 ExperimentLogger（ADR-003 原则）
- 通过 SSE/WebSocket 实时监听 A2A 事件 → 拒绝：过度复杂，增加依赖

**Consequences:**
- ✅ 所有 4 个 CSV 在实验运行中实时写入
- ✅ 即使实验被 kill，已有数据不丢失
- ✅ 改动仅 3 行，无框架变更

### ADR-007: 新增 GPS 工具 + NavigateTo 返回增强 (2026-06-27)

**Context:**
- Agent 反复调用 `navigate_to(ReservoirOmaha)` 3+ 次，从不执行 GetSupply
- 导航是瞬间传送，但 tool 返回值没有"已到达"的明确信号，LLM 误以为仍在路上
- 坐标使用 `pos.x` 但 `get_position()` 返回 tuple，导致 AttributeError 崩溃
- Agent 没有不消耗 step 就能查询自身状态的工具

**Decision:**
- NavigateTo 返回前拼接 `"You have arrived at {target_id}.\nYour position: ({x}, {y}, {z}).\n"` 前缀
- 新增 `get_agent_state` 工具（GPS）：直接从 barrier 读 `env.controller` 获取坐标/库存，不调用 `submit_action()`，不消耗 step
- 使用 tuple 索引 `pos[0]/pos[1]/pos[2]` 而非属性访问
- system prompt 强调 NavigateTo 是瞬间传送、GPS 可用于确认位置

**Alternatives Considered:**
- 仅修改 observation 文本让 LLM 自行推断 → 拒绝：deepseek-v4-flash 无法可靠推断，"已到达"必须显式声明
- 在 barrier 中保存 agent 位置并由 submit_action 返回 → 拒绝：GPS 工具更通用，且 zero-cost

**Consequences:**
- ✅ Coverage 从 33% → 100%（Agent 知道导航已完成）
- ✅ Transport Rate 从 20% → 47%
- ✅ Agent 可以随时调用 GPS 确认位置，不影响 barrier 步进
- ⚠️ 120s timeout 仍然不够，Agent 探索完没时间灭火（编排策略需后续优化）

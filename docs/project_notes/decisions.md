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

### ADR-008: Agent Token 消耗统计 (2026-06-27)

**Context:**
- 多轮对话中 LLM 上下文不断累积，每次调用 token 递增，需要量化每 agent 的成本
- LLM API 返回 `response.usage` 含 `prompt_tokens` / `completion_tokens` / `total_tokens`，但之前只存了 `total_tokens`（用于摘要触发）
- 三路 LLM 调用需要覆盖：Worker agent（Alice/Bob）、Coordinator agent（编排规划）、summarization（摘要生成）

**Decision:**
- Agent 类新增每个调用 + 累计 6 个字段（`api_prompt_tokens`, `api_completion_tokens`, `cumulative_total_tokens` 等）
- `llm_response` 回调传递 `usage=response.usage`，回调方提取后调 `LogTokenUsage()`
- `_create_summary()` 中的 LLM 调用也计入累计字段
- Logger 新增 `token_usage.csv`（Step/Agent/PromptTokens/CompletionTokens/TotalTokens 每行一次 LLM 调用）
- `summary.csv` 动态生成 per-agent token 累计列

**Alternatives Considered:**
- 在 Agent 外部订阅 LLM API 日志 → 拒绝：框架外无法获取 response.usage
- 只记录 total_tokens 不拆 prompt/completion → 拒绝：prompt 膨胀才是核心关注点

**Consequences:**
- ✅ 每次 LLM 调用的 prompt/completion/total 入 CSV，可追踪上下文窗口增长
- ✅ Coordinator / Worker / Summarization 全路径覆盖
- ✅ summary.csv 含每个 agent 的 token 累计数，便于对比
- ⚠️ 两套 Agent 代码副本（worker_agent + router_agent）需同步修改

### ADR-009: 增量 flush_summary() 兜底进程异常退出 (2026-06-27)

**Context:**
- shell timeout（300s）直接 SIGTERM 进程，`finally` 块不执行
- `summary.csv` 只在 `close()` 中写入，因此异常退出时丢失

**Decision:**
- Logger 新增 `flush_summary()` 方法，每次覆盖写入 summary.csv
- 每个 poll step 后调 `flush_summary()`，确保磁盘上始终有最新数据

**Alternatives Considered:**
- atexit handler → 拒绝：无法保证在 SIGTERM 下执行
- Signal handler → 拒绝：需要匹配具体信号，且 SIGKILL 不可捕获

**Consequences:**
- ✅ 任何时刻 kill 进程，summary.csv 有截至上一完整 step 的数据
- ✅ 与现有 `close()` 兼容（close 内部调 flush_summary + 关文件句柄）
- ⚠️ 每次 poll 都写一次 summary 文件，数据量很小无性能问题

### ADR-010: benchmark 实时进度跟踪 + 异常监控 (2026-06-30)

**Context:**
- 100 轮 benchmark 需要 ~3-5 小时，手动统计进度不现实
- 之前只能等全部跑完看 index.json，期间无法了解进展
- 子进程崩溃时 benchmark 静默标记为 timeout，无法及时发现问题
- 需要运行中实时查看进度 + 失败时立即告警

**Decision:**
- benchmark.py 添加三层实时监控：
  1. **stderr 进度条**：每轮完成时输出 `[████░░] 45/100 (45%)  ✓30 ✗8 ⏱3 ⊘4  ▶3  120s elapsed · ETA 180s`
  2. **progress.json**：每个状态变更点原子写入 `{total, running, success, failed, timeout, skipped, timestamp}`
  3. **SIGUSR1 信号处理**：`kill -USR1 <pid>` 在 stderr 打印当前快照
- 使用 Claude Code 的 `Monitor` 工具实时 grep 子进程输出中的 ERROR/FAILED/Traceback 信号
- 使用 `TaskCreate` 跟踪 benchmark 执行状态

**Alternatives Considered:**
- 用 web dashboard（FastAPI + HTML） → 拒绝：增加依赖，开发成本高，实验性项目不需要
- 只依赖 CLI 日志 → 拒绝：无法自动化告警，需人盯着终端
- 外部轮询 result.json 目录 → 已经在用 progress.json 补充，但轮询不如事件驱动及时

**Consequences:**
- ✅ 运行中实时可见进度条 + ETA
- ✅ progress.json 可被任何外部脚本读取（例如 `while true; do clear; cat progress.json; sleep 5; done`）
- ✅ SIGUSR1 可在不中断运行的情况下获取进度
- ✅ Monitor + grep 实现异常推送，失败时立刻知道
- ⚠️ stderr 进度条与日志输出可能交错，但 `\r` 覆盖机制能保持可读性
- ⚠️ 并发 2 时进度条闪烁（两个子进程几乎同时完成），但不影响准确性

**注意：进度跟踪 bug**
- 最初 progress 更新无条件 `success += 1`，未检查实际 `run.status`，导致崩溃后仍显示 100% success
- 修复方法：检查 `run.status == "success"` 才递增 success，否则递增 timeout

### ADR-007: benchmark 用子进程而非进程内执行实验 (2026-06-28)

**Context:**
- 并发 benchmark 需要同时跑 2 个实验，每个实验启动 coordinator + workers（固定端口 8080, 8191...）
- 进程内执行时，实验结束后 daemon 线程的 uvicorn 服务器不释放端口，后续实验绑定失败
- 即使加了 `shutdown()` 方法，Worker 的端口释放仍不可靠（thread.join timeout 后残留）

**Decision:**
- `benchmark.py` 的 `run_single` 用 `asyncio.create_subprocess_exec()` 启动独立 `experiment.py` 进程
- 每个子进程通过 CLI 参数 `--coordinator-port` / `--agent-base-port` / `--log-dir` 获得唯一端口范围
- 子进程完成后写 `run_metrics.json` 到 `exp_log_dir`，benchmark 读取并转写 `result.json`
- 用 `--run-timeout N` 包裹 `proc.communicate()`，超时则 `proc.kill()`

**Alternatives Considered:**
- 修复 daemon 线程 shutdown → 尝试了但 Worker 端口释放不可靠，需要干预 uvicorn 内部状态
- 用 `concurrent.futures.ProcessPoolExecutor` → 不如 asyncio.subprocess 灵活，不易控制超时和端口分配

**Consequences:**
- ✅ 实验完全隔离，进程退出时所有端口自动释放
- ✅ 子进程不影响主进程的 asyncio 事件循环
- ✅ 超时通过 `proc.kill()` 强制终止
- ⚠️ 子进程需要独立 env（PYTHONPATH、proxy 等），手动配置
- ⚠️ 实验结果通过 JSON 文件传递，而非函数返回值

### ADR-011: SARBarrier 使用 threading 同步原语替代 asyncio (2026-07-01)

**Context:**
- Worker 在独立线程中运行各自的 asyncio 事件循环
- barrier 使用 `asyncio.Event`/`asyncio.Lock`，但这些原语在 Python 3.10 中不支持跨事件循环——`Event.set()` 用 `loop.call_soon()`（非 `call_soon_threadsafe`）调度唤醒，其他线程的等待者永远不被唤醒
- 导致 barrier 60s 超时静默触发、`TimeoutAgents` 追踪为空、`asyncio.Lock` 无法跨线程互斥

**Decision:**
- `asyncio.Event` → `threading.Event`（`set()`/`wait(timeout)` 线程安全）
- `asyncio.Lock` → `threading.Lock`（`with` 语法跨线程互斥）
- `_execute_step` 改为 sync 函数，通过 `asyncio.to_thread()` 调用
- 新增 `expected_step` 参数防止多 agent 同时超时导致重复执行
- `submit_action` 用 `asyncio.to_thread(event.wait, remaining)` 实现异步等待

**Alternatives Considered:**
- `asyncio.run_coroutine_threadsafe()` 统一到主事件循环 → 拒绝：需要大改 worker 调用方式，侵入性强
- 单事件循环 + 进程内序列化 → 拒绝：worker A2A server 需要独立事件循环处理 HTTP 请求
- 用 `threading.Barrier` → 拒绝：不支持 per-agent 超时和 NoOp 填充

**Consequences:**
- ✅ 跨线程同步正确，`TimeoutAgents` 追踪准确
- ✅ `threading.Event.set()` 可靠唤醒其他线程的等待者
- ✅ `expected_step` guard 防止重复执行
- ⚠️ `_execute_step` 持锁期间 `get_env_snapshot` 会阻塞 coordinator 事件循环（<1s，可接受）
- ⚠️ `asyncio.to_thread` 占用线程池，4 agent 场景下无压力

### ADR-012: Worker 自动 NoOp 保持 barrier 同步 (2026-07-01)

**Context:**
- Coordinator 给不同 agent 不同长度的动作链。短链 agent 完成后返回，长链 agent 剩余步骤每步等 60s 超时。
- 依赖 coordinator 手动平衡任务长度（用 NoOp 填充）对 LLM 要求太高，不可靠。

**Decision:**
- `no_op` 工具返回 barrier 的 `finished`/`step` 状态
- Worker 完成主任务后自动调 `no_op()`，看到 `[MISSION COMPLETE]` 才返回
- 连续 5 次 no_op 后强制返回（让 coordinator 重新规划）
- Coordinator prompt 不再要求手动填充 NoOp

**Alternatives Considered:**
- Worker 无限 no_op 直到 finished → 拒绝：coordinator 永远拿不到 collect_results，无法重新规划
- 固定 no_op 次数由 coordinator 指定 → 拒绝：增加 prompt 复杂度，LLM 难以准确计数
- 在 barrier 层自动填充已返回 agent 的 NoOp（不靠 worker 主动） → 这已经是超时机制的行为，但浪费 60s/步

**Consequences:**
- ✅ Barrier 超时率从 89% 降到 32%
- ✅ Coordinator 不需要手动平衡任务长度
- ⚠️ 5 次 no_op 上限浪费部分步数预算（可调参）
- ⚠️ 需要在 coordinator prompt 中强调"仍应给最长有用链"，否则 LLM 会变懒给短任务

### ADR-013: TimeoutAgents 追踪 + step 可见性 + 实验生命周期修复 (2026-07-01)

**Context:**
- trajectory.csv 无法区分"LLM 主动 NoOp"和"系统超时注入的 NoOp"——prompt 效果分析不可信
- Coordinator prompt 说 "Monitor the step counter" 但 `query_sar_state` 不返回 step 信息
- 实验 coordinator 结束后 poll loop 继续空转，barrier 每步 60s 超时直到 max_steps
- `barrier.stop()` 不唤醒等待中的 worker，导致 hang
- 单次实验无 wall-clock 上限，可能跑 2+ 小时

**Decision:**
- `barrier._last_timeout_agents` 记录每步超时注入的 agent 索引列表
- `trajectory.csv` 新增 `TimeoutAgents` 列
- `query_sar_state` 返回 `step`/`max_steps`/`finished`
- poll loop: `a2a_task.done()` 无异常时 break（coordinator 结束即退出）
- `barrier.stop()`: 设 `_stopped`/`_finished` + `event.set()` 唤醒所有 worker
- `submit_action`: 开头检查 `_stopped/_finished`，立即返回
- 单次实验加 600s wall-clock 安全网

**Alternatives Considered:**
- 在分析脚本中推断 NoOp 来源 → 拒绝：无法准确区分
- 把 step 信息放到 coordinator prompt 文本中 → 拒绝：LLM 需要结构化数据，不能从文本推断

**Consequences:**
- ✅ Trajectory 数据可信——可过滤系统 NoOp 只分析真实动作
- ✅ Coordinator 能做步数预算决策
- ✅ 实验不空转、不 hang、不超时
- ✅ 清理干净，benchmark 子进程不残留

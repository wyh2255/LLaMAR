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

### 2026-06-28 - CoordinatorServer 和 Worker 缺少 shutdown，daemon 线程永久持有端口
- **Issue**: 并发 benchmark 中，实验完成后端口仍被 daemon 线程占用，后续实验绑定失败 → 静默卡死 → 600s 超时
- **Root Cause**: `CoordinatorServer.run()` 使用 `uvicorn.run()`（阻塞，不可停止）且没有 `shutdown()` 方法；`SARCoordinator.stop()` 检查 `hasattr(self._server, "shutdown")` 为 False，是空操作。Worker 的 `stop()` 也是 `pass`。daemon 线程的 uvicorn 服务器永久运行。
- **Solution**: 
  1. `CoordinatorServer.run()` 改用 `uvicorn.Config + uvicorn.Server` 模式，新增 `shutdown()` 方法设置 `should_exit = True`
  2. `Worker.stop()` 使用 `threading.Event` 通知事件循环退出 + 设置 `server.should_exit = True`
  3. 最终改用**子进程方案**：`benchmark.py` 用 `asyncio.create_subprocess_exec()` 启动独立 `experiment.py` 进程，实验结束时整个进程退出，所有端口自动释放
- **Prevention**: 使用 uvicorn 时优先用 `uvicorn.Server` + `serve()` 模式而非 `uvicorn.run()`，确保可停止。daemon 线程应提供关闭信号机制。多实验批量运行时，子进程隔离比线程隔离更可靠。

### 2026-06-28 - benchmark asyncio.gather Path/str 类型错误
- **Issue**: benchmark 预处理 `result.json` 时崩溃，`exp_log_dir` 是 str 但代码用了 `/` Path 运算符
- **Root Cause**: `exp_log_dir = str(log_dir / "experiment_logs")` 后，又写了 `metrics_file = exp_log_dir / "run_metrics.json"`，str 不支持 `/` 运算符
- **Solution**: 改为 `metrics_file = Path(str(exp_log_dir)) / "run_metrics.json"`
- **Prevention**: 路径操作统一用 Path 对象，不要混用 str 和 Path

### 2026-06-28 - 系统 python3 缺少项目依赖（libGL、pip 包），必须使用 uv venv

- **Issue**: benchmark 子进程 ImportError 链：`cv2` → `libGLX.so.0` → `libGLEW.so.2.2` → `matplotlib` → `fastapi`，全部 100 轮在 0.3s 内崩溃
- **Root Cause**: benchmark 使用 `sys.executable`（系统 `/usr/bin/python3`）启动子进程，但项目依赖只在 uv venv（`.venv/`）中安装。系统 python3 缺少 OpenCV 所需的 `libGLX.so.0`、libGLEW 等系统级图形库，以及 `anthropic`/`fastapi`/`matplotlib` 等 Python 包。
- **Solution**:
  1. 在 `benchmark.py` 子进程 env 中添加 `LD_LIBRARY_PATH` 指向 conda lib（含完整 libGL/libGLEW）
  2. 安装缺失 Python 包：`anthropic`、`uvicorn`、`fastapi`、`matplotlib`
  3. 添加 `_PROJECT_ROOT` 到子进程 `PYTHONPATH`（`sar_orch` 模块需要从项目根 import）
  4. **关键修复**：改用 `uv run python` 启动 benchmark，`sys.executable` 变为 `.venv/bin/python3`，子进程自动拥有所有依赖
- **Prevention**: 始终用 `uv run python` 而非 `python3` 运行任何需要项目依赖的命令。检查 `pyproject.toml` 确保所有依赖声明完整。

### 2026-06-29 - benchmark run_metrics.json 在子进程被 kill 时丢失
- **Issue**: Benchmark 所有子进程超时被 `proc.kill()` 后，没有任何实验产生 `result.json`。`aggregate.py` 无法聚合任何数据。
- **Root Cause**: `experiment.py:286-288` 在 `try/finally` 块**之外**写 `run_metrics.json`。`proc.kill()` 发送 SIGTERM，`finally` 块执行（停 workers/coordinator/barrier/close logger），但**不执行** `try` 块之外的代码。因此 `run_metrics.json` 永不生成。
- **Solution**: 将 `run_metrics.json` 写入移到 `finally` 块内，或在 poll loop 中定期写入（类似 `flush_summary()` 模式）。
- **Prevention**: 子进程的超时 kill 必须假设 `finally` 之后还有代码需要执行。应把关键输出放在 `finally` 中。

### 2026-06-29 - max_steps 与 run_timeout 不匹配导致 benchmark 必然超时
- **Issue**: Scene 1 的 `max_steps=1200`，poll loop 每 2s 轮询一次，理论上需要 2400s 才能达到上限。但 benchmark 的 `--run-timeout 900` 在 900s 就 kill 了子进程。即使 agent 正常推进，900s 也不足以完成 1200 步。
- **Root Cause**: benchmark 的 `run_timeout` 和场景的 `max_steps` 来自两个独立的配置源，没有做一致性检查。
- **Solution**: benchmark 应自动计算 `run_timeout = max_steps * avg_step_time * safety_factor`，或从场景配置读取。
- **Prevention**: 子进程超时时间必须 >= 场景所需的理论最大时间。固定值配置容易与场景参数脱节。

### 2026-06-29 - Agent 在 GPS 工具后停滞，永不消耗 step（Benchmark 全面超时根因）
- **Issue**: Benchmark 16 个 Scene 1 运行全部超时 900s，0 个成功。日志显示所有 agent 只在 Step 0 调了 `get_agent_state()`（GPS），之后 LLM 返回文本而非工具调用，Agent 循环终止。Barrier 步进计数器永远 = 0，poll loop 永不退出直到被 kill。
- **Root Cause**: 双重原因叠加：
  1. **GPS 工具不消耗 step**：`get_agent_state()` 绕过 barrier 直接读 env，调完后 barrier 的 step 计数器不增加。Agent 框架在 LLM 返回文本（而非工具调用）时自动结束循环（`agent.py:504`）。
  2. **Coordinator prompt 退化**：Coordinator 的 system prompt 明确说"不要派探索任务"，但 DeepSeek 仍然派了"报告你发现了什么"的探索性任务，Agent 执行完 GPS 后没有后续行动指令。
  3. **LLM 非确定性行为**：同一 prompt 下 DeepSeek 有时返回工具调用链（Phase 2 seed=42 成功），有时只返回文本（benchmark seeds 全部失败）。
- **Solution**: 尚未修复。需要从以下方向解决：
  1. 确保 Agent 在返回文本后继续生成工具调用，而不是结束循环（修改 `agent.py` 中 LLM response 处理逻辑）
  2. 增强 Coordinator prompt 约束力，使用更强硬的措辞或 few-shot 示例
  3. 或让 `get_agent_state()` 在调完后自动触发一个"空步进"以推动 barrier 前进
- **Prevention**: Agent 框架中，对于"仅查询不消耗 step"的工具，应在 LLM 返回文本时自动补充一个 NoOp 步进请求，防止 barrier 永久停滞。Benchmark 矩阵应包含已验证的 seed 作为冒烟测试。

### 2026-06-30 - benchmark progress 跟踪未区分 success/timeout

- **Issue**: progress.json 中 `success=100` 但实际所有子进程都崩溃了，真正的 `status="timeout"`。
- **Root Cause**: `run_single()` 中正常路径的 progress 更新无条件 `_GLOBAL_PROGRESS["success"] += 1`，未检查实际 `run.status`。只有 `asyncio.TimeoutError` 分支正确递增 `timeout`，正常完成的子进程即使 metrics 显示 `finished=False` 也被算作 success。
- **Solution**: 在 progress 更新处增加 `if run.status == "success"` 判断
- **Prevention**: 所有计数器操作必须与实际状态检查关联，不能假设"走到这里就是成功"。

### 2026-06-28 - 子进程 http_proxy 环境变量导致 WebSocket 连接被代理拦截
- **Issue**: benchmark 子进程中，worker WebSocket 连接 localhost 被系统代理拦截，返回 503
- **Root Cause**: 子进程继承 `http_proxy=http://172.20.176.1:7892`，`no_proxy` 设置不生效，websockets 库通过代理连接 localhost
- **Solution**: 在子进程 env 中移除 `http_proxy`/`https_proxy`/`HTTP_PROXY`/`HTTPS_PROXY`，同时设置 `no_proxy`/`NO_PROXY`
- **Prevention**: 涉及 localhost 通信的子进程应主动清除代理环境变量

### 2026-07-01 - SARBarrier 跨线程 asyncio 同步原语失效
- **Issue**: `TimeoutAgents` 列全部为 `[]`，但日志显示每步耗时 ~60s（barrier 超时），coordinator 只给 2/4 agent 分发任务。超时明明在发生但追踪为空。
- **Root Cause**: Worker 在独立线程中运行各自的 asyncio 事件循环（`threading.Thread(target=lambda: asyncio.run(run()))`），但 barrier 使用 `asyncio.Event` 和 `asyncio.Lock`。Python 3.10 中 `Event.set()` 调用 `future.set_result()` → `loop.call_soon()`（非 `call_soon_threadsafe`），导致其他线程事件循环中的等待者**永远不会被唤醒**。`asyncio.Lock` 也无法跨线程提供互斥。
- **Solution**: 将 `asyncio.Event` → `threading.Event`，`asyncio.Lock` → `threading.Lock`。`_execute_step` 改为 sync，通过 `asyncio.to_thread()` 调用。新增 `expected_step` 参数防止多 agent 同时超时导致重复执行。
- **Prevention**: 多线程 + asyncio 混用时，跨线程共享的同步原语必须用 `threading` 版本，或通过 `asyncio.run_coroutine_threadsafe()` 统一到单事件循环。`asyncio.Event/Lock` 仅限同事件循环内使用。

### 2026-07-01 - Coordinator 不给所有 agent 分发任务导致 barrier 空等 60s
- **Issue**: 4 agent 实验中，coordinator 第一轮只给 2 个 agent 分发任务，另外 2 个无任务 → 不提交 action → barrier 等 60s 超时填 NoOp → 每步浪费 60s。600s 只跑了 9 步。
- **Root Cause**: Coordinator prompt 说 "parallelize" 但没说 "必须给每个 agent 分发任务"。LLM 只给有活干的 agent 分发，其余不管。
- **Solution**: Prompt 新增 `Step Mechanics (CRITICAL)` 节，解释 barrier 机制 + 要求每轮给所有在线 agent 分发任务（含 NoOp 待命）。
- **Prevention**: 涉及同步屏障的系统，prompt 必须明确解释"所有参与者每轮必须提交"的约束，不能假设 LLM 自行推断。

### 2026-07-02 - `RunResult` 未从 schema 包导出
- **Issue**: 实验启动即失败 `ImportError: cannot import name 'RunResult' from 'Agent.worker_agent.schema'`
- **Root Cause**: `schema/schema.py` 已定义 `RunResult`，但 `schema/__init__.py` 未导出。
- **Solution**: 在 `src/Agent/worker_agent/schema/__init__.py` 和 `src/Agent/router_agent/schema/__init__.py` 补全导出。
- **Prevention**: 在 `schema/` 包中新增类后，同步检查 `__init__.py` 的导出列表。

### 2026-07-02 - 进程 shutdown 阶段 SIGABRT 崩溃（未修复）
- **Issue**: 实验主逻辑正常结束、metrics 和 CSV 已落盘后，Python 进程收到 fatal signal 6，退出码 134。
- **Root Cause**: 初步判断 daemon 后台线程中的 asyncio event loop 在解释器关闭时访问已释放对象，或 SSE/WebSocket 清理顺序不当。`dmesg` 日志显示 `python3: potentially unexpected fatal signal 6`，堆栈指向 `EventQueueSource._dispatch_loop() ... was cancelled without calling EventQueue.close() first`。
- **Solution**: 未修复。需显式关闭 worker/coordinator 的 event loop 和 WebSocket，或在 `finally` 中给后台线程足够退出时间；必要时用 `atexit` 注册清理。
- **Prevention**: asyncio 后台线程应注册显式清理，避免解释器关闭时访问已释放对象。

### 2026-07-02 - `finish_task` 工具未实际被调用（未修复）
- **Issue**: `agent_interactions.csv` 中未见 `finish_task`；子任务完成后 Worker 调用 `no_op()` 并返回文本摘要。`require_explicit_completion=True` 的退出逻辑无法通过 `finish_task` 触发。
- **Root Cause**: `sar_orch/prompts/worker/system.md` 的 Available Tools 列表里没有 `finish_task`，Critical Rules 也指导使用 `no_op()` 等待。工具已注册但 LLM 不知道该用。
- **Solution**: 未修复。需在 Worker prompt 中加入 `finish_task` 工具说明，并修改 Rule 6 引导子任务完成时调用 `finish_task(success=..., summary=..., task_description=...)`。
- **Prevention**: 新增工具后必须同步更新对应 role 的 system prompt。

### 2026-07-02 - `agent_adapter.py` ruff E402 违规（未修复）
- **Issue**: `uv run --with ruff ruff check` 报 10 处 E402（Module level import not at top of file），全部在 `src/a2a/worker/agent_adapter.py`。
- **Root Cause**: `logger = logging.getLogger(__name__)` 之后做模块级导入。
- **Solution**: 未修复。需将 `src/a2a/worker/agent_adapter.py` 的导入全部移到文件顶部。
- **Prevention**: 模块级导入必须放在文件顶部，logger 初始化前不引入副作用导入。

### 2026-07-02 - `ContextManager.assemble()` 隐式假设脆弱（未修复）
- **Issue**: `result.extend(messages[1:])` 假设 `messages[0]` 是 system prompt。如果调用方历史不以 system 开头，会误删第一条消息。
- **Root Cause**: 编码时未考虑非标准消息序列。
- **Solution**: 未修复。当前 SAR 路径满足假设，但属于潜在隐患。
- **Prevention**: 不要假设消息列表的绝对顺序；应通过消息 role 类型定位 system prompt。

### 2026-07-01 - Auto-NoOp 让 coordinator 变懒，给出过短任务链
- **Issue**: 实现 worker 自动 no_op 后，coordinator 只给 2 步任务（NavigateTo + GetSupply），知道 worker 会自动填充。步骤 3-7 全是 NoOp，浪费 5 步预算。覆盖率从 100% 降到 67%。
- **Root Cause**: Prompt 说"不用平衡任务长度"，LLM 理解为"可以给短任务"。Auto-NoOp 本应是安全网，却成了 coordinator 偷懒的借口。
- **Solution**: Prompt 强调"给最长可能的动作链"，新增 bad example（2 步任务），要求"每个 agent 都要有有用任务，NoOp standby 仅用于真正无事可做时"。
- **Prevention**: 给 LLM 减负的 prompt 改动可能产生反效果——LLM 会过度依赖安全网。安全网机制（auto-no_op）的 prompt 描述应同时强调"仍应尽力给出最长链"。

### 2026-07-04 - Worker `ask_coordinator` 中断后 Coordinator 编排循环挂死
- **Issue**: 将 Worker prompt 从完成任务后 `no_op()` 等待改为 `ask_coordinator()` 询问指令后，Worker 正确调用了 `ask_coordinator`（日志可见 `[PAUSE] question=...`），但协调器的编排循环永久挂死，实验最终被 600s 墙钟超时截断。
- **Root Cause**: `ask_coordinator` → `NeedInputError` → `Agent.run()` 返回 `RunResult(need_input=True)` → `AgentAdapter.execute()` 调用 `TaskUpdater.requires_input()` → Worker 状态变为 `INPUT_REQUIRED`。Push callback 收到状态更新后记录到 EventStore，但 `INPUT_REQUIRED` **不是 terminal 状态**，`resolve_global_future()` 不会被调用 → `collect_results` 等待的 Future 永不 resolve → 协调器永久阻塞在 `collect_results`。
- **完整阻塞链**:
  1. Coordinator `dispatch_task` → Worker 开始执行 → Worker 完成任务 → Worker `ask_coordinator` → Worker 进入 INPUT_REQUIRED
  2. Coordinator `collect_results` → Future 永不 resolve（push callback 不处理非 terminal 状态）
  3. Coordinator 无法继续下一轮调度 → 所有 Worker 无新任务 → barrier 空转
- **Solution**:
  1. 删除阻塞式 `collect_results` 工具，新增 `query_task_events` 工具：基于已有 push callback 通道，从 `EventStore` 查询每个任务当前状态（`RUNNING/COMPLETED/FAILED/CANCELED/INPUT_REQUIRED`），支持短超时等待 actionable 事件。
  2. 在 `agent_executor.py` 工具列表中用 `QueryTaskEventsTool` 替换 `CollectResultsTool`。
  3. Coordinator prompt 新增 `Handling Worker Status` 章节：遇到 `INPUT_REQUIRED` 立即调用 `respond_worker(task_id, response)` 回复，然后继续 `query_task_events` 直到任务完成。
  4. 修复 `respond_worker`：支持传入 worker UUID 或 dispatch task_id；A2A message 使用 worker UUID。
  5. `EventStore` 新增结构化 `get_task_state()`；`TaskStore` 维护 dispatch_id ↔ worker_id 双向映射；`dispatch_task` 失败时写入 EventStore。
  6. 将 `router_max_steps` 从 20 提升到 200，为查询式编排提供足够步数预算。
- **Prevention**: 涉及同步屏障/中断机制的系统，prompt 必须明确解释"所有参与者每轮必须提交"以及"INPUT_REQUIRED 必须立即回复"的约束；阻塞等待工具无法处理非 terminal 状态时，应改为事件查询 + 主动响应模型。

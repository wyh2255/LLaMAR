# SAR Console — 一键启动控制台与实时监控前端

> 适用范围:`sar_orch/console/`、`sar_orch/user_command_queue.py`，以及 `src/a2a/coordinator/server.py`、`sar_orch/coordinator_state_provider.py`、`src/Agent/router_agent/context.py` 中相关的扩展点。
> 配套文档:[框架.md](框架.md)(整体架构)、[data_flow.md](data_flow.md)(通用数据流)、[logging_map.md](logging_map.md)(NDJSON 日志格式)。

## 1. 概述

SAR Console 是一个独立的 Web 控制台，用于交互式地运行和观察 SAR 多智能体实验，覆盖四个需求:

1. **一键启动/停止实验**:UI 配置 scene/agents/seed/model/mode/max_steps/初始任务，点击后以子进程拉起 `sar_orch/experiment.py`(coordinator + SAR 仿真 + workers 一次全部就绪)。
2. **运行中途注入用户指令**:UI 输入自然语言命令，注入 coordinator router 的下一轮 LLM 上下文（不打断当前轮）。
3. **实时交互仪表盘**:coordinator 的决策与工具调用、每个 worker 的工具调用、agent 间交互事件，统一按时间归并展示。
4. **实时 Mission Graph**：任务 DAG 节点（状态着色）、依赖边、节点详情、按 worker 分组的 dispatch 记录。

设计约束：控制台与实验进程**完全隔离**（实验崩溃不影响控制台）；不改动 `experiment.py` / `benchmark.py`；复用 coordinator 已有的 SSE/日志设施，不引入新的事件总线。

## 2. 架构总览

```
浏览器 ──> SAR Console 服务 :9000  (sar_orch/console/server.py, FastAPI)
             │ ① run control   asyncio.create_subprocess_exec(sar_orch/experiment.py)
             │ ② log SSE       tail 实验 log_dir 下的 *.ndjson(replay + follow)
             │ ③ command fwd   POST /api/command → coordinator :8080 /api/user-command
             │ ④ reverse proxy GET /coord/* → coordinator :8080/*(SSE 透传)
             ▼
        experiment.py 子进程 (默认端口 8080/8081/8191+, 可用 --coordinator-port 换组)
             │  内部: coordinator server (uvicorn 线程) + SARBarrier + N 个 worker 线程
             ▼
        UserCommandQueue → SARCoordinatorStateProvider.snapshot()
             → payload["user_commands"] → CoordinatorPinnedState
             → Context Memory "### User Commands" (下一轮 pre_llm 生效)
```

关键决策:

- **子进程模式**(同 `benchmark.py`)而非 in-process：端口冲突可换组、崩溃隔离、停止=杀进程即全清（worker/coordinator 都是实验进程内的线程，无孙进程）。
- **日志流走文件 tail** 而非订阅：`src/a2a` 没有 pub/sub 机制，coordinator 已有的 `/logs/{task_id}/stream` 就是文件 tail 模式，控制台对实验 log_dir 做同样的事，零侵入。
- **反向代理同源化**:`/coord/*` 代理 coordinator，前端只跟 :9000 通信，避免 CORS。
- **命令注入走 pinned-state 链路**:router 的 `pre_llm` 每轮都会刷新 runtime state，命令排进队列后在下一轮 Context Memory 出现恰好一次（drain 语义），无需侵入 agent loop。

## 3. 组件详解

### 3.1 控制台服务 `sar_orch/console/server.py`

启动:

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python -m sar_orch.console.server --port 9000
```

全局单 run 状态 `RunState`(proc / params / log_dir / started_at / log_file)，同一时间只允许一个实验。

**子进程环境 `_build_sub_env()`**（与 benchmark.py 的重要差异）:

- `PYTHONPATH` 前置 `<project>:<project>/src`。
- **保留** `http_proxy`/`https_proxy`:`.env` 里的 LLM 网关（`api_base`)可能只能经代理访问；只把 `no_proxy` 和 `NO_PROXY` 都扩展上 `localhost,0.0.0.0,127.0.0.1,::1`，保证 worker↔coordinator 本机通信绕过代理。
- 对照:`benchmark.py` 是**剥离**代理变量的（其目标环境直连 LLM API 可达）。
- `experiment.py` 的 `--model/--provider/--api-base` CLI 默认值本来就取自 `.env`，控制台无需显式传递。

**子进程日志**:stdout/stderr 合并写入 `<log_dir>/console_subprocess.log`（行缓冲），`/api/run/status` 返回末尾 50 行。

**log_dir 约定**:`sar_orch/results/console_<YYYYMMDD_HHMMSS>_s<scene>_seed<seed>_a<agents>/`。

**路径安全**:`/api/logs/stream` 的 `path` 参数经 `resolve()` 后必须 `is_relative_to(log_dir)` 且后缀为 `.ndjson`，防目录穿越。

### 3.2 API 一览（控制台 :9000)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单文件前端 `index.html` |
| POST | `/api/run/start` | body `{scene, agents, seed, model, mode, max_steps, task}`;409 = 已有活跃 run |
| POST | `/api/run/stop` | terminate → 5s 宽限 → kill；成功返回 `{stopped:true, exit_code}`，无活跃 run 返回 `{stopped:false, detail}` |
| GET | `/api/run/status` | `{running, pid, exit_code, params, log_dir, started_at, uptime_s, log_tail[50]}` |
| GET | `/api/logs` | 递归发现 log_dir 下 `*.ndjson`，返回 `{log_dir, streams: [{path, source, size, mtime}]}`;source 推断：一级子目录名（worker)、`events.ndjson`→events、其余→coordinator |
| GET | `/api/logs/stream?path=` | SSE:replay 已有行后 300ms 轮询追新；文件尚不存在时等待其出现；文件截断重建时从头读 |
| POST | `/api/command` | `{text}` → 转发 coordinator `/api/user-command`;text 空白 → 400,coordinator 不可达 → 502,coordinator 返回非 200 时原样透传其状态码与 detail |
| GET | `/coord/{path:path}` | 反代 coordinator（仅 GET);`map/state`、`dashboard/stream` 走流式透传，其余 JSON 透传 |

### 3.3 Coordinator 侧新增端点（`src/a2a/coordinator/server.py`)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/mission-graph` | 返回 `SARCoordinatorStateProvider.mission_graph_snapshot()`:`{step_budget, mission_finished, mission_dag_view, physical_dispatches_view, task_status_view}`;state_provider 无此方法 → 404 |
| POST | `/api/user-command` | body `{text, source?}` → `UserCommandQueue.put()`;queue 未注入 → 503,text 空白 → 400 |

配套:`CoordinatorServer.set_user_command_queue(queue)`（仿 `set_barrier` 模式，在 `sar_orch/coordinator.py:start()` 中与 `set_barrier`/`set_semantic_map` 一起注入）。

### 3.4 用户命令注入链路

```
UI POST /api/command
  → console POST :8080/api/user-command
  → UserCommandQueue.put(text)                       # sar_orch/user_command_queue.py
  → SARCoordinatorStateProvider.snapshot()            # router 每轮 pre_llm 调用
      drain() → payload["user_commands"]
      _runtime_version += 1                           # 同一 env step 内也强制刷新
  → CoordinatorContextManager._project_runtime_state_to_pinned()
      pinned.user_commands = payload.get("user_commands", [])   # 无新命令时清空
  → CoordinatorContextManager._render_current_state()
      "### User Commands" 小节（标注为高优先级人工指令）
```

要点:

- `UserCommandQueue` 用 `threading.Lock`（遵循 ADR-011:coordinator HTTP 线程与 router 线程跨线程，禁用 asyncio 原语）；容量上限 50，满则丢弃最旧。
- **drain 语义**：每条命令只在下一轮 Context Memory 出现一次，之后从 pinned state 清除，不会反复占用上下文。
- **生效时机**：下一轮 `pre_llm`，不打断进行中的 LLM 调用；命令不会作为独立 mission 提交（`MissionRuntimeManager` 同时只允许一个 active runtime)。
- 测试覆盖:`tests/test_user_command_injection.py`(10 个用例：FIFO/有界/线程安全/注入一次/version 递增/投影清除/渲染）。

### 3.5 前端 `sar_orch/console/index.html`

单文件、零依赖（无 CDN/框架），三栏网格布局 `320px 1fr 420px`，暗色主题对齐 `sar_orch/ui/dashboard/index.html`。

- **左栏 Run Control**：表单 + Start/Stop（内联红/绿结果提示，禁用态有 title 解释）+ run 信息 + 可折叠进程日志尾；status 轮询 2s。
- **中栏 Mission Graph**：轮询 `/coord/api/mission-graph`(1.5s);SVG 按 `depends_on` 最长路径分层渲染，节点按状态着色（pending 灰 / ready·dispatched 蓝 / running 青 / completed 绿 / failed 红 / canceled 暗 / input_required 琥珀）；点击节点出详情面板（objective、assignments、依赖、该节点的 dispatch 行）；下方 Per-worker dispatches 表按 worker 分组。
- **右栏 Live Feed**:`/api/logs` 轮询 3s，每条新流开一个 `EventSource`；事件按到达序归并，source 着色徽标（coordinator 紫 / events 青 / worker 按名字哈希色相），长文本折叠，500 条上限，来源过滤 chips，自动滚动开关；底部命令输入框（Enter 发送，本地回显 source="you")。

**关键前端逻辑 — `effectiveNodes(graph)`**:coordinator 经常直接派任务而不调 `update_plan`，此时 `mission_dag_view` 为空但 `physical_dispatches_view` 有数据。前端在 DAG 为空时**从 dispatches 合成节点**（每个 `logical_node_id` 一个，多 dispatch 合并 participants，状态按活跃优先 `input_required > running > ready/dispatched/pending > failed > completed/succeeded > canceled`)；仅当两个视图都为空才显示 "waiting for mission graph…"。step 头、节点详情、per-worker 表每次轮询独立渲染，不依赖 DAG 非空。

## 4. 端口布局

| 端口 | 占用者 | 说明 |
|---|---|---|
| 9000 | console server | `--port` 可改 |
| 8080 | coordinator HTTP/WS | 实验子进程；console 固定代理此端口 |
| 8081 | coordinator A2A | coordinator_port + 1 |
| 8191+ | worker A2A | agent_base_port + i |

**不要**在 console 运行期间并行跑 benchmark 或另一个 experiment（默认端口组相同）。console 当前未暴露端口参数（`RunState.coordinator_url` 读 `params.coordinator_port`，默认 8080)；如需换组，需给 `/api/run/start` 加 `coordinator_port`/`agent_base_port` 字段并透传 CLI——这是已知的后续扩展点。

## 5. 故障排查（已踩过的坑)

- **实验起来 16s 就 `framework_error` + LLM Connection error**：子进程代理变量被剥掉，而 `.env` 的 `api_base` 只能走代理。console 已改为保留代理（见 3.1)。
- **Stop 后"后台还在交互"**：两种假象——①实验是由**另一个已被杀掉的 console 实例**启动的孤儿（owner 死了，新实例显示 no active run,Stop 自然无效）;②Stop 生效后 log SSE 仍在 replay 文件 backlog,feed 继续滚动看起来像还在跑。真实验证：看 `/api/run/status` 的 `running` 和 8080 端口是否释放。
- **Mission Graph 长期 "waiting…"**：先 curl `/coord/api/mission-graph` 区分是后端 404/502(coordinator 没起来/run 已结束）还是 `mission_dag_view: []`（本 run 没走 update_plan，应看合成节点）。
- **点击 Start 无反应**：有活跃 run 时按钮禁用（悬停有提示）；启动后 coordinator 约需 15s 才会有 mission graph 数据。
- **本仓库 venv 跑实验需 `matplotlib`**(SAR/utils.py 依赖）;`numpy`/`cv2` 已在，SAR 不用 pygame/PIL。
- **pytest 环境**：系统 Python3.10 的 pytest 会加载 ROS 插件并崩溃；正确姿势 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --with pytest --with pytest-asyncio python -m pytest -p pytest_asyncio.plugin tests/ -q`（其中 5 个测试文件依赖 SAR 运行时，venv 装 matplotlib 前会 collection error，属既有问题）。

## 6. 文件清单

| 文件 | 说明 |
|---|---|
| `sar_orch/console/server.py` | 控制台后端（run control / log SSE / command 转发 / 反代） |
| `sar_orch/console/index.html` | 单文件前端（Run Control / Mission Graph / Live Feed / 命令框） |
| `sar_orch/user_command_queue.py` | 线程安全的用户命令队列 |
| `sar_orch/coordinator_state_provider.py` | `submit_user_command()`、`mission_graph_snapshot()`、snapshot() drain 注入 |
| `sar_orch/coordinator.py` | 创建 queue 并注入 provider 与 server |
| `src/a2a/coordinator/server.py` | `set_user_command_queue()`、`/api/mission-graph`、`/api/user-command` |
| `src/Agent/router_agent/context.py` | `CoordinatorPinnedState.user_commands` + 投影白名单 + `### User Commands` 渲染 |
| `tests/test_user_command_injection.py` | 注入链路测试(10 用例) |

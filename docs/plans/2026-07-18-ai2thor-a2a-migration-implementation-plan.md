# AI2Thor → A2A 迁移：分阶段实施计划

> 姊妹文档：`docs/plans/2026-07-18-ai2thor-a2a-migration-design.md`（架构设计，下称「设计文档」）。
> 本文档回答的是**怎么落地**：任务拆分、依赖顺序、每步的验收门禁、测试矩阵、风险与回滚。
> 所有路径、类名、行号均在 2026-07-18 对照真实仓库核查过；与设计文档冲突处以本文档的现状描述为准。

---

## 0. 现状基线（核查结论）

### 0.1 已存在

| 资产 | 位置 | 说明 |
|------|------|------|
| 设计文档 | `docs/plans/2026-07-18-ai2thor-a2a-migration-design.md` | untracked，未提交 |
| `ai2thor` extra 依赖组 | `pyproject.toml` L73-81 | **未提交修改**；含 ai2thor/scipy/pandas/torch/sentence-transformers/opencv-python |
| `uv.lock` | 仓库根 | **未提交修改**；与 `pyproject.toml` 的 ai2thor extra 同步 |
| wheel packages 声明 | `pyproject.toml` L113-114 | `ai2thor_orch` 已列入 `packages`，但目录尚不存在 |
| pytest 配置 | `pyproject.toml` L116-123 | `testpaths` 含 `ai2thor_orch`；`unity` marker 已注册 |
| 旧版 AI2Thor 代码 | `AI2Thor/`、`thortils/` | 非合法 Python 包（无 `__init__.py` 治理），靠 `sys.path` 注入 |
| A2A 协调器 | `src/a2a/coordinator/server.py` | 含 SAR 硬编码：`_barrier`、`set_barrier()`、`_do_cancel_experiment()` |
| TaskWatchdog | `src/a2a/coordinator/task_watchdog.py` | `_check_all()` 直读 `self._barrier._step_counter`、`_refresh_progress_by_domain_delta()` 调 `get_metrics()` — SAR 耦合 |
| StateProvider 协议 | `src/Agent/router_agent/state_provider.py` | 环境无关的 `RuntimeState` DTO + `StateProvider` Protocol，可直接复用 |
| 优雅停机助手 | `src/a2a/shared/server_lifecycle.py` | `shutdown_uvicorn_server()`、`shutdown_a2a_active_tasks()` |
| SAR 参考实现 | `sar_orch/barrier.py`、`sar_orch/coordinator_state_provider.py`、`sar_orch/worker_state_provider.py` | 线程模型（`threading.Event`+`threading.Lock`）已验证可复用 |
| SAR 回归测试 | `tests/test_sar_*.py`、`tests/test_task_watchdog.py`、`tests/test_server_lifecycle.py`、`tests/test_coordinator_state_provider.py` 等 50 个文件 | 每次改 A2A 共享层后必须回归 |
| 模型元数据 | `~/.hermes/models_dev_cache.json` | kimi-k3: context 1048576 / output 131072 |

### 0.2 尚不存在（全部为新建）

| 缺失项 | 设计文档中的角色 |
|--------|------------------|
| `ai2thor_orch/` 整个包 | 新 orchestration 层（contracts / executor / barrier / experiment / benchmark / prompts / tools / state providers / tests） |
| `src/a2a/coordinator/run_control.py` | `EnvironmentRunControl` Protocol |
| `scripts/ai2thor_runtime_smoke.py` | G1 Unity 探针 |
| `AI2Thor/__init__.py`、`thortils/__init__.py` 等包治理 | 消除 `sys.path` 注入 |
| `ai2thor_orch/tests/` | 全部单测 |

### 0.3 关键集成缝（改动面最大的 4 处）

1. **`src/a2a/coordinator/server.py`**
   - L267-268 `self._barrier = None`，L278-280 `set_barrier()` —— SAR 专用注入点。
   - L413-475 `_do_cancel_experiment()` —— 直接 `self._barrier.stop()`，无 `run_control` 守卫。
   - L316 lifespan 内 `self._task_watchdog._barrier = self._barrier`。
   - L692/L718/L768/L784/L811 push-callback 里 `self._barrier._step_counter`。
   - L837-889 `/map/state` SSE 与 L890+ `/dashboard/stream` —— 调 `get_env_snapshot()`/`get_metrics()`/`get_trajectory_history()`/`get_observation_stream()`，AI2Thor 不接入，需加守卫防止空转或 500。

2. **`src/a2a/coordinator/task_watchdog.py`**
   - L126-129 `_check_all()` 用 `getattr(self._barrier, "_step_counter", 0)`。
   - L202-209 `_refresh_progress_by_domain_delta()` 调 `self._barrier.get_metrics()`，返回 SAR 指标（coverage/transport_rate），AI2Thor 语义不同。

3. **`src/a2a/worker/agent_adapter.py`**
   - L73 已接受 `state_provider`；L120-129 `build_controller()` 走 `src/Agent/worker_agent/build.py`。
   - **没有 barrier 注入点**：L87 `self._extra_tools` 是静态工具列表，AI2Thor worker 工具需要持有 `AI2ThorBarrier` 引用 —— 这是新增 seam，不是改现有签名。

4. **Context manager 层**
   - `src/Agent/router_agent/context.py` `CoordinatorContextManager`（L748+）的 `_render_environment_view()` 是 SAR 专属（fires/persons/reservoirs）。
   - `src/Agent/worker_agent/context.py` `WorkerContextManager`（L816+）同理。
   - `CoordinatorPinnedState` / `WorkerPinnedState` 字段是 SAR schema —— AI2Thor 需要**子类化**而不是改字段，否则破坏 SAR 回归。

---

## 1. 总体策略

- **分支**：`feat/ai2thor-scene-adaptation`（当前分支），按阶段切子分支或逐个 PR 合入。
- **运行模式**：`LLAMAR_AI2THOR_MODE=fake|unity`，默认 `fake`。CI 与本地开发只跑 fake；unity 仅在远程 A100 上显式开启。
- **提交纪律**：`pyproject.toml` + `uv.lock` 的 ai2thor extra 作为 **G0 的第一个 commit** 先落盘，后续每个阶段独立 commit，保证 `git bisect` 可用。
- **回归红线**：任何对 `src/a2a/`、`src/Agent/` 共享层的修改，必须跑 SAR 回归集（见 §6.3）全绿才可合并。

---

## 2. 阶段拆解

### G0 — 依赖闭包与包治理（0.5 天）

**目标**：`uv sync --extra ai2thor` 可复现；`AI2Thor/`、`thortils/` 成为合法包；不再有 `sys.path` 注入。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G0.1 | 提交 ai2thor extra + lock | `pyproject.toml`、`uv.lock` | 把当前未提交的改动作为独立 commit：`chore(deps): add ai2thor optional extra` |
| G0.2 | 包治理：加 `__init__.py` | `AI2Thor/__init__.py`、`AI2Thor/Tasks/__init__.py`、`thortils/__init__.py` | 空文件或仅导出公共符号；**不改**旧代码逻辑 |
| G0.3 | 消除 `sys.path` 注入 | `sar_orch/barrier.py` L20-22、`AI2Thor/env_new.py` L17 | 改为正常 `import AI2Thor...` / `import thortils...`；在 `pyproject.toml` 用 `tool.uv.sources` 或 editable path 声明本地包 |
| G0.4 | 创建 `ai2thor_orch/` 骨架 | `ai2thor_orch/__init__.py`、`ai2thor_orch/tests/__init__.py` | 空包，让 wheel packages 声明不再悬空 |
| G0.5 | 验证收集 | — | `PYTHONPATH="src:$PYTHONPATH" uv run pytest --collect-only -q` 应收集到 ≥29 个测试且不报 import 错误 |

**验收门禁**

```bash
cd /home/wyh/daily_work/LLaMAR-ai2thor
uv sync --extra ai2thor
PYTHONPATH="src:$PYTHONPATH" uv run pytest --collect-only -q   # 0 errors
uv run python -c "import AI2Thor, thortils; print('ok')"       # 无 sys.path hack
```

**回滚**：`git revert` G0.2/G0.3 两个 commit 即可；不影响 SAR。

---

### G1 — Unity runtime 探针（0.5 天，远程 A100）

**目标**：产出机器可读的 Unity 启动报告，确认远程宿主可用；失败时明确阻断原因。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G1.1 | 新建探针脚本 | `scripts/ai2thor_runtime_smoke.py` | 独立可运行；初始化 `ai2thor.controller.Controller`，加载一个 FloorPlan，执行 `Reset` + 一个 `MoveAhead`，输出 JSON 报告（版本、scene、agent 数、metadata schema 关键字段、耗时、错误堆栈） |
| G1.2 | 支持 `--report` 写文件 | 同上 | `--report PATH` 写 JSON，供 CI artifact 收集 |
| G1.3 | 在远程 A100 执行 | — | `LLAMAR_AI2THOR_MODE=unity uv run python scripts/ai2thor_runtime_smoke.py --report reports/unity_smoke.json` |

**验收门禁**

- 报告包含 `"status": "ok"`、`scene` 非空、`metadata` 含 `objects` / `agent` / `reachablePositions`（或当前版本等价字段）。
- 若失败：报告必须含 `"status": "error"` + `traceback`，并在计划文档的风险登记册里记录阻断原因。

**风险**：本机无 GPU；远程 A100 若缺 Unity build 则 G1 硬阻断。**备选**：先在本地 WSL 用 fake 模式推进 G2，G1 与 G2 并行。

---

### G2 — 契约 / ControllerExecutor / AI2ThorBarrier（2-3 天，核心）

**目标**：fake 模式下环境回合语义完备，单测覆盖状态机、预算、超时、取消。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G2.1 | 数据契约 | `ai2thor_orch/contracts/__init__.py`、`ai2thor_orch/contracts/types.py` | `ActionRequest` / `ActionResult` / `RoundResult` / `PublicObservation` / `CoordinatorObservation` / `AgentPublicState` / `RunStatus` dataclass；纯数据、零依赖 |
| G2.2 | ControllerExecutor | `ai2thor_orch/executor/__init__.py`、`ai2thor_orch/executor/controller_executor.py` | `ThreadPoolExecutor(max_workers=1)` 包装 ai2thor Controller；串行执行 `step()`；注入 fake/unity 工厂；`stop()` 幂等；**禁用 `asyncio.to_thread` 默认 executor 复用**（避免与 wait_round 死锁） |
| G2.3 | AI2ThorBarrier | `ai2thor_orch/barrier/__init__.py`、`ai2thor_orch/barrier/ai2thor_barrier.py` | 复用 `sar_orch/barrier.py` 的 `threading.Event`+`threading.Lock` 模式；实现 `submit_action()` / `wait_round()` / `request_stop()` / `stop()` / `get_run_status()` / `snapshot_public()` / `snapshot_coordinator()`；round deadline 超时自动 NoOp |
| G2.4 | BudgetLedger | `ai2thor_orch/budget/__init__.py`、`ai2thor_orch/budget/ledger.py` | 记录每回合估计 token、实际 token、剩余预算；供 coordinator state provider 读取 |
| G2.5 | Fake Controller | `ai2thor_orch/tests/fakes.py` | 确定性 fake：`reset()` 返回固定 metadata；`step()` 按脚本推进；支持注入失败、延迟、超时场景 |
| G2.6 | 单测 | `ai2thor_orch/tests/test_contracts.py`、`test_executor.py`、`test_barrier.py`、`test_budget.py` | 覆盖：正常回合、超时自动 NoOp、request_stop 唤醒所有 waiter、executor 串行性、预算耗尽边界 |

**验收门禁**

```bash
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests -m "not unity" -v
```

全绿；且 `test_barrier.py` 中包含：
- 2 agent 正常提交 → round 推进；
- 1 agent 超时 → 自动 NoOp，round 仍推进，`timeout_agents=[idx]`；
- `request_stop()` 后所有 `wait_round()` 立即返回 `stopped=True`。

**风险（来自子代理审查）**：单线程 executor 不能同时持有 action execution 和事件等待 —— `wait_round()` 必须在 **asyncio 侧** 等 `threading.Event`，不能 `await asyncio.to_thread(...)` 复用同一 executor，否则死锁。实现时把 `wait_round()` 写成 `asyncio.wrap_future` / 直接 `loop.run_in_executor(None, event.wait)` 的**独立线程**，与 Controller executor 分离。

---

### G3 — A2A 生命周期统一（EnvironmentRunControl）（1.5 天）

**目标**：cancel/shutdown 不再硬编码 `barrier.stop()`；AI2Thor 与 SAR 共用同一停止协议；SAR 行为零回归。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G3.1 | 新建协议 | `src/a2a/coordinator/run_control.py` | `EnvironmentRunControl` Protocol：`request_stop(reason: str) -> None`、`stop() -> None`、`get_run_status() -> RunStatus`；`RunStatus` 是环境无关 DTO（step/max_steps/finished/stopped/timeout_agents/domain_metrics: dict） |
| G3.2 | SAR 适配器 | `sar_orch/barrier.py`（改） | 给 `SARBarrier` 增加 `get_run_status()`，返回 `RunStatus(step=_step_counter, ..., domain_metrics={"coverage":..., "transport_rate":...})`；`request_stop(reason)` 记录 reason 后调 `stop()` |
| G3.3 | Server 改造 | `src/a2a/coordinator/server.py` | 新增 `set_run_control(rc: EnvironmentRunControl)`；`_do_cancel_experiment()` 改为 `if self._run_control: self._run_control.request_stop("cancel:"+context_id)`；push-callback 里 5 处 `self._barrier._step_counter` 改为 `self._run_control.get_run_status().step if self._run_control else 0`；**保留** `set_barrier()` 仅供 `/map/state`、`/dashboard/stream` 使用 |
| G3.4 | SSE 守卫 | `src/a2a/coordinator/server.py` L837+ | `/map/state`、`/dashboard/stream` 在 `self._barrier is None` 时返回 404 而非空转（现状是 `yield waiting` 死循环）；AI2Thor 模式不调用 `set_barrier()`，自然 404 |
| G3.5 | TaskWatchdog 条件化 | `src/a2a/coordinator/task_watchdog.py` | `_check_all()` 与 `_refresh_progress_by_domain_delta()` 不再直读 `_barrier`；改为读 `run_control.get_run_status()`，domain delta 仅在 `domain_metrics` 含 SAR 键时生效；AI2Thor 模式下注入 `run_control` 但不注入 `barrier`，watchdog 自动退化为通用超时检测 |
| G3.6 | AI2Thor 适配 | `ai2thor_orch/barrier/ai2thor_barrier.py` | `AI2ThorBarrier` 实现 `EnvironmentRunControl` |
| G3.7 | 测试 | `tests/test_run_control.py`（新）+ 回归 | 新协议单测；SAR 回归集全绿 |

**验收门禁**

```bash
PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_run_control.py -v
PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_sar_barrier_observability.py tests/test_task_watchdog.py tests/test_server_lifecycle.py tests/test_coordinator_push_callback.py -v
```

---

### G4 — Worker 工具 / StateProvider / Context（2 天）

**目标**：AI2Thor worker 有可用的受限工具集；coordinator/worker 的 Context Memory 不渲染 SAR 字段。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G4.1 | AI2Thor worker 工具 | `ai2thor_orch/tools/worker/*.py` | `move_ahead` / `rotate_left` / `rotate_right` / `look_up` / `look_down` / `pickup` / `put` / `open` / `close` / `done`；每个工具持有 `AI2ThorBarrier` + `agent_idx` 引用，调用 `barrier.submit_action()`；禁止 bash/file 工具（`include_base_tools=False`） |
| G4.2 | 工具注入 seam | `ai2thor_orch/experiment/ai2thor_experiment.py` | 新建 `AI2ThorExperiment` 类，组装 worker 时把 barrier 绑进工具实例，再经 `extra_tools=[...]` 传给 `AgentAdapter`；**不改** `AgentAdapter` 签名 |
| G4.3 | Worker StateProvider | `ai2thor_orch/state/__init__.py`、`ai2thor_orch/state/worker_state_provider.py` | 实现 `StateProvider.snapshot()`，读 `AI2ThorBarrier.snapshot_public(agent_idx)`，payload 键：`position`/`rotation`/`inventory`/`step`/`visible_objects`(alias)/`current_task`/`mission_status` |
| G4.4 | Coordinator StateProvider | `ai2thor_orch/state/coordinator_state_provider.py` | 读 `snapshot_coordinator()`，payload 键：`step_budget`/`team_status_summary`/`task_status_view`/`mission_finished`/`run_status`；**不渲染** fires/persons/reservoirs |
| G4.5 | Context 子类 | `ai2thor_orch/state/context.py` | `AI2ThorCoordinatorContextManager(CoordinatorContextManager)` 与 `AI2ThorWorkerContextManager(WorkerContextManager)`：覆盖 `_render_environment_view()` 输出 AI2Thor 场景摘要；子类化保证 SAR pinned schema 不变 |
| G4.6 | 可见性 / alias | `ai2thor_orch/visibility.py` | `AliasRegistry`：raw objectId → alias（如 `CounterTop_1`）；worker 只见 alias；coordinator 可见 raw+alias；审计测试确保 worker 工具返回串中无 raw id 泄漏 |
| G4.7 | 单测 | `ai2thor_orch/tests/test_state_providers.py`、`test_visibility.py`、`test_tools.py` | 用 fake barrier 验证投影内容、alias 隔离、工具入参出参 |

**验收门禁**

```bash
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests -m "not unity" -v
# 重点：test_visibility.py 中 worker 视角快照 assert "raw_object_id" not in rendered_text
```

---

### G5 — Verifier / Benchmark / E2E（2 天）

**目标**：至少一个任务模板可端到端跑通并给出 `verified_completion=True`。

**任务**

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| G5.1 | 任务契约加载 | `ai2thor_orch/contracts/task.py` | 加载 `AI2Thor/Tasks/<task_id>/FloorPlan*.py` + `checker.py`；解析初始 inventory、目标、成功条件 |
| G5.2 | Verifier | `ai2thor_orch/verifier/__init__.py`、`ai2thor_orch/verifier/verifier.py` | `verify_postconditions(final_metadata) -> bool`；首版只支持 `3_transport_groceries` 这一类（checker 已存在，见 `AI2Thor/Tasks/3_transport_groceries/checker.py`） |
| G5.3 | Experiment runner | `ai2thor_orch/experiment/ai2thor_experiment.py` | 参照 `sar_orch/experiment.py`：启动 coordinator server + worker 进程、注入 `run_control`、round loop、写 `logs/YYYYMMDD_HHMMSS/` |
| G5.4 | Benchmark | `ai2thor_orch/benchmark.py` | 参照 `sar_orch/benchmark.py`：扫描 `AI2Thor/Tasks/*/`，组合 scene × agents × seed；`--mode fake|unity`、`--run-timeout`、断点续跑 |
| G5.5 | Unity E2E（远程） | — | `LLAMAR_AI2THOR_MODE=unity uv run python -m ai2thor_orch.experiment.ai2thor_experiment --task 3_transport_groceries --agents 2 --seed 42` |
| G5.6 | 报告 | `ai2thor_orch/tests/test_verifier.py`、`test_experiment_e2e.py`(fake) | fake 模式 E2E：2 agent 跑 5 回合内完成一次 mock transport；verifier 断言 metadata |

**验收门禁**

- fake 模式：`uv run pytest ai2thor_orch/tests/test_experiment_e2e.py -v` 全绿。
- unity 模式（远程 A100）：`logs/<ts>/summary.csv` 含 `verified_completion=True` 至少一行；`events.ndjson` 无未捕获异常。

---

## 3. 关键设计决策（实现层）

### 3.1 停止协议：`EnvironmentRunControl`

```python
# src/a2a/coordinator/run_control.py
class RunStatus(BaseModel):
    step: int = 0
    max_steps: int = 0
    finished: bool = False
    stopped: bool = False
    stop_reason: str = ""
    timeout_agents: list[int] = []
    domain_metrics: dict[str, Any] = {}   # SAR: coverage/transport_rate; AI2Thor: 自定义

class EnvironmentRunControl(Protocol):
    def request_stop(self, reason: str) -> None: ...
    def stop(self) -> None: ...                      # 立即唤醒所有 waiter
    def get_run_status(self) -> RunStatus: ...
```

- `request_stop` 是**协调器侧**语义：记录原因、设置 flag、唤醒；幂等。
- `stop` 是**环境侧**语义：直接终止回合、唤醒、关闭 executor。
- Server 只依赖 Protocol，不知道 SAR/AI2Thor 区别。

### 3.2 Barrier 线程模型

- 沿用 `sar_orch/barrier.py` 的 `threading.Event` + `threading.Lock`（已验证跨 event loop 安全）。
- **不把** `asyncio.to_thread` 的默认 executor 与 Controller 的单线程 executor 混用 —— 子代理审查指出的死锁风险。
- `wait_round()` 在 asyncio 侧 `await loop.run_in_executor(None, self._round_event.wait, timeout)`，`None` executor 是独立线程池，与 Controller executor 解耦。

### 3.3 Context 渲染：子类化而非改 schema

- `CoordinatorPinnedState` / `WorkerPinnedState` 的 SAR 字段**不动**。
- `AI2ThorCoordinatorContextManager` 覆盖 `_render_environment_view()`，输出：
  ```
  Scene: FloorPlan1 | Task: 3_transport_groceries | Step: 7/50
  Agents: Alice(at CounterTop_1, holding: Mug) | Bob(at Fridge, empty)
  Objects of interest: Mug, Plate, Lettuce (via AliasRegistry)
  ```
- worker 同理，只渲染自己视角。

### 3.4 AliasRegistry 与可见性

- raw `objectId`（如 `Mug|-01.5|+00.9|+02.3`）只在 coordinator / verifier 层出现。
- worker 工具返回串经 `AliasRegistry.redact(text)` 过滤，把 raw id 替换为 alias。
- G4.7 的 `test_visibility.py` 作为**泄漏审计**：对 worker snapshot 做 `assert not re.search(r"\|[+-]\d", rendered)`。

### 3.5 TaskWatchdog 退化策略

- AI2Thor 模式：`AI2ThorExperiment` 创建 `TaskWatchdog` 时注入 `run_control=ai2thor_barrier`，不注入 `barrier`。
- `_refresh_progress_by_domain_delta()` 检查 `run_status.domain_metrics` 是否含 `"coverage"`；没有则跳过 domain delta，只保留通用超时（`TASK_STALE` / `WORKER_UNREACHABLE` / `TASK_DEADLINE_*`）。

---

## 4. 文件改动清单（汇总）

### 新建

```
src/a2a/coordinator/run_control.py
scripts/ai2thor_runtime_smoke.py

ai2thor_orch/
  __init__.py
  contracts/{__init__.py, types.py, task.py}
  executor/{__init__.py, controller_executor.py}
  barrier/{__init__.py, ai2thor_barrier.py}
  budget/{__init__.py, ledger.py}
  state/{__init__.py, worker_state_provider.py, coordinator_state_provider.py, context.py}
  tools/worker/{__init__.py, move_ahead.py, rotate.py, look.py, pickup.py, put.py, open_close.py, done.py}
  visibility.py
  verifier/{__init__.py, verifier.py}
  experiment/{__init__.py, ai2thor_experiment.py}
  benchmark.py
  prompts/coordinator/system.md
  prompts/worker/system.md
  tests/{__init__.py, fakes.py, test_contracts.py, test_executor.py, test_barrier.py,
         test_budget.py, test_state_providers.py, test_visibility.py, test_tools.py,
         test_verifier.py, test_experiment_e2e.py}

AI2Thor/__init__.py
AI2Thor/Tasks/__init__.py
thortils/__init__.py

tests/test_run_control.py
```

### 修改

```
pyproject.toml                      # G0: 本地包 source 声明（AI2Thor/thortils editable）
uv.lock                             # G0: 随 pyproject 同步
sar_orch/barrier.py                 # G3.2: +get_run_status(), +request_stop(reason); 去 sys.path 注入
src/a2a/coordinator/server.py       # G3.3/G3.4: +set_run_control(); _do_cancel_experiment 改走 run_control; SSE 守卫
src/a2a/coordinator/task_watchdog.py # G3.5: 条件化 domain delta
AI2Thor/env_new.py                  # G0.3: 去 sys.path 注入
AGENTS.md                           # G5 后更新命令段（fake/unity 运行方式）
```

### 不改（明确排除）

- `src/Agent/router_agent/context.py` / `src/Agent/worker_agent/context.py` 的 SAR pinned schema —— 用子类化扩展。
- `src/a2a/worker/agent_adapter.py` 的构造函数签名 —— 用 `extra_tools` 注入绑定好 barrier 的工具实例。
- `sar_orch/coordinator_state_provider.py` / `worker_state_provider.py` —— AI2Thor 写自己的，不复用。

---

## 5. 依赖顺序图

```
G0 (deps/包治理)
 ├─→ G1 (unity 探针, 远程)        ← 可并行
 └─→ G2 (契约/executor/barrier)   ← fake, 本地可先行
      └─→ G3 (run_control 统一)
           └─→ G4 (tools/state/context)
                └─→ G5 (verifier/benchmark/E2E)
```

- G1 与 G2 可并行：G2 用 fake Controller，不依赖真实 Unity。
- G3 依赖 G2 的 `AI2ThorBarrier` 存在（要实现 `EnvironmentRunControl`）。
- G5 的 unity E2E 依赖 G1 通过。

---

## 6. 测试矩阵

### 6.1 新增测试

| 文件 | 阶段 | 模式 | 关键断言 |
|------|------|------|---------|
| `ai2thor_orch/tests/test_contracts.py` | G2 | fake | dataclass 序列化/默认值/不可变 |
| `ai2thor_orch/tests/test_executor.py` | G2 | fake | 串行性、stop 幂等、异常传播 |
| `ai2thor_orch/tests/test_barrier.py` | G2 | fake | 正常回合、超时 NoOp、request_stop 唤醒、run_status 内容 |
| `ai2thor_orch/tests/test_budget.py` | G2 | fake | 预算累计、耗尽边界 |
| `tests/test_run_control.py` | G3 | — | 协议契约、SAR 适配器、server cancel 路径 |
| `ai2thor_orch/tests/test_state_providers.py` | G4 | fake | snapshot payload 键、版本递增 |
| `ai2thor_orch/tests/test_visibility.py` | G4 | fake | alias 隔离、raw id 不泄漏 |
| `ai2thor_orch/tests/test_tools.py` | G4 | fake | 工具调用 → barrier.submit_action 入参正确 |
| `ai2thor_orch/tests/test_verifier.py` | G5 | fake | postconditions 判定 |
| `ai2thor_orch/tests/test_experiment_e2e.py` | G5 | fake | 2 agent 跑通 mock 任务 |
| `scripts/ai2thor_runtime_smoke.py --report` | G1 | unity | 报告 JSON 字段齐全 |

### 6.2 运行命令

```bash
# 本地默认（fake，跳过 unity）
cd /home/wyh/daily_work/LLaMAR-ai2thor
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests tests/test_run_control.py \
  -m "not unity" -v

# 仅 AI2Thor 包
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests -m "not unity" -v

# Unity smoke（远程 A100）
LLAMAR_AI2THOR_MODE=unity uv run python scripts/ai2thor_runtime_smoke.py --report reports/unity.json

# Unity 单测（远程，显式）
PYTHONPATH="src:$PYTHONPATH" LLAMAR_AI2THOR_MODE=unity \
  uv run pytest ai2thor_orch/tests -m unity -v
```

### 6.3 SAR 回归红线（每次改 `src/a2a/` 或 `src/Agent/` 后必跑）

```bash
PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_sar_barrier_observability.py \
  tests/test_task_watchdog.py \
  tests/test_server_lifecycle.py \
  tests/test_coordinator_state_provider.py \
  tests/test_worker_state_provider.py \
  tests/test_coordinator_push_callback.py \
  tests/test_coordinator_semantic_mode.py \
  tests/test_a2a_e2e.py \
  -v
```

**约束**：`testpaths = ["sar_orch", "ai2thor_orch", "integration"]`，`tests/` 目录不在默认收集路径，需显式指定文件。

---

## 7. 风险登记册

| # | 风险 | 影响 | 缓解 | 阶段 |
|---|------|------|------|------|
| R1 | 远程 A100 缺 Unity build | G1 硬阻断，G5 unity E2E 无法进行 | G1 探针脚本产出详细错误报告；G2 用 fake 先行不阻塞；准备第二备选宿主 | G1 |
| R2 | 单线程 executor 与 `asyncio.to_thread` 默认 executor 混用导致 wait_round 死锁 | barrier 挂死，实验永不推进 | `wait_round` 显式用 `run_in_executor(None, ...)`；G2 单测加超时用例 | G2 |
| R3 | `TaskWatchdog` 在 AI2Thor 下 domain delta 失效，退化为仅超时检测 | 监督粒度变粗，但不影响正确性 | G3.5 条件化；文档明确说明 AI2Thor 无 coverage/transport_rate | G3 |
| R4 | `CoordinatorContextManager` / `WorkerContextManager` 的 SAR renderer 被 AI2Thor 误用 | Context Memory 渲染 fires/persons 等无关字段，误导 LLM | G4.5 子类化覆盖 `_render_environment_view()`；G4.7 渲染快照测试 | G4 |
| R5 | raw objectId 泄漏到 worker 视角 | worker 可直接定位物体，破坏语义地图实验假设 | G4.6 AliasRegistry + redact；G4.7 泄漏审计测试 | G4 |
| R6 | verifier 依赖 G1 的真实 metadata schema，G1 受阻时 verifier 只能按推测 schema 编写 | verifier 误判 | G5.2 首版只支持 `3_transport_groceries`（checker 已存在）；schema 以 fake metadata 为初版契约，unity 后校准 | G5 |
| R7 | `pyproject.toml` wheel packages 已含 `ai2thor_orch` 但目录不存在，打包警告 | `uv build` 可能失败 | G0.4 先建空骨架 | G0 |
| R8 | `/map/state`、`/dashboard/stream` 在 AI2Thor 模式下空转 | SSE 端点永不返回数据，前端挂起 | G3.4 守卫：`self._barrier is None` 时 404 | G3 |
| R9 | 本地 fake 与远程 unity 行为分叉，某 bug 只在 unity 出现 | 本地无法复现 | G1 报告含版本/scene/metadata hash；G5 unity E2E 日志完整落盘；文档明确本地/远程责任分界 | G1/G5 |

---

## 8. 回滚策略

- **G0 问题**：`git revert` 包治理 commit；`AI2Thor/` 恢复 `sys.path` 注入。
- **G2 问题**：`ai2thor_orch/` 整体删除即可，不动共享层，SAR 无感。
- **G3 问题**：`src/a2a/coordinator/server.py` 与 `task_watchdog.py` 的改动单独 revert；`set_barrier()` 保留意味着旧 SAR 注入路径仍可用 —— 这是有意的**双轨设计**，降低回滚成本。
- **G4/G5 问题**：仅删 `ai2thor_orch/`，不影响 `src/`。

双轨原则：`set_run_control()` 与 `set_barrier()` 并存，cancel 优先走 `run_control`，`run_control is None` 时回落到旧 `barrier.stop()` 路径。这样 G3 合并后即使 AI2Thor 侧有 bug，SAR 实验仍按原逻辑运行。

---

## 9. 实施顺序建议（按 commit 粒度）

1. `chore(deps): add ai2thor optional extra` —— 当前未提交的 pyproject + uv.lock
2. `feat(ai2thor): package governance for AI2Thor/thortils` —— G0.2 + G0.3 + G0.4
3. `feat(ai2thor): unity runtime smoke probe` —— G1
4. `feat(ai2thor): contracts + controller executor` —— G2.1 + G2.2 + G2.5 + 部分测试
5. `feat(ai2thor): AI2ThorBarrier with round semantics` —— G2.3 + G2.4 + 剩余测试
6. `feat(a2a): EnvironmentRunControl protocol + SAR adapter` —— G3.1 + G3.2
7. `refactor(a2a): server cancel via run_control + SSE guards` —— G3.3 + G3.4 + G3.5 + SAR 回归
8. `feat(ai2thor): worker tools + state providers + context subclasses` —— G4
9. `feat(ai2thor): verifier + experiment runner + benchmark` —— G5.1-G5.4
10. `test(ai2thor): fake E2E + unity smoke on A100` —— G5.5-G5.6
11. `docs: update AGENTS.md AI2Thor runbook` —— 收尾

---

## 10. 与 AGENTS.md 的一致性

- 本计划遵循 AGENTS.md 的硬件约束（fake 本地 / unity 远程 A100）、`uv` 依赖纪律、`no_proxy` 要求。
- G5 完成后需把 AGENTS.md 的「AI2Thor Development (WIP)」段从「命令会逐步稳定」更新为正式命令（fake 单测、unity smoke、experiment 运行方式）。
- 现有 AGENTS.md 中的 `uv sync --extra ai2thor`、`pytest ai2thor_orch/tests -m "not unity"`、`scripts/ai2thor_runtime_smoke.py --report` 三条命令与本计划 §6.2 完全一致 —— 无需改动，只需在 G5 后去掉「WIP」标注。

---

## 进度跟踪（实施时更新）

| 阶段 | 状态 | Commit | 备注 |
|------|------|--------|------|
| G0.1 deps extra+lock | ✅ done | `e4bcd60` | 2026-07-18 提交 |
| G0.2-G0.5 包治理/骨架/验证 | ✅ done | `e9f17d7` | torch 拆分到 `ai2thor-unity` extra；venv 恢复；29 tests 收集 0 errors；2 个 integration 测试失败为预存问题（断言过时的 `_handle_worker_ws` 属性），与 G0 无关 |
| G1 探针脚本 | ✅ done（本地） | `2adffb1` | fake 模式本地验证通过；unity 实跑待远程 A100 |
| G2 契约/Executor/Barrier | ✅ done | `5f87092` | 75/75 fake 单测通过；**技术债**：barrier 的 `_execute_round` 跑在 asyncio 默认池，`ControllerExecutor` 的专用池未接线（安全但非串行隔离），G3 一并处理 |
| G3 RunControl 统一 | ✅ done | `7b495a5` | 15+75+68 全绿；双轨 cancel、`_current_step()` helper、SSE 404 守卫、watchdog 条件化；G2 executor 技术债已修（`_execute_round` 真正跑在专用单线程池） |
| G4 工具/State/Context | ✅ done | `590fd57` | 129/129 通过；worker 工具经 AliasRegistry 双向转换 + redact 防泄漏；Context 子类只覆盖渲染不改 SAR schema |
| G5 Verifier/Benchmark/E2E | ✅ done（本地 fake） | `c8697b2` | 145/145 通过 + E2E 3/3 + 真实 benchmark CLI 跑通；unity E2E 待远程 A100 |

---

*计划版本：v1.0 · 2026-07-18 · 基于设计文档 v1 与当日仓库实况核查*

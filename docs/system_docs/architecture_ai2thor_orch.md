---
日期: 2026-07-18
文档类型: 系统架构文档
文档概述: ai2thor_orch 编排层架构——AI2Thor 环境向 A2A 迁移后的独立编排包，涵盖数据契约、回合屏障、执行器、可见性、Worker 工具、状态投影、Context 渲染、任务契约、验证器与实验运行器
---

# ai2thor_orch 编排层架构

## 1. 概述

`ai2thor_orch/` 是 AI2Thor 环境的 A2A 编排层，与 `sar_orch/` 平级、结构对齐。它将 AI2Thor Unity 仿真接入 Coordinator-Worker 架构，复用 `src/a2a/` 传输层与 `src/Agent/` ReAct 内核，同时保持 SAR 行为零回归。

设计文档：`docs/plans/2026-07-18-ai2thor-a2a-migration-design.md`
实施计划（含进度跟踪）：`docs/plans/2026-07-18-ai2thor-a2a-migration-implementation-plan.md`

**当前状态**：G0–G5 全部实现完成并 commit。fake 模式（mock Controller）端到端可本地运行；unity 模式（真实 Unity）留接口，待远程 A100 验证。

---

## 2. 目录结构

```
ai2thor_orch/                       # AI2Thor A2A 编排层（~4950 行）
├── contracts/                      # 数据契约（纯 dataclass，零依赖）
│   ├── types.py                    #   7 个核心 DTO（97 行）
│   └── task.py                     #   TaskContract + load_task()（165 行）
├── executor/                       # Controller 串行执行器
│   └── controller_executor.py      #   ControllerExecutor（103 行）
├── barrier/                        # 回合同步屏障
│   └── ai2thor_barrier.py          #   AI2ThorBarrier（480 行）
├── budget/                         # Token 预算
│   └── ledger.py                   #   BudgetLedger（72 行）
├── visibility.py                   # 可见性：AliasRegistry（93 行）
├── tools/                          # Worker 受限工具集
│   └── worker/                     #   move/rotate/look/pickup/put/open_close/done
├── state/                          # 运行时状态投影 + Context 渲染
│   ├── worker_state_provider.py    #   AI2ThorWorkerStateProvider（63 行）
│   ├── coordinator_state_provider.py # AI2ThorCoordinatorStateProvider（89 行）
│   └── context.py                  #   两个 ContextManager 子类（137 行）
├── verifier/                       # 任务完成验证
│   └── verifier.py                 #   verify_postconditions / verify_round（142 行）
├── experiment/                     # 实验运行器
│   ├── ai2thor_experiment.py       #   AI2ThorExperiment（364 行）
│   └── __main__.py                 #   python -m 入口（49 行）
├── benchmark.py                    # 基准测试 CLI（345 行）
├── prompts/                        # 系统提示
│   ├── coordinator/system.md
│   └── worker/system.md
└── tests/                          # 单测（fake 模式，145 个测试）
    ├── fakes.py                    #   FakeController + FakeEvent 工厂
    ├── test_contracts.py           #   14 个
    ├── test_executor.py            #   8 个
    ├── test_barrier.py             #   19 个
    ├── test_budget.py              #   10 个
    ├── test_visibility.py          #   12 个
    ├── test_tools.py               #   21 个
    ├── test_state_providers.py     #   12 个
    ├── test_context.py             #   8 个
    ├── test_verifier.py            #   13 个
    └── test_experiment_e2e.py      #   3 个（fake E2E）
```

---

## 3. 分层架构

ai2thor_orch 采用与 sar_orch 相同的分层，每层只依赖下层：

```
┌─────────────────────────────────────────────────────┐
│  Experiment / Benchmark (实验运行器 + CLI)           │  ← G5
├─────────────────────────────────────────────────────┤
│  Verifier / TaskContract (任务契约 + 完成验证)        │  ← G5
├─────────────────────────────────────────────────────┤
│  StateProvider / Context (状态投影 + 渲染)            │  ← G4
├─────────────────────────────────────────────────────┤
│  Worker Tools (受限工具集)                            │  ← G4
├─────────────────────────────────────────────────────┤
│  Barrier / Executor / Budget (回合核心)               │  ← G2/G3
├─────────────────────────────────────────────────────┤
│  Contracts / Visibility (数据契约 + 可见性)           │  ← G2
└─────────────────────────────────────────────────────┘
```

---

## 4. 核心组件

### 4.1 数据契约 `contracts/types.py`

7 个纯 dataclass，零外部依赖，是全包的数据交换格式：

| DTO | 用途 |
|-----|------|
| `ActionRequest` | agent 提交的动作（agent_idx, action, round_no） |
| `ActionResult` | 单 agent 动作结果（observation, success, position, inventory, raw） |
| `RoundResult` | 一回合全部结果（results, timeout_agents, finished, domain_metrics） |
| `PublicObservation` | worker 可见观测（visible_objects 用 alias，不含 raw objectId） |
| `CoordinatorObservation` | coordinator 全局观测（agents, objects, scene, step） |
| `AgentPublicState` | 单 agent 公开状态 |
| `RunStatus` | 运行状态（step/max_steps/finished/stopped/stop_reason/timeout_agents/domain_metrics），**同时被 `EnvironmentRunControl` 协议复用** |

`RunStatus` 的字段名与实施计划 §3.1 严格一致，由 `test_contracts.py::test_field_names_match_plan_section_3_1` 显式审计。

### 4.2 ControllerExecutor `executor/controller_executor.py`

持有专用 `ThreadPoolExecutor(max_workers=1)`，将所有 AI2Thor Controller 调用串行化到单一后台线程：

- `execute_step(actions)`：逐个调 `controller.step()`，收集每个的 `event.metadata`，按输入顺序返回结构化结果
- `reset(scene)`：场景重置 + 初始 metadata
- `stop()`：幂等，`controller.stop()` + `executor.shutdown(wait=False)`

**线程模型**（G3 修正后）：barrier 的 `_execute_round` 经 `loop.run_in_executor(self._executor._executor, ...)` 真正跑在这个专用单线程池上，与 asyncio 默认池（event wait 用）完全隔离，避免 R2 死锁。

### 4.3 AI2ThorBarrier `barrier/ai2thor_barrier.py`

回合同步屏障，复用 `sar_orch/barrier.py` 的 `threading.Event` per agent + `threading.Lock` 模型（非 asyncio primitives，因为 worker 在独立线程的独立事件循环里）：

| 方法 | 说明 |
|------|------|
| `submit_action(agent_idx, action)` | 异步提交动作，等齐或超时后推进回合，返回 `ActionResult` |
| `request_stop(reason)` | 记录停止原因、设标志、唤醒所有等待者 |
| `stop()` | `request_stop('env_stop')` + `executor.stop()` |
| `get_run_status()` | 返回 `RunStatus` DTO |
| `snapshot_public(agent_idx)` | worker 视角观测（visible_objects 经 alias 转换） |
| `snapshot_coordinator()` | coordinator 全局观测 |
| `is_finished()` | 回合耗尽或已停止 |

公开 API 与 `SARBarrier` 完全对齐，使 G3 的统一协议可直接适配。超时自动为未提交 agent 填充 NoOp 并记录 `timeout_agents`（与 SAR 行为一致）。

### 4.4 AliasRegistry `visibility.py`

AI2Thor 的 raw objectId 含绝对世界坐标（如 `Mug|-01.5|+00.9|+02.3`），绝不能暴露给 worker agent（会泄漏场景布局先验）。`AliasRegistry` 做双向映射：

- `register(raw_id)` → 生成稳定 alias（`Mug_1`、`CounterTop_2`）
- `alias(raw_id)` → 查 alias；`raw(alias)` → 反查 raw_id（pickup/put 用）
- `redact(text)` → 把文本里所有 raw objectId 替换成 alias（未注册的自动注册）
- `is_raw_id_leaked(text)` → 审计 helper，检测 `\|[+-]\d` 模式，供测试断言

正则：`[A-Za-z]\w*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*`

### 4.5 BudgetLedger `budget/ledger.py`

Token 预算追踪：`record_round(round_no, prompt, completion)`、`remaining()`、`exhausted()`、`summary()`。供实验运行器在每回合后记账。

---

## 5. Worker 工具集 `tools/worker/`

每个工具继承 `src/Agent/worker_agent/tools/base.py` 的 `Tool`，构造函数接收 `(barrier, agent_idx, alias_registry)`：

| 工具 | action_string | 说明 |
|------|---------------|------|
| `move(direction)` | `MoveAhead` 等 | direction ∈ {ahead, back, left, right} |
| `rotate(direction)` | `RotateLeft/Right` | |
| `look(direction)` | `LookUp/LookDown` | |
| `pickup(object_alias)` | `PickupObject(<raw_id>)` | alias → raw 转换 |
| `put(receptacle_alias)` | `PutObject(<raw_id>)` | alias → raw 转换 |
| `open(object_alias)` / `close(object_alias)` | `OpenObject/CloseObject` | |
| `done()` | — | 返回 `task_complete=True` |

**统一行为**：`execute()` 调 `await barrier.submit_action(agent_idx, action_string)`，返回的 observation 文本必经 `alias_registry.redact()` 过滤。`parameters` 用 JSON Schema enum 限制合法取值。Worker **不**获得 bash/file 工具（实验组装时 `include_base_tools=False`）。

---

## 6. 状态投影与 Context 渲染 `state/`

### 6.1 StateProvider

实现 `src/Agent/router_agent/state_provider.py` 的 `StateProvider` Protocol：

- `AI2ThorWorkerStateProvider.snapshot()`：读 `barrier.snapshot_public(agent_idx)`，payload 键 `position/rotation/inventory/step/visible_objects/current_task/mission_status`
- `AI2ThorCoordinatorStateProvider.snapshot()`：读 `barrier.snapshot_coordinator()`，payload 键 `step_budget/team_status_summary/task_status_view/mission_finished/run_status`

**不渲染** fires/persons/reservoirs 等 SAR 字段。

### 6.2 Context 子类 `state/context.py`

- `AI2ThorWorkerContextManager(WorkerContextManager)`
- `AI2ThorCoordinatorContextManager(CoordinatorContextManager)`

两者**只覆盖 `_render_environment_view()`**，从 `RuntimeState.payload` 读数据渲染 AI2Thor 相关状态，pinned state schema（`WorkerPinnedState`/`CoordinatorPinnedState`）完全继承 SAR 未修改——SAR 渲染路径零影响。

---

## 7. 任务契约与验证 `contracts/task.py` + `verifier/verifier.py`

### 7.1 TaskContract

`load_task(task_id, scene)` 用 `importlib` 从 `AI2Thor/Tasks/<task_id>/` 动态加载 checker.py 和 FloorPlan 配置，解析出 `subtasks`、`coverage_objects`、`initial_inventory`。首版仅支持 `3_transport_groceries`，其他 task_id 抛 `NotImplementedError`。

### 7.2 Verifier

- `verify_postconditions(final_metadata, contract)`：检查所有 `coverage_objects` 是否都在 Fridge 内（经 `parentReceptacles`）
- `verify_round(round_result, contract)`：返回 `{verified_completion, coverage, details}`

这是**环境层**的确定性验证，不依赖 coordinator LLM 自报成功。

---

## 8. 实验运行器 `experiment/ai2thor_experiment.py`

组装整条栈并驱动回合循环：

```
FakeController (fake) / Controller (unity)
  → ControllerExecutor
    → AI2ThorBarrier
      → AliasRegistry
        → per-agent worker 工具集
          → worker/coordinator StateProvider
```

- fake 模式：agent 用确定性 round-robin 策略（`[MoveAhead, RotateLeft, RotateRight, LookUp, LookDown]`），**不接入 LLM**，用于本地验证回合语义
- unity 模式：`_create_controller` 抛 `NotImplementedError`（留接口，待远程 A100）
- 日志：`logs/<timestamp>_<task>_<scene>_a<N>_seed<S>_<mode>/` 下写 `summary.csv`、`events.ndjson`、`run_meta.json`

`AI2ThorExperiment` 是 `EnvironmentRunControl` 的组装点——它把 `AI2ThorBarrier` 注入 coordinator server 的 `set_run_control()`（G3 协议）。

---

## 9. 基准测试 `benchmark.py`

CLI：`uv run python -m ai2thor_orch.benchmark --task 3_transport_groceries --scene 1 --agents 2 --seed 42 --mode fake`

- `--list-tasks` 扫描 `AI2Thor/Tasks/*/` 发现任务（当前 29 个目录）
- `--agents`/`--seed` 支持 sweep（空格分隔多值）
- `--run-timeout` 单 run 墙钟超时
- 聚合表格 + `benchmark_results/index.json`

---

## 10. 与 SAR 的关系

| 维度 | SAR (`sar_orch/`) | AI2Thor (`ai2thor_orch/`) |
|------|-------------------|---------------------------|
| 屏障 | `SARBarrier` | `AI2ThorBarrier`（API 对齐） |
| 停止协议 | `get_run_status()`/`request_stop()`（G3 适配） | 原生实现 `EnvironmentRunControl` |
| Worker 工具 | 14 个 SAR 领域工具 | 7 个 AI2Thor 导航/操作工具 |
| 可见性 | 无（SAR objectId 无坐标泄漏问题） | `AliasRegistry` 强制 alias |
| Context | `WorkerContextManager`/`CoordinatorContextManager` | 各自子类覆盖 `_render_environment_view()` |
| 验证 | SAR checker（coverage/transport_rate） | `Verifier`（postcondition 检查） |
| 共享层 | `src/a2a/`、`src/Agent/` | **完全复用，零修改** |

**SAR 零回归保证**：G3 采用双轨 `set_run_control()`/`set_barrier()` 并存，cancel 优先走 run_control、None 回落旧 `barrier.stop()`；Context 扩展用子类化而非改 pinned schema。68 个 SAR 回归红线测试全绿。

---

## 11. 测试与验证

```bash
# AI2Thor 包（fake 模式，145 个测试）
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests -m "not unity" -q

# RunControl 协议（15 个）
PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_run_control.py -q

# SAR 回归红线（68 个，必须全绿）
PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_sar_barrier_observability.py tests/test_task_watchdog.py \
  tests/test_server_lifecycle.py tests/test_coordinator_state_provider.py \
  tests/test_worker_state_provider.py tests/test_coordinator_push_callback.py \
  tests/test_coordinator_semantic_mode.py -q

# fake E2E
PYTHONPATH="src:$PYTHONPATH" uv run pytest ai2thor_orch/tests/test_experiment_e2e.py -v
```

**当前验证状态**：160 (ai2thor_orch + run_control) + 68 (SAR 红线） 全绿；真实 benchmark CLI 端到端跑通（5 rounds / 0.1s）。

**唯一剩余项**：unity 模式远程 A100 端到端验证（需 `uv sync --extra ai2thor-unity` 安装 CUDA torch）。

---

## 12. Commit 历史

| Commit | 内容 |
|--------|------|
| `e4bcd60` | G0.1 deps：ai2thor optional extra + lock |
| `e9f17d7` | G0：包治理（AI2Thor/ 正规包、sys.path 注入改 lazy）、torch 拆到 ai2thor-unity |
| `2adffb1` | G1：Unity runtime 探针脚本 `scripts/ai2thor_runtime_smoke.py` |
| `5f87092` | G2：契约、ControllerExecutor、AI2ThorBarrier、BudgetLedger、AliasRegistry |
| `7b495a5` | G3：`EnvironmentRunControl` 统一停止协议（唯一改共享层的阶段） |
| `590fd57` | G4：worker 工具、StateProvider、Context 子类 |
| `c8697b2` | G5：任务契约、Verifier、Experiment runner、Benchmark、fake E2E |
| `efd6e24` | docs：AGENTS.md + 设计/计划文档 |

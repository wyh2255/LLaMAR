# AI2Thor × A2A 多智能体实验迁移设计（Rev 2）

- 状态：设计已审查，尚未开始实现
- 目标仓库：`/home/wyh/daily_work/LLaMAR-ai2thor`
- 历史来源：2026-07-16「AI2Thor 三阶段执行计划」会话中的 Rev 2 设计；原 `.hermes/plans/` 临时文件在仓库更名后已不存在，本文件为迁移后的正式版本。
- 实施原则：先通过依赖与 Unity runtime 门禁，再编写环境适配层；不以 Python import 成功替代真实 Controller 验证。

## 1. 目标与边界

### 1.1 目标

在不回归既有 SAR 实验链路的前提下，将环境无关的 A2A / Agent 编排框架接入 AI2Thor，形成可重放、可观测、无 oracle 泄漏的多智能体实验平台。

首个可交付版本必须支持：

1. 一个已验证任务和场景初始化；
2. 两个 AI2Thor agent 的逻辑回合协作；
3. 受限 worker 工具调用，而不是 worker 直接操纵 `ai2thor.controller.Controller`；
4. 固定顺序、单线程执行的 Controller 操作；
5. 局部可观测状态、公共别名和可审计 action/result 日志；
6. 以终局 metadata 后置条件验证的成功判定；
7. logical round、physical action、controller query 三类独立预算。

### 1.2 非目标

本阶段不做以下工作：

- 不重写 `AI2Thor/`、`thortils/` 中已有的场景、导航、交互和任务资产；
- 不把 SAR 专用 `sar_orch`、SAR map/dashboard HTTP 路径改造成 AI2Thor 通用实现；
- 不让 LLM 直接持有原始 Controller 或未过滤的终局 checker truth；
- 不宣称 AI2Thor 多 agent 在物理层面并行；共享 Controller 的调用必须串行；
- 不把旧任务 checker 的进度指标当作 benchmark 成功真值。
- 不假设当前开发机器拥有运行 Unity 的 GPU；所有必须依赖 Unity 的门禁都支持在远程 headless A100 服务器上执行，也支持在本地使用 fake Controller 进行开发。

### 1.3 设计决策

采用隔离式适配，而不是修改 A2A 核心或复制整套历史 AI2Thor wrapper：

```text
src/a2a + src/Agent                 环境无关的编排、任务和 Agent 核心
          │
          ├── sar_orch/             既有 SAR 专属实现，保持独立
          │
          └── ai2thor_orch/         新增 AI2Thor 环境适配层
                 ├── controller_executor.py
                 ├── barrier.py
                 ├── state_provider.py
                 ├── tools/worker/
                 ├── task_catalog.py
                 ├── verifier.py
                 └── experiment.py
                         │
                         ├── AI2Thor/    已有任务、场景和 legacy wrapper
                         └── thortils/   已有导航、地图和交互工具
```

当前 `src/a2a/coordinator/server.py` 的 `set_barrier()` 明确注释为 SAR map 可视化用途。因此 AI2Thor 不应复用它来承载控制生命周期。实施时应新增窄的、环境无关的 `EnvironmentRunControl` 注入点，避免 AI2Thor 进入 SAR 的 map/dashboard 分支。

## 1.4 硬件与部署约束

设计必须满足以下运行环境约束：

1. **当前开发机器无 Unity GPU 支持**：本地开发主要进行代码设计、fake Controller 单元测试、依赖闭包与 A2A 生命周期验证；不强制要求本机启动真实 AI2Thor Unity 进程。
2. **真实仿真运行在远程 headless A100 服务器**：所有需要 Unity 的门禁（G1 runtime probe、G5 端到端 benchmark）必须能通过 SSH/CLI 在远程 headless Linux 服务器上复现，且支持无显示环境（`X_DISPLAY` 缺失或 `xvfb`）。
3. **依赖必须通过 `pyproject.toml` + `uv` 跨机器复现**：任何 `pip install`、本地 venv 手动安装、sys.path 脚本注入都不允许作为交付前提。`AI2Thor/` 和 `thortils/` 要么作为本地包被 `pyproject.toml` 发现，要么在 G0 中完成 import 路径治理。
4. **运行模式分层**：
   - `fake` 模式：不依赖 ai2thor 包，使用 mock Controller 测试 barrier/executor/DTO/state machine；可在任何机器上 CI 运行。
   - `unity` 模式：依赖完整 ai2thor extra，需要 GPU/X server，仅在 A100 服务器或具备显示能力的机器上运行。
   - 默认运行必须可切换为 `fake` 模式，保证本地与 CI 不因为没有 Unity 而失败。

## 2. 已核验现状与待验证项

### 2.1 当前可复用资产

- `src/a2a/` 与 `src/Agent/` 提供 Coordinator、Worker、A2A server、任务队列、状态提供器接入等环境无关能力。
- `AI2Thor/` 保留任务、场景初始化、legacy wrapper 与 baseline 资产；实现阶段应复用任务定义，而不是重新造场景或 checker。
- `thortils/` 是已有导航、地图和交互能力的候选复用层。
- `sar_orch/` 可作为 barrier、experiment lifecycle、logger 和状态投影的参考，不是 AI2Thor 的运行时依赖。

### 2.2 当前明确的缺口

截至写入本设计时，`pyproject.toml` 仅定义 `sar`、`coordinator`、`llm`、`memory`、`dev` extras，未声明 AI2Thor runtime extra。故从干净环境重建时，不能假设 `ai2thor`、`scipy`、`pandas`、任务 checker 或 `thortils` 的完整导入链可用。

历史审查还发现：

- `thortils` 可能经数学/导航模块引入 `scipy`；
- legacy task checker 的导入路径可能经日志模块引入 `pandas`；
- AI2Thor 可 import 不等于 Unity build 可下载、可启动或能在 WSL 下运行；
- 任务目录、checker 和 `FloorPlan*.py` 的静态数量只可作为 catalog 候选，不能证明其 initializer 签名、依赖或终局后置条件可运行。

因此所有与版本、任务 catalog、Unity runtime 有关的陈述在 Phase 0 和 Phase 1 之前均视作“待运行验证”，而不是实现前提。

## 3. 架构契约

### 3.1 `EnvironmentRunControl`

A2A 生命周期只依赖一个窄接口，而不依赖 SAR barrier：

```python
class EnvironmentRunControl(Protocol):
    async def request_stop(self, reason: str) -> None: ...
    def is_finished(self) -> bool: ...
    def get_run_status(self) -> Mapping[str, object]: ...
```

Coordinator server 在构造完成后注册该对象；A2A cancel、watchdog、server shutdown 都通过它请求停止。AI2Thor 的实现将委托给 barrier，但不暴露 `Controller`。

约束：

- `request_stop()` 必须幂等；
- stop 会先唤醒等待 barrier 的 worker，再结束 A2A active tasks 与 server；
- 任何控制路径都不能在 event loop 中直接执行阻塞 Unity 调用；
- AI2Thor 不调用现有的 SAR `set_barrier()`、SAR map ingest 或 SAR dashboard route。
- **与现有 SAR `barrier.stop()` 的共存**：A2A core 的 cancel 端点当前无条件调用 `self._barrier.stop()`。注入 `EnvironmentRunControl` 后，规则改为：
  - 若 `run_control` 已注册，则 cancel / shutdown 统一调用 `run_control.request_stop()`，不再直接调用 `barrier.stop()`；
  - 若 `run_control` 未注册（SAR 旧路径），保留原有 `barrier.stop()` 回退；
  - 该回退逻辑必须在 `CoordinatorServer` 中通过单一入口实现，防止 AI2Thor 路径下出现 `run_control` 与 `barrier` 双重停止。

### 3.2 `AI2ThorBarrier`

`AI2ThorBarrier` 是 worker tool、状态投影、实验 runner 和控制生命周期之间的唯一环境状态机。建议对外契约：

```python
class AI2ThorBarrier(EnvironmentRunControl, Protocol):
    async def open_round(self, round_id: int) -> RoundHandle: ...
    async def submit_action(
        self, round_id: int, agent_id: int, action: PublicAction
    ) -> AgentActionResult: ...
    async def wait_round(self, round_id: int) -> RoundResult: ...
    def snapshot_public(self, viewer: Viewer) -> PublicEnvironmentState: ...
    def get_metrics(self) -> RunMetrics: ...
    def get_last_step_log(self) -> PublicStepLog | None: ...
    async def request_stop(self, reason: str) -> None: ...
    @property
    def step_counter(self) -> int: ...
    @property
    def revision(self) -> int: ...
```

`PublicAction` 必须是验证后的 DTO，而不是任意 `dict`。动作名称、参数、目标别名、agent identity 和调用来源需在进入 executor 前校验。

**线程模型约束**：worker 在独立线程中运行并拥有独立的 asyncio 事件循环（参见现有 SAR 实现与 ADR-011），因此 `AI2ThorBarrier` 内部的跨线程同步原语**必须是 `threading.Event` / `threading.Lock`，不能直接使用 `asyncio.Event`**。虽然接口对外暴露为 `async def`，但内部等待/唤醒机制应在线程层实现，再在协程层封装。`request_stop()` 必须幂等，且通过线程安全的事件唤醒所有阻塞在 `wait_round` / `submit_action` 的调用者。

### 3.3 显式回合状态机

AI2Thor 多 agent 采用“逻辑回合屏障 + 单 Controller 串行执行”，不是物理同时动作。

**线程模型**：状态机使用 `threading` 原语实现，因为 worker 运行在独立线程中。对外 `async def` 接口内部通过 `asyncio` 事件循环与线程事件桥接，确保跨线程等待者能被正确唤醒（避免 `asyncio.Event` 跨线程失效问题，参考 SAR ADR-011）。

```text
IDLE
  └─ open_round() ──> OPEN
OPEN
  ├─ 合法 submit ──> OPEN（累计每 agent 一份 action）
  ├─ 收齐 actions ──> EXECUTING
  ├─ deadline 到达 ──> EXECUTING（缺席者得到 Pass / timeout 记录）
  ├─ cancel / fatal error ──> ABORTED
  └─ request_stop() ──> STOPPING
EXECUTING ──> CLOSED（产生同一 round 的稳定结果快照）
ABORTED / STOPPING ──> STOPPED
CLOSED ──> IDLE（仅 runner 可开启下一 round）
```

固定语义：

- 只有 runner / coordinator 可以调用 `open_round()`；零 submit 必须在 deadline 后关闭，不能永久卡住。
- 每个 agent 每轮最多提交一次；重复提交返回确定性 `DUPLICATE_SUBMISSION`，不覆盖首个动作。
- action 的物理执行顺序固定为稳定的 public agent order；该顺序和每个结果写入日志。
- 缺席 agent 得到显式 `TIMEOUT_PASS`；它会计入 logical round 和 physical action 的规则由预算契约定义。
- `cancel`、工具异常、Controller 致命错误和 stop 不能半提交为成功结果；必须记录 terminal reason 和未执行 action。
- 已结束或停止的 run 拒绝新 round 与新 action，返回 `RUN_FINISHED` 或 `RUN_STOPPED`。

### 3.4 单线程 `ControllerExecutor`

每个实验实例建立一个 `ThreadPoolExecutor(max_workers=1)` 或语义等价的专用串行 executor。下列调用一律只能从该 executor 发出：

- Controller 初始化、reset、step 和 query；
- reachable-position、visibility / segmentation 等查询；
- task scene initializer；
- final metadata 和后置条件读取；
- `Controller.stop()`。

禁止：

- tool 或 worker 直接持有并调用 Controller；
- 为每次动作创建独立的 `asyncio.to_thread()`；
- 多个实验共享同一个 Controller；
- 取消 coroutine 后假设底层 Unity 调用已经停止。

**本地开发与远程 headless 运行**：

- 本地 `fake` 模式：executor 使用 mock Controller，不调用 Unity，只验证调用顺序与返回结果。
- 远程 `unity` 模式：executor 在 headless A100 上通过 `ai2thor` 启动或连接 Unity；需支持 `xvfb` 或 `DISPLAY` 缺失环境，并记录 build/OS/GPU 配置。
- 任何真实 Unity 调用必须可在环境变量 `LLAMAR_AI2THOR_MODE=unity` 下显式开启；默认运行应为 `fake` 模式，避免本地无 GPU 时崩溃。

executor 的每次调用应带 operation name、round、agent、开始/结束时间、异常类型和 controller query/action 计数，供轨迹和诊断使用。

## 4. 预算、动作与失败语义

### 4.1 三类预算

```python
@dataclass(frozen=True)
class BudgetState:
    logical_rounds_used: int
    logical_rounds_limit: int
    physical_actions_used: int
    physical_actions_limit: int
    controller_queries_used: int
    controller_queries_limit: int | None
```

- `logical_round`：一次由 `open_round()` 至 `CLOSED` 的协调决策周期。
- `physical_action`：实际送入 Unity 的单个 agent action；timeout 的 `Pass` 是否计入必须固定并写入 benchmark config。推荐计入，以防通过缺席规避环境步数。
- `controller_query`：对环境的非 action 查询，例如 reachable positions 或 segmentation metadata；避免导航或 debug 查询掩盖实际计算成本。

宏动作必须在执行前通过“最坏情况下的 physical action 数”做原子预算预检。若剩余预算不足，整个宏动作被拒绝为 `BUDGET_EXHAUSTED`，不得执行前半段；后续 agent 看到明确的轮次结果。

### 4.2 动作结果

每个 action 生成独立 `AgentActionResult`：

```text
round_id, agent_alias, action_kind, target_alias?,
accepted, executed, result_code, error_category?,
physical_actions_delta, controller_queries_delta,
public_observation_delta, timestamp
```

`result_code` 需区分至少：`SUCCESS`、`INVALID_ACTION`、`TARGET_NOT_VISIBLE`、`PRECONDITION_FAILED`、`CONTROLLER_FAILURE`、`TIMEOUT_PASS`、`DUPLICATE_SUBMISSION`、`BUDGET_EXHAUSTED`、`RUN_STOPPED`。

这使 LLM 接收的是可解释的领域结果，而不是 Python exception、raw object id 或内部堆栈。

## 5. 可见性、身份与 oracle 防护

### 5.1 Public / internal DTO 分层

内部环境状态可以包含 raw object id、完整 metadata、task initializer 信息和 checker 辅助数据，但不得直接进入：

- worker 或 coordinator prompt / Context Memory；
- A2A HTTP payload；
- 普通 experiment 日志；
- dashboard、轨迹摘要或对外结果。

对 LLM 和公共日志只暴露 `PublicEnvironmentState`：已公开对象、当前可见对象、公共别名、agent pose（按权限裁剪）、持有物、最近 action 结果、已知地图和公共任务进度。

### 5.2 Opaque alias

不使用按全局 object id 排序得出的稳定名称（例如 `Bread_1`），因为它会泄漏未见对象数、初始化顺序或世界结构。

改为：对象首次进入某角色可见的公共视图时，按该视图的发现次序分配 opaque alias，例如 `object-A7`。内部 id 到 public alias 的映射仅在 adapter 内保存。未公开对象、raw id、checker truth、目标房间捷径和全局 inventory 一律不进入 public DTO。

### 5.3 成功真值

legacy checker 若只提供 progress 或启发式信息，应命名为 `legacy_progress_*`，可供诊断但不得用于：

- `is_finished()` 的成功分支；
- run 终止条件；
- benchmark success rate；
- 对 coordinator 的“已完成”通知。

每个 benchmark task 都需要一个明确的 `verify_postconditions(final_metadata)`。只有该 verifier 返回通过时才写入 `verified_completion=True`；这是唯一的任务成功真值。无 verifier 的任务可以做探索 smoke，不得进入正式 benchmark。

## 6. 状态投影与 Context 连续性

### 6.1 Coordinator 投影

Coordinator 只接收公共、跨 agent 的任务状态：

- `BudgetState` 与本轮计划/实际差额；
- public agent alias、位置、朝向、持有物与健康状态；
- 经可见性过滤的 object / room / map 事实；
- round log、待执行和失败原因；
- `legacy_progress_*`（显式标记为非成功）；
- `verified_completion`、terminal reason 和公共 metrics。

### 6.2 Worker 投影

Worker 每次工具调用前、回合结果后或指定 refresh hook 时刷新，只看：

- 自身 pose、inventory、局部可见对象和局部地图；
- 分配子任务、公开通信摘要；
- 自身和本轮 action result；
- 剩余三类预算。

任何位置或 map state 的主事实源必须是 barrier snapshot，而不是 LLM observation 文本。状态投影需带 revision / round id，防止异步回调造成旧结果覆盖新状态。

### 6.3 观测与日志

每轮写入可解析的 `events.ndjson` / trajectory 记录：公开输入、接受/拒绝决策、固定执行顺序、公共结果、预算 delta、状态 revision、terminal reason。原始 metadata 若为调试所需，应保存在受控本地调试工件中，默认不上传、不注入 LLM 且不作为普通日志。

推荐新增指标：

- `VerifiedSuccessRate`：以 `verified_completion` 计算；
- `CostPerVerifiedSuccess`；
- `AgentUtilization`：有意义 action / 可用 logical rounds；
- `TimeoutPassRate`、`InvalidActionRate`；
- `PhysicalActionsPerVerifiedSuccess`；
- `ControllerQueriesPerRound`；
- `PublicStateFreshness`：context revision 滞后情况。

## 7. 任务 catalog 与验证策略

任务 catalog 的每个候选必须记录：

```text
task_id, scene_initializer, initializer_signature_status,
checker_import_status, dependency_closure_status,
runtime_smoke_status, postcondition_verifier_status,
benchmark_eligible, exclusion_reason
```

进入 benchmark 的门槛：

1. 依赖闭包可从项目声明和 lockfile 重建；
2. scene initializer 可在专用 executor 中运行；
3. 至少一个两 agent Controller smoke 成功；
4. 目标所需动作的 precondition / postcondition 可验证；
5. 不存在 raw id 或终局 truth 泄漏到公共 state 的路径；
6. 存在经过单元测试的 `verify_postconditions`。

静态统计（任务目录、checker 数、FloorPlan 数）不构成 eligibility 证明。缺 checker 或缺 verifier 的任务必须显式标为 `exploration_only` 或排除。

## 8. 分阶段实施与验收门禁

### Phase 0 / G0：可复现依赖闭包

**任务 1：建立 `ai2thor` optional extra。**

- 在 `pyproject.toml` 新增 `ai2thor` extra；按实际 import closure 声明 AI2Thor、数值/导航和 checker 依赖。必须包含：`ai2thor`、`scipy`、`pandas`、`torch`、`sentence-transformers`、`opencv-python`（`AI2Thor/env_new.py` 导入 `cv2`）。
- 若 `torch` / `sentence-transformers` 体积过大（>700MB）且仅用于 legacy task_mapper，可评估是否将其从 Phase 0 import smoke 中隔离，但必须在 `ai2thor` extra 中声明。
- 使用 `uv` 生成并提交一致的 `uv.lock`；不依赖本机 venv 漂移。
- 修复 `AI2Thor/` 与 `thortils/` 的导入路径：这两个目录不是合法包，依赖 `sys.path` 注入；G0 必须使它们可通过 `PYTHONPATH` 或 `pyproject.toml` 的 `packages` / `tool.uv.sources` 被稳定导入。
- 增加无 Unity 启动副作用的 import smoke：AI2Thor、`thortils`、目标 checker、scene initializer。对 `torch` / `sentence-transformers` 的 import smoke 可单独标记为 `requires_large_deps`。
- 确认测试收集路径不会因 optional dependency 缺失而隐性跳过。所有测试在 `fake` 模式下应可通过；`unity` 模式测试默认跳过，仅在 `LLAMAR_AI2THOR_MODE=unity` 时运行。

**G0 通过条件：**从干净环境安装对应 extra 后，所有列出的 import smoke 和基础 pytest collection 成功。否则停止，不实现 barrier。

### Phase 1 / G1：真实 Controller runtime smoke

**任务 2：编写独立、无 LLM 的 runtime probe。**

probe 必须在 WSL / 远程 headless Linux 实际启动/连接 Unity Controller，并全部在单 executor 内验证：

- `agentCount=2` 与 `MultiAgentEvent` 分 agent metadata；
- scene reset 与任务 initializer；
- instance segmentation / 可见对象读取；
- reachable-position query；
- 受限 Move / Rotate；
- Pickup、Put、Open、Close 的严格前后置条件；
- executor 内 `Controller.stop()`；
- timeout、异常和清理后无遗留子进程或端口；
- headless 支持：`xvfb` 或 `DISPLAY` 缺失时的启动参数；远程 A100 上的 build/GPU 配置记录。

**G1 通过条件：**probe 产生机器可读报告，并完整记录 Unity/build、OS/WSL、Python、GPU/渲染配置、成功 action 和失败诊断。仅 `import ai2thor` 不通过 G1。本地若无 GPU，G1 可在远程 A100 上执行，但必须有可复现脚本。

### Phase 2 / G2：契约与串行执行器

**任务 3：实现 DTO、预算和 `ControllerExecutor`。**

- 实现 Public / internal DTO、`BudgetState`、错误码和统一 action validation。
- 实现 one-controller-per-run executor，并测试所有 controller 调用被串行化。
- 为 executor 加 operation log、timeout 分类、stop 幂等性和资源释放测试。

**任务 4：实现 explicit-round `AI2ThorBarrier`。**

- 按第 3.3 节实现 state machine；不从 SARBarrier 继承 SAR 行为。
- 覆盖零提交、重复提交、部分 timeout、取消、异常、stop 和结束后拒绝输入。
- 使用 fake Controller 做 deterministic unit test；运行时测试只在 G1 后执行。

**G2 通过条件：**round state machine 与预算测试完整通过，且任何 controller call 都无法绕过 executor。

### Phase 3 / G3：A2A 生命周期与 runner

**任务 5：新增 `EnvironmentRunControl` 注入。**

- 在 A2A core 增加环境无关接口和注册方法；不改变 SAR map/dashboard 语义。
- 修改现有 cancel 端点，统一通过 `run_control.request_stop()` 停止；保留未注册时的 `barrier.stop()` 回退路径。
- Watchdog / cancel / graceful shutdown 先调用 `request_stop()`，再 drain worker、Coordinator 和 server。
- 用 fake control 验证 stop 调用顺序、幂等性及异常情况下的 cleanup。
- **Worker 停止传播**：`request_stop()` 需唤醒 AI2ThorBarrier 中等待的 worker 协程/线程；worker 侧收到 `TASK_CANCEL` 后退出 Agent 循环。Controller 的 `stop()` 与 worker 进程清理由 `AI2ThorExperiment` 在协调器与 worker 都 drain 后统一执行。

**任务 6：实现 `ai2thor_orch/experiment.py`。**

- 创建 run root、配置快照、catalog selection、executor、barrier、server 和 shutdown order。
- persist 公共轨迹、metrics、验证报告和失败分类；敏感内部 metadata 默认隔离。

**G3 通过条件：**不使用 LLM 的两 agent smoke 可重复运行、超时退出且无残留资源；SAR 定向回归仍通过。

### Phase 4 / G4：最小工具和状态连续性

**任务 7：实现最小 worker tools。**

按需、分批加入：Move、Rotate、Look、可见目标查询、Pickup、Put、Open、Close。每个工具只接收 public alias，调用 barrier，并将标准 `AgentActionResult` 返回给 LLM。

导航宏动作、Explore 和复杂交互必须在直接动作 + 可见性 + 预算语义稳定后实现；不能提前以宏动作绕过 physical action budget。

**任务 8：实现 state providers 与 Context hooks。**

- Worker / Coordinator 投影分别按第 6 节实现；
- 基于 barrier revision 更新，避免 observation 文本成为权威位置来源；
- 为回合结果、工具失败、取消和 terminal state 做刷新测试；
- 验证 raw id、未见对象与 checker truth 不进入 public context；
- 验证 `AI2ThorBarrier.step_counter` / `revision` 能被 `TaskWatchdog` 与 state provider 稳定读取。

**G4 通过条件：**工具、context、日志三处都只能看到允许的公共状态；局部可见性和 alias 泄漏测试通过。

### Phase 5 / G5：任务 verifier 与 benchmark

**任务 9：实现 task catalog 和终局 verifier。**

- 为首个任务实现严格 `verify_postconditions`，包括每个目标物体和容器的最终 metadata 条件。
- 将 legacy checker 仅记录为进度；测试其不能改变 success。
- 逐个验证候选任务，并将无 verifier / runtime incompatibility 的任务排除。

**任务 10：端到端 benchmark 与回归。**

- 在 `fake` 模式下运行 scripted 两 agent end-to-end run，验证 barrier、tools、verifier、metrics 全链路；
- 在远程 headless A100 上运行 `unity` 模式 benchmark，记录真实 Controller 配置与成功率；
- 生成 `VerifiedSuccessRate` 等指标；
- 在 CI / 本地分层运行 fake-controller unit、依赖 smoke、可选 Unity integration；
- 对 A2A core 与 SAR 定向集运行回归。

**G5 通过条件：**至少一个 catalog task 在真实 Unity 运行时完成、`verified_completion=True` 且所有公共状态、预算和关闭语义审计通过；同时 `fake` 模式 scripted run 在无 Unity 环境下也能稳定通过。

## 9. 测试矩阵

| 层级 | 重点 | 不启动 Unity |
|---|---|---|
| DTO / policy | alias、可见性、raw-id redaction、错误码 | 是 |
| Budget | round/action/query 计数、宏动作原子拒绝 | 是 |
| Barrier | zero-submit、duplicate、timeout、cancel、stop | 是，fake Controller |
| Executor | 单线程、operation 顺序、stop / timeout cleanup | 是，fake Controller |
| A2A lifecycle | EnvironmentRunControl、watchdog、shutdown 顺序 | 是 |
| Dependency | clean install、import closure、pytest collection | 是 |
| Unity smoke | controller、2 agent、scene、action 后置条件 | 否，G1 后 |
| E2E | tools、context、verifier、metrics | 否，G4/G5 后 |
| SAR regression | 既有 SAR 定向测试 | 是 |

## 10. 风险与停止条件

| 风险 | 影响 | 处理与停止条件 |
|---|---|---|
| WSL / Unity runtime 无法启动 | 无法进行真实实验 | G1 未通过则停止 adapter 开发，先解决环境或改用受支持宿主；确保远程 A100 headless 复现路径 |
| 依赖只在本地 venv 可用 | CI / 他人不可重建 | G0 未通过则不开始 barrier；`torch` / `sentence-transformers` 必须从 `ai2thor` extra 声明并锁入 `uv.lock` |
| 多线程调用 Controller | 非确定性、崩溃、不可重放 | 只允许 ControllerExecutor；发现绕过调用即阻断 |
| checker 被误用为 success | benchmark 虚高 | 无 postcondition verifier 不得纳入 benchmark |
| alias 或 context 泄漏 | oracle 信息污染 LLM 评测 | public/internal DTO 测试失败即阻断 G4 |
| timeout / cancel 卡死 | server 与 Unity 资源泄漏 | G2/G3 未覆盖 terminal state 则不接 LLM |
| 宏动作隐藏成本 | 预算不可比较 | 预检与 physical-action 记账未实现前不开放宏工具 |
| 本地无 GPU 却默认启动 Unity | 开发机无法测试、CI 崩溃 | 默认 `fake` 模式；`unity` 模式显式开启并在 headless A100 复现 |

## 11. 预期文件范围

新增文件预计包括：

```text
ai2thor_orch/
  __init__.py
  controller_executor.py
  contracts.py
  barrier.py
  state_provider.py
  verifier.py
  task_catalog.py
  experiment.py
  tools/worker/{move,rotate,look,objects,interact}.py
  tests/{test_executor,test_barrier,test_budget,test_visibility,test_verifier}.py

scripts/ai2thor_runtime_smoke.py
```

可能修改的既有文件仅限：

```text
pyproject.toml                 # ai2thor extra 与声明依赖
uv.lock                        # 可复现锁定
src/a2a/...                    # 仅 EnvironmentRunControl 的环境无关注入点
tests/...                      # lifecycle / contract 回归
```

`AI2Thor/` 与 `thortils/` 先作为复用资产；只有在 G0/G1 证据表明某个最小修复确有必要时，才单独评审修改。

## 12. 实施前检查清单

开始写代码前，确认：

- [ ] 当前分支与未提交改动已记录；不覆盖用户现有删除的 `ai2thor_integration_analysis.md`。
- [ ] G0 clean install 和 import closure 报告已保存；`torch` / `sentence-transformers` / `scipy` / `pandas` / `opencv-python` 已在 `ai2thor` extra 中声明并锁入 `uv.lock`。
- [ ] `AI2Thor/` 与 `thortils/` 的导入路径已通过 `PYTHONPATH` 或包配置治理，不再依赖隐式 `sys.path` 注入。
- [ ] G1 Unity runtime probe 已通过或明确记录失败原因；headless A100 复现脚本已准备。
- [ ] 首个 task 的 initializer、动作范围、postcondition verifier 已选定。
- [ ] ControllerExecutor、round state machine 和三类预算的测试先于工具实现。
- [ ] Public/internal DTO、opaque alias 和日志脱敏策略已评审。
- [ ] AI2ThorBarrier 内部使用 `threading` 原语，接口层对 worker 线程安全；已验证跨线程等待/唤醒。
- [ ] A2A stop / watchdog 与 server shutdown 的调用顺序有 regression test；`barrier.stop()` 与 `run_control.request_stop()` 不会双重触发。
- [ ] SAR 回归集作为每阶段变更后的保护网。
- [ ] 本地默认运行使用 `fake` 模式；`unity` 模式仅通过显式环境变量在远程 A100 上开启。

## 13. 完成定义

AI2Thor 适配不以“新建了 barrier 文件”或“agent 发出了动作”定义完成。最小完成定义是：在可复现环境中，一个真实两 agent AI2Thor task 通过受限工具、单线程 Controller、逻辑回合 barrier 和公共状态投影运行到终局；其成功由终局 metadata 后置条件唯一确认，并可从 run artifacts 重放动作、预算、状态 revision 和停止原因。
# 语义地图模块化与步间连续性增强计划

日期: 2026-07-17
文档类型: 架构设计 / 实施计划
状态: 已完成（截至 2026-07-18，Phase 0–7 已验收）

## 实施跟踪

| Phase | Subagent | 主 Agent 审查 | 状态 | 说明 |
|---|---|---|---|---|
| 0 | deepseek-v4-flash（主 Agent 补齐第 3 轮） | 通过（2026-07-18） | 完成 | 测试契约已覆盖 fire/person/agent 乱序位置、revision 原子快照、MapDiff 的稳定投影/resolve/lost、MapSummarizer 的 blocking single-flight/同实例 timeout/usage/JSONL/150 字、以及 Provider→Context→Hook 链路。53 个 future-contract RED 均为已知 feature-absent assertion/import，10 个既有 GREEN 通过；ruff、diff check 通过；仅 tests/ 与本计划改动。 |
| 1 | deepseek-v4-flash | 通过（2026-07-18） | 完成 | `SemanticMapStore` 与 `WorkerReportPublisher` 已无行为变更地迁移至 `sar_orch.map`；所有生产 import 已切换，新旧路径 class object identity 由测试锁定。独立复验 36 passed + 2 个 compatibility tests passed；新增目录与改动文件的 ruff、diff check 通过。全量 `sar_orch/` ruff 仍被 `evolve_alignment.py`、`launch_dashboard.py` 的 7 个既有错误阻塞。 |
| 2 | deepseek-v4-flash | 通过（2026-07-18） | 完成 | `SemanticMapStore` 现以单锁提供 `_revision` 与原子 `snapshot_with_revision()`；有效 observation、首次 prior 初始化、值变化的 budget 更新会推进 revision；fire/person/agent 均不会被旧 step 位置回退。独立复验 `tests/test_semantic_map.py` 19 passed，legacy identity 2 passed；targeted ruff、diff check 通过。 |
| 3 | deepseek-v4-flash（主 Agent 补强） | 通过（2026-07-18） | 完成 | `MapDiffCalculator` 为纯 dict 比较器，稳定输出 fire/person/conflict/stale delta。主审发现并以 RED→GREEN 补强：revision metadata 不再硬编码、multi-object delta 使用稳定排序、conflict/stale 均按 `(object_type, name)` 对齐。独立复验 map-diff + semantic-map 39 passed；targeted ruff、tracked/untracked diff whitespace check 通过。 |
| 4 | deepseek-v4-flash（主 Agent 补强） | 通过（2026-07-18） | 完成 | `MapSummarizer` 以 `asyncio.Lock` 在 await 前 claim revision，具备 timeout/error 降级、JSONL、token sink、compact projection 与 `SummaryTrigger`。主审新增并修复真实 router `Message` 输入、无 usage 或 mapping usage、诊断写入失败、首个低优先级 delta、乱序 revision completion、稳定排序/噪声脱敏和 typed trigger；map summarizer/diff/store 61 passed，targeted ruff/diff check 通过。 |
| 5 | deepseek-v4-flash（主 Agent 补强） | 通过（2026-07-18） | 完成 | 新增可选 `AsyncStatePreparer`，并在 hook 中确保 prepare → refresh → prune → assemble。Provider 用原子 revision/snapshot 建立 baseline、计算 delta、按 revision 触发 summary，且 oracle 不泄露 semantic fields。主审以 RED→GREEN 补强：先同步 snapshot 后仍能建立 baseline、无地图变更的 env step 也递增 runtime version、snapshot-only consumer 同步看到同 step revision。非 Phase-6 provider 22 passed、hooks 4 passed、context 5 passed；ruff/diff check 通过。Phase 6 pinned/render contracts 仍按预期 RED。 |
| 6 | deepseek-v4-flash（主 Agent 补强） | 通过（2026-07-18） | 完成 | `run_experiment()` 向 coordinator 注入 run 根目录的 `map_summary.jsonl`；semantic mode 且 logger/path 齐全时才构造 `MapSummarizer`，其 token sink 以当前 barrier step 写入 `MapSummarizer` token row 后显式 `flush_summary()`。主审以 RED→GREEN 修复 legacy semantic tool 的 worker 兼容投影、渲染器与实际 MapDiff `{old,new}` schema 不匹配、以及异常 oversized summary 的 150 字上限。Context 按优先级渲染最多 5 条高价值 change，digest 含两个 revision，oracle 不渲染。Phase 0–6 selected suite 129 passed，targeted ruff/diff check 通过；完整 `tests/` 为 748 passed、1 skipped，另有 2 个未改动的 sandbox test 因硬编码旧仓库名 `LLaMAR` 在当前 `LLaMAR-sematic_map` 路径失败。 |
| 7 | N/A（真实 smoke） | 通过（2026-07-18） | 完成 | 已运行 `scene=1, agents=2, seed=42, semantic, max_steps=20`。run 根目录生成并通过 JSON 解析的 `map_summary.jsonl`（7 条，5 success/2 timeout）；timeout 保留上一成功 summary，未中断 run。`token_usage.csv` 有 5 条 `MapSummarizer` row（steps 1/3/12/17/20，total 3703），`summary.csv` 聚合完全一致；coordinator 第 6 个 LLM request 已实际携带 `### Map Changes (revision 10)` 与 `### Map Summary (revision 10)`。20 step 到达安全上限（coverage=1.0、transport=0.9333、finished=false），无 barrier timeout agent。 |

## 目标

将语义地图实现收敛到 `sar_orch/map/`，并为 semantic-mode coordinator 提供两种互补的连续性信息：

1. **确定性的结构化地图变化**：每个 semantic map revision 都可得到稳定、属性级的 `map_delta`。
2. **受预算约束的自然语言总结**：仅在周期性或高优先级变化时调用 LLM，生成简短的 `map_summary`。

两者都进入现有 `RuntimeState -> CoordinatorPinnedState -> Context Memory` 链路。完整 semantic map 仍保留，diff 与 summary 只补足跨轮次连续性，不能替代地图事实。

## 已确认的现状与约束

| 事实 | 当前实现 | 对本计划的约束 |
|---|---|---|
| Runtime state 注入是同步的 | `ContextManager.refresh_runtime_state()` 直接调用 `StateProvider.snapshot()` | `snapshot()` 必须保持同步、轻量、无 LLM I/O。 |
| coordinator 在 LLM 前有异步 hook | `CoordinatorSARHooks.pre_llm()` 是 `async` | 有界的 LLM summary 应在此处的准备阶段执行，再同步投影状态。 |
| 环境 poll loop 位于 experiment | `run_experiment()` 负责 barrier polling 与 step logging | 不在 `SARCoordinator` 中假设不存在的 poll loop；不从 experiment loop 跨线程写 continuity 状态。 |
| worker observation 会异步推送 | coordinator server 的 push callback 写入 `SemanticMapStore` | 不能只按 `env_step` 缓存 semantic map；同一步的新 observation 必须可见。 |
| agent 的实时位置来自 barrier | `SARCoordinatorStateProvider._build_team_status()` 每步用 barrier 覆盖 observation 位置 | agent movement 不属于 semantic-map diff 的事实源，也不得触发地图 summary。 |
| token usage 支持任意 agent 名 | `ExperimentLogger.log_token_usage()` 动态累积并写入 summary | summary 调用以 `Agent=MapSummarizer` 记录，必须使用同一 `ExperimentLogger`。 |

## 非目标

- 不改变 SAR 环境规则、worker observation 格式或 A2A 协议。
- 不用 LLM 修正、合并或覆盖 semantic map 的事实状态。
- 不把 agent 实时位置从 barrier 降级为 observation 驱动的状态。
- 不在每一步、每轮 LLM 都强制生成自然语言总结。

## 目标架构

```text
worker report
  -> coordinator push callback
  -> SemanticMapStore.ingest_observation() [lock + map_revision++]
  -> CoordinatorSARHooks.pre_llm()
       -> ContextManager.prepare_runtime_state(agent.llm)
          -> SARCoordinatorStateProvider.prepare_for_llm()
             -> snapshot_with_revision() [atomic]
             -> MapDiffCalculator.diff() [pure / deterministic]
             -> MapSummarizer.maybe_summarize() [async, deadline, single-flight]
       -> ContextManager.refresh_runtime_state()
          -> StateProvider.snapshot() [sync, cached projection only]
          -> CoordinatorPinnedState
       -> Context Memory render
```

关键边界：`prepare_for_llm()` 可以异步；`snapshot()` 不可以。总结超时或失败时，`snapshot()` 仍返回最近成功 summary 或空字符串，coordinator 主链路继续运行。

## 目录与公共 API

```text
sar_orch/
├── map/
│   ├── __init__.py              # 显式 public API
│   ├── store.py                 # SemanticMapStore、ObservationRecord 等
│   ├── publisher.py             # WorkerReportPublisher
│   ├── diff.py                  # MapDiffCalculator、稳定 MapDelta schema
│   └── summarizer.py            # MapSummarizer、SummaryTrigger
├── semantic_map.py              # 兼容 shim（显式 re-export）
├── observation_publisher.py     # 兼容 shim（显式 re-export）
├── coordinator_state_provider.py
├── coordinator.py
├── worker.py
└── results/<run_id>/
    ├── semantic_map.jsonl
    ├── map_summary.jsonl        # 新增，位于 run 根目录
    ├── token_usage.csv
    ├── coordinator/
    ├── <agent-name>/
    └── supervision/
```

`map_summary.jsonl` 必须通过显式 `map_summary_path` 注入，不能从 coordinator 的 `log_dir` 推断；后者当前是 `<run>/coordinator`，而实验级产物应与 `semantic_map.jsonl` 同级。

### `sar_orch/map/__init__.py`

导出下列稳定 API：

```python
from sar_orch.map.store import (
    AgentSemanticState,
    ObservationRecord,
    SemanticMapStore,
    SemanticObject,
    TERMINAL_STATUS_ORDER,
)
from sar_orch.map.publisher import WorkerReportPublisher
from sar_orch.map.diff import MapDiffCalculator
from sar_orch.map.summarizer import MapSummarizer, SummaryTrigger

__all__ = [
    "AgentSemanticState",
    "MapDiffCalculator",
    "MapSummarizer",
    "ObservationRecord",
    "SemanticMapStore",
    "SemanticObject",
    "SummaryTrigger",
    "TERMINAL_STATUS_ORDER",
    "WorkerReportPublisher",
]
```

### 兼容 shim

不要使用裸 `import *`，避免迁移后意外暴露 `copy`、`Path`、typing 名称等内部实现。shim 显式导出与原模块兼容的公共名称，并用测试锁定兼容性：

```python
# sar_orch/semantic_map.py
from sar_orch.map.store import (
    AgentSemanticState,
    ObservationRecord,
    SemanticMapStore,
    SemanticObject,
    TERMINAL_STATUS_ORDER,
)

__all__ = [
    "AgentSemanticState",
    "ObservationRecord",
    "SemanticMapStore",
    "SemanticObject",
    "TERMINAL_STATUS_ORDER",
]
```

`observation_publisher.py` 同理，仅 re-export `WorkerReportPublisher`。

---

## 实施计划

### Phase 0：先建立回归测试与数据契约

**目标：** 在迁移前锁定现有 API，并用失败测试定义 revision、位置顺序和状态连续性的正确行为。

**文件：**
- 修改: `tests/test_semantic_map.py`
- 新建: `tests/test_map_diff.py`
- 新建: `tests/test_map_summarizer.py`
- 修改: `tests/test_coordinator_state_provider.py`
- 修改: `tests/test_context_snapshot.py`
- 新建: `tests/test_coordinator_hooks.py`

**步骤：**

1. 为现有 `SemanticMapStore` 写两个失败测试：同一对象先收到 step 8 的位置、再收到 step 7 的位置时，最终位置仍为 step 8；agent observation 同样不得回退。
2. 写失败测试：同一 `env_step` 内 ingest 一条新 observation 后，`snapshot_with_revision()` 的 revision 必须增加，且拿到的新 snapshot 含该对象。
3. 写 `MapDiffCalculator` 的 baseline、stable projection、stale/conflict 解除、`change_count` 用例；测试应只依赖普通 dict snapshot，不依赖环境或 LLM。
4. 写 summarizer 的 fake LLM 测试：触发原因、single-flight、timeout、持久化、token sink 与 150 字硬截断。
5. 写 provider/context/hook 集成测试：同一步 revision 变化会更新 pinned `map_delta`；summary 在同一 revision 后续 LLM round 仍可见；hook 先 prepare 后 refresh。
6. 分阶段运行对应测试，确认 migration 前只有新增契约测试失败，旧回归测试保持通过。

### Phase 1：迁移地图模块并保持 import 兼容

**目标：** 仅移动代码与 import，不在本阶段改变行为。

**文件：**
- 新建: `sar_orch/map/__init__.py`
- 新建: `sar_orch/map/store.py`
- 新建: `sar_orch/map/publisher.py`
- 修改: `sar_orch/semantic_map.py`
- 修改: `sar_orch/observation_publisher.py`
- 修改: `sar_orch/coordinator.py`
- 修改: `sar_orch/coordinator_state_provider.py`
- 修改: `sar_orch/worker.py`
- 修改: `sar_orch/tools/coordinator/query_semantic_map.py`
- 修改: `sar_orch/tools/coordinator/query_team_status.py`

**步骤：**

1. 将 `semantic_map.py` 的实现移动到 `map/store.py`，将 `observation_publisher.py` 的实现移动到 `map/publisher.py`；第一步不重命名类、函数或字段。
2. 在新模块中定义显式 `__all__`，再创建上述兼容 shim。
3. 将全部生产 import 改为 `from sar_orch.map import ...`。特别覆盖 `worker.py` 中的 `WorkerReportPublisher`，不能只修改 coordinator 侧。
4. 保留少量 legacy-import 测试，断言 legacy 与新路径导出的 class object 相同；其余测试改用新公共 API。
5. 运行 semantic map、worker observation、coordinator state 的现有测试，确认模块迁移没有行为变化。

### Phase 2：为 SemanticMapStore 引入原子 map revision，并修复乱序位置

**目标：** 建立可观测、原子的地图版本，使同一 env step 内异步 observation 不会被缓存掩盖。

**文件：**
- 修改: `sar_orch/map/store.py`
- 修改: `tests/test_semantic_map.py`

**数据契约：**

- `SemanticMapStore` 在锁保护下维护 `_revision: int`，初始为 0。
- 每个造成 map 事实或 freshness 语义变化的成功 mutation 后递增 revision：有效 observation ingestion、首次 prior 初始化，以及值确实变化的 `update_step_budget()`。
- 新增 `snapshot_with_revision(max_stale_steps=5) -> tuple[int, dict[str, Any]]`。revision 与 snapshot 必须在同一把 store lock 内产生。
- 原有 `snapshot()` 保持兼容，内部调用 `snapshot_with_revision()` 并只返回 snapshot。

**步骤：**

1. 在 `_merge_locked()` 中把 `SemanticObject.position` 更新改为 `rec.step >= existing.last_seen_step` 才允许覆盖；保留原有 terminal-status 合并规则。
2. 对 agent observation 路径使用相同的 step guard。若 agent 的旧 observation 仍需保存来源，可以追加来源，但不能回退 `last_position`。
3. 让 `update_step_budget()` 仅在 budget dict 真正改变时 bump revision，避免无意义的 revision 风暴。
4. 让 `ingest_observation()` 在 mutation 成功并完成 JSONL event 前/后以一致顺序 bump revision；失败不得生成 revision 空洞。
5. 运行 Phase 0 的乱序与原子 snapshot 测试，再运行完整 `tests/test_semantic_map.py`。

### Phase 3：实现纯函数 MapDiffCalculator

**目标：** 用稳定投影比较连续 revision，输出可测试、可渲染、可用于触发策略的 `map_delta`。

**文件：**
- 新建: `sar_orch/map/diff.py`
- 修改: `tests/test_map_diff.py`

**边界：**

- 输入只接受 semantic map snapshot；不读取 barrier、文件、时钟或 LLM。
- **不**在 map delta 中计算 agent movement。agent 实时 position/inventory 继续由 `team_status_summary` 的 barrier 投影提供。
- baseline（没有 previous snapshot）返回 `None`，不把全量初始地图伪装成“新增”。
- `lost` 仅作为 schema 保留字段；当前 store 不删除 objects，真实缺失通过 stale/resolved 表达。

**稳定投影规则：**

- 对 fires/persons 使用 `(object_type, name)` 对齐。
- 比较 `position`、top-level `status`，以及筛选后的 `attributes`。
- 排除会产生噪声的 `last_seen_ts`、`sources`、`recent_observations`、`confidence`、`observed_cells`。
- `attributes["status"]` 不重复计为 attributes change；强度使用专用 `intensity_changed`。
- conflict 与 stale 使用 `(object_type, name)` 的集合差，输出新增与解除两类事件。

**输出 schema：**

```python
{
    "env_step": 12,
    "base_revision": 41,
    "revision": 42,
    "change_count": 4,
    "fires": {
        "gained": [{"name": "fire-A", "position": [1, 2, 0], "intensity": 3}],
        "lost": [],
        "intensity_changed": [{"name": "fire-B", "old": 5, "new": 3}],
        "status_changed": [],
        "position_changed": [],
        "attributes_changed": [],
    },
    "persons": {
        "gained": [],
        "lost": [],
        "status_changed": [{"name": "person-1", "old": "trapped", "new": "rescued"}],
        "position_changed": [],
        "attributes_changed": [],
    },
    "conflicts_new": [],
    "conflicts_resolved": [],
    "stale_new": [],
    "stale_resolved": [],
}
```

`change_count` 只统计语义对象与 conflict/stale 状态变化，绝不统计正常 agent movement。

### Phase 4：实现有预算、可降级的 MapSummarizer

**目标：** 对高价值 map delta 生成短总结，不让 LLM 调用破坏 coordinator 的正确性、时延或日志。

**文件：**
- 新建: `sar_orch/map/summarizer.py`
- 修改: `tests/test_map_summarizer.py`

**接口与所有权：**

```python
class SummaryTrigger:
    reasons: tuple[str, ...]  # periodic, fire_change, person_change, conflict, stale

class MapSummarizer:
    def __init__(
        self,
        *,
        summary_path: Path,
        token_usage_sink: Callable[..., None],
        trigger_interval: int = 5,
        summary_timeout_seconds: float = 5.0,
        max_summary_chars: int = 150,
    ) -> None: ...

    async def maybe_summarize(
        self,
        *,
        llm_client: Any,
        env_step: int,
        map_revision: int,
        snapshot: dict[str, Any],
        map_delta: dict[str, Any],
    ) -> str: ...
```

`MapSummarizer` 不在构造函数中保存 `SARCoordinator._llm_client`；该属性当前不存在。调用时从 `CoordinatorSARHooks.pre_llm()` 接收本轮 `agent.llm`。`SARCoordinator` 只负责注入 `ExperimentLogger.log_token_usage` 和明确的 `summary_path`。

**触发规则：**

1. 有语义变化时，距离最近一次成功/尝试 summary 达到 5 个 env step，触发 `periodic`。
2. 新 person、person terminal status、fire 新增/强度/状态变化，触发对应高优先级原因。
3. conflict/stale 的新增或解除触发对应原因。
4. 不因 agent movement、sources append、timestamp、confidence 单独触发。
5. 同一 `map_revision` 至多发起一次调用；返回 `SummaryTrigger` 而不是仅返回 bool，持久化 trigger reasons。

**并发、超时与输入预算：**

- summarizer 在 coordinator server 的事件循环内用 `asyncio.Lock` 保护 `last_attempted_revision`、`last_summary` 与 JSONL append；先标记 attempt，再 `await`，避免并发 pre-LLM 轮次重复付费。
- 外层用 `asyncio.wait_for(..., timeout=5.0)`。timeout、网络异常、usage 缺失都不向上抛出；保留上次成功 summary 并记录状态。
- 输入不是原始完整 snapshot。构建 deterministic compact projection：本次 delta、step budget、最多 10 个 active fire/person 的稳定字段、最多 5 项 stale/conflict，以及上一条 summary。不得传 `sources`、原始 observations 或未裁剪 `observed_cells`。
- prompt 要求中文、聚焦变化、不臆测；响应写入前实施确定性字符上限，超过 150 字按安全边界截断。

**持久化与 token：**

每次 attempt append 一行到 `<run>/map_summary.jsonl`：

```json
{"env_step": 10, "base_revision": 41, "map_revision": 42,
 "trigger_reasons": ["fire_change"], "status": "success",
 "summary": "...", "token_usage": {"prompt_tokens": 0, "completion_tokens": 0,
 "total_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0},
 "timestamp": 1234567890.0}
```

成功响应且有 usage 时，以 `agent="MapSummarizer"` 调用 `ExperimentLogger.log_token_usage()`；使用当前 run 的默认 RunID/Model/PromptVersion。随后调用 `flush_summary()`，保证低频 summary token 不会在下一次 poll 前丢失。

### Phase 5：在 StateProvider 中协调 revision、diff 与异步准备

**目标：** 让 state provider 成为 continuity 的单一状态所有者，同时保持 `snapshot()` 的同步 API。

**文件：**
- 修改: `sar_orch/coordinator_state_provider.py`
- 修改: `src/Agent/router_agent/state_provider.py`
- 修改: `src/Agent/router_agent/context.py`
- 修改: `src/Agent/router_agent/hooks.py`
- 修改: `tests/test_coordinator_state_provider.py`
- 新建: `tests/test_coordinator_hooks.py`

**StateProvider 扩展：**

在 `state_provider.py` 新增可选 `AsyncStatePreparer` protocol；不要把 `StateProvider.snapshot()` 改成 async：

```python
class AsyncStatePreparer(Protocol):
    async def prepare_for_llm(self, llm_client: Any) -> None: ...
```

`ContextManager.prepare_runtime_state(llm_client)` 通过 `isinstance`/`hasattr` 调用该可选协议；普通 provider 保持 no-op。`CoordinatorSARHooks.pre_llm()` 的顺序固定为：

```python
await self._ctx.prepare_runtime_state(agent.llm)
self._ctx.refresh_runtime_state()
self._ctx.prune_history(agent.messages)
return self._ctx.assemble(agent.system_prompt, agent.messages)
```

**`SARCoordinatorStateProvider` 内部状态：**

- `_semantic_cached: dict | None` 与 `_semantic_revision: int | None`
- `_previous_map_snapshot: dict | None` 与 `_previous_map_revision: int | None`
- `_last_map_delta: dict | None`
- `_last_summary: str` / `_last_summary_revision: int`
- 独立的 `_runtime_version: int`；不要再让现有 `_last_version` 同时代表 env step、cache revision 和 diff 推进条件。
- 一个由 `SARCoordinator` 注入的 `MapSummarizer | None`。

**`prepare_for_llm()` 算法：**

1. semantic mode 以外直接返回。
2. 调用 `SemanticMapStore.snapshot_with_revision()`，得到同锁的 `(revision, snapshot)`。
3. revision 未变化时直接返回；变化时以 previous snapshot 计算 delta。首次 snapshot 只建立 baseline。
4. 原子地更新 cache、previous snapshot、previous revision、`_last_map_delta` 和 runtime version。
5. 仅在 delta 存在时调用 `await summarizer.maybe_summarize(...)`；将返回/保留的 summary 存到 provider。
6. summary 错误只写 JSONL diagnostics，不使 provider snapshot stale。

**`snapshot()` 算法：**

- 仍是纯同步投影；它不得调用 LLM，也不得推进 previous snapshot。
- 若 `prepare_for_llm()` 尚未运行，安全读取 `(revision, snapshot)` 并建立 baseline/cache，保证 debug 或非 hook 调用可用。
- payload 追加：`map_revision`、`map_delta`、`map_summary`、`map_summary_revision`。
- `RuntimeState.version` 使用单调 `_runtime_version`，`RuntimeState.env_step` 保持真实 barrier step，避免同一步 map revision 更新被标记为未变化。

### Phase 6：构造路径、日志路径与 Context Memory 渲染

**目标：** 用明确的依赖注入把 summarizer 接入 coordinator，并让 LLM 能以受限篇幅看到 delta/summary。

**文件：**
- 修改: `sar_orch/coordinator.py`
- 修改: `sar_orch/experiment.py`
- 修改: `src/Agent/router_agent/context.py`
- 修改: `tests/test_context_snapshot.py`

**构造与路径：**

1. `SARCoordinator.__init__()` 新增 `map_summary_path: str | Path | None`，不增加假想的 `_llm_client` 字段。
2. `run_experiment()` 在已创建 `exp_dir` 后传入 `exp_dir / "map_summary.jsonl"`。
3. `SARCoordinator.start()` 使用该路径、`self._exp_logger.log_token_usage` 与 `self._exp_logger.flush_summary` 构造 `MapSummarizer`，然后注入 `SARCoordinatorStateProvider`。
4. 若未提供 experiment logger 或 summary path，summary feature 显式 disabled 并记录 warning；不能悄悄写入 coordinator log directory。

**Pinned state 与渲染：**

在 `CoordinatorPinnedState`、`_project_runtime_state_to_pinned()` 中增加：

```python
map_revision: int = 0
map_delta: dict = Field(default_factory=dict)
map_summary: str = Field(default_factory=str)
map_summary_revision: int = 0
```

渲染要求：

- `_render_environment_view()` 在已知 objects 后输出 `### Map Changes (revision N)`；按优先级渲染 person terminal、fire intensity/status、gained、conflict/stale 的新增和解除，最多 5 条，随后输出 `... and N more changes`。
- `map_delta` 中不渲染 agents。Environment 里的 Workers 改读 `ps.team_status_summary["workers"]`，以保持 barrier 的实时 position/inventory 为权威。
- `_render_current_state()` 在 step budget 后输出 `### Map Summary (revision N)`；只在 summary 非空时加入。
- state digest 包含 `map_revision` 与 `map_summary_revision`，不得在同一步发生地图变化后输出误导性的 “State unchanged since last round”。
- oracle mode 不生成或渲染 semantic map continuity 字段。

### Phase 7：全链路验证、日志检查与渐进启用

**目标：** 在不消耗真实模型成本的前提下验证 deterministic path，再做一个受限 smoke run。

**自动化测试矩阵：**

| 文件 | 关键用例 |
|---|---|
| `tests/test_semantic_map.py` | 乱序 fire/person/agent position 不回退；revision 只在有效 mutation 时增长；原子 snapshot/revision。 |
| `tests/test_map_diff.py` | baseline、gained、intensity/status/position、stable-field 排除、conflict/stale 新增与解除、无 agent delta。 |
| `tests/test_map_summarizer.py` | trigger reasons、同 revision single-flight、5 秒 timeout、异常回退、JSONL schema、token sink、150 字上限。 |
| `tests/test_coordinator_state_provider.py` | 同 env step observation 使 map revision/cache 刷新；delta 保留至下一 revision；summary failure 不使 RuntimeState stale。 |
| `tests/test_context_snapshot.py` | pinned projection、最多 5 条渲染、summary/revision digest、worker position 使用 team status。 |
| `tests/test_coordinator_hooks.py` | `pre_llm` 严格先 `prepare_runtime_state()` 再 `refresh_runtime_state()`；普通 provider 仍兼容。 |

**命令：**

```bash
cd /home/wyh/daily_work/LLaMAR-sematic_map
PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_semantic_map.py \
  tests/test_map_diff.py \
  tests/test_map_summarizer.py \
  tests/test_coordinator_state_provider.py \
  tests/test_context_snapshot.py \
  tests/test_coordinator_hooks.py -q

PYTHONPATH="src:$PYTHONPATH" uv run --with ruff ruff check src/ sar_orch/ tests/
```

通过 mock/fake LLM 测试后，运行受限真实 smoke：

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py \
  --scene 1 --agents 2 --seed 42 --max-steps 20
```

验证以下事实：

```bash
# <run_id> 替换为本次 experiment 目录名
uv run python -c 'import json, pathlib; [json.loads(line) for line in pathlib.Path("sar_orch/results/<run_id>/map_summary.jsonl").read_text().splitlines() if line]; print("map_summary.jsonl: valid")'
grep 'MapSummarizer' sar_orch/results/<run_id>/token_usage.csv
grep -R 'Map Changes\|Map Summary' sar_orch/results/<run_id>/coordinator/
```

smoke 需要同时检查：同一步 A2A observation 能改变 map revision、`map_summary.jsonl` 位于 run 根目录、token row 有正确 RunID/Model、以及 coordinator 未因 summary timeout/错误停机。

---

## 验收标准

1. 所有生产地图 import 使用 `sar_orch.map`；legacy shim 的公开 class 与新 API 相同。
2. 任意同一步新 observation 都能通过 revision 让下一轮 coordinator pre-LLM 看到新的 map delta。
3. 旧 observation 不会覆盖新位置；agent 实时位置仍来自 barrier。
4. `snapshot()` 无 `await`、无 LLM 调用、无文件写入；所有 summary I/O 只在异步 prepare 阶段发生。
5. 每个 map revision 至多发起一次总结；timeout/异常不阻断 coordinator，且保留上次成功 summary。
6. summary 输入和 Context Memory 都有确定的条目/字符上限；正常 agent 移动不会提高 summary 调用频率。
7. `map_summary.jsonl`、`token_usage.csv`、`summary.csv` 均包含可追溯的总结记录，且不会破坏已有 run 目录结构。
8. 上述单测、ruff 和 20-step smoke run 全部通过。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| revision 过度递增导致频繁 prepare | `update_step_budget()` 只在值变化时递增；summarizer 仅对非空 semantic delta 触发。 |
| LLM summary 造成 coordinator 延迟 | 仅高价值/周期性触发，使用 5 秒 deadline、single-flight 和失败回退。 |
| 原始 snapshot 过大导致 token 成本高 | 只向 summarizer 传 deterministic compact projection；Context delta 最多渲染 5 条。 |
| 同一步异步 observation 与缓存竞争 | 在 store lock 内返回 revision+snapshot；provider 以 map revision 而非仅 env step 失效缓存。 |
| shim 改变旧模块 API | 显式 `__all__` 与 legacy-import tests。 |
| Summary token 未及时写入 aggregate | 成功记录后显式调用 `ExperimentLogger.flush_summary()`。 |

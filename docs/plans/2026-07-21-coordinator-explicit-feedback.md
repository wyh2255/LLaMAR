# Coordinator 显式反馈设计（Explicit Task Feedback）实施计划

> 日期: 2026-07-21
> 分支: feat/send-message-facade
> 状态: REVISED — 根据审核意见修订，待实施

---

## 1. 问题陈述

### 1.1 当前反馈链路

```
Coordinator LLM
  ↓ send_message(message_type="assign_task") 
  ↓ 同步返回 ToolResult(success=True) — 仅表示"消息已发出"
  ↓ (异步) A2A 推送 → Worker 执行
  ↓ Worker push callback → EventStore.append() + SemanticMapStore.ingest_observation()
  ↓ 下一轮 LLM 调用前
  ↓ SARCoordinatorStateProvider.snapshot()
  ↓ CoordinatorContextManager._project_runtime_state_to_pinned()
  ↓ 渲染进 Context Memory (pinned state)
```

### 1.2 核心问题

| # | 问题 | 证据 |
|---|------|------|
| P1 | **工具返回无任务反馈** | `SendMessageTool._handle_assign_task` 成功路径返回 `ToolResult(success=True, content="dispatched...")`，只证明消息入队，不包含任务后续进展 |
| P2 | **反馈完全依赖隐式推理** | Coordinator 必须从 `semantic_summary` + `map_delta` + `recent_changes` 三个间接信号推断"我的任务是否有效" |
| P3 | **语义地图无历史** | `SemanticMapStore` 只保存当前快照；`map_delta` 虽计算了 revision 间差异，但它是"环境变化"视角，不是"任务效果"视角 |
| P4 | **任务卡住难以识别** | Coordinator 无法区分"任务正常运行中" vs "Worker 卡死/循环"，因为缺少 `steps_elapsed` 这种显式时间维度 |
| P5 | **任务结果无摘要** | Worker `finish_task` 后的结果文本只能通过 `EventStore.get_task_state()` 主动查询，不会自动进入 Context |

### 1.3 影响

从 `docs/project_notes/bugs.md` 与 `references/coordinator-task-management-chaos.md` 已知，3-agent 场景下 Coordinator 出现 task thrashing（重复 dispatch 同一 agent、cancel 不存在的 task），根因之一就是**反馈太隐式导致 Coordinator 误判任务状态**。

---

## 2. 设计目标

| 目标 | 说明 |
|------|------|
| G1 | **显式性** — Coordinator 不需要从地图变化"推理"任务状态，而是直接看到结构化的任务生命周期 |
| G2 | **时效性** — 每个反馈条目携带 step 时间戳，Coordinator 可以判断"这个任务跑了多久没动静" |
| G3 | **因果性** — 任务完成时显式关联其对语义地图的影响（"task-001 导致 CaldorFire_1 intensity high→none"） |
| G4 | **确定性** — 框架层（state provider + context render）实现，不依赖 LLM 是否记得调用查询工具 |
| G5 | **低开销** — 反馈注入是 cheap 的内存读取，不引入额外 LLM 调用或网络请求 |

---

## 3. 方案概览

采用**三层递进**设计，Phase 1 是核心（必须），Phase 2/3 是增强（按效果决定是否需要）：

| Phase | 层 | 内容 | 预期收益 |
|-------|-----|------|---------|
| **1** | 框架层 | TaskFeedback 结构化注入 — 新增 `task_feedback` 字段到 `CoordinatorPinnedState`，每轮渲染任务状态、耗时、结果摘要 | 解决 P1/P2/P4/P5 |
| **2** | 框架层 | Task-Map 因果关联 — Worker `finish_task` 时上报 `affected_objects`，Context 渲染"任务→地图变化"映射 | 解决 P3 |
| **3** | Prompt 层 | 反馈协议说明 — 在 Coordinator system prompt 中显式解释各反馈字段含义（兜底，防止 Phase 1+2 仍被误读） | 提升 LLM 对反馈的理解 |

---

## 4. Phase 1 — TaskFeedback 结构化注入

### 4.1 前置改动：PhysicalDispatch 新增字段

**审核发现**：`PhysicalDispatch` (mission_runtime.py:110-124) 当前只有 `dispatch_id, context_id, logical_node_id, worker_id, worker_task_id, state, artifact, result, finalization_seq, created_at`，**缺少 `dispatched_at_step` 和 `objective`**。

需修改 `src/a2a/coordinator/mission_runtime.py`：

```python
@dataclass
class PhysicalDispatch:
    dispatch_id: str
    context_id: str
    logical_node_id: str
    worker_id: str
    worker_task_id: str | None = None
    state: PhysicalState = PhysicalState.PREPARED
    artifact: str | None = None
    result: Any | None = None
    finalization_seq: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    dispatched_at_step: int = 0  # NEW: dispatch 时的环境 step
```

`TaskStore.create_physical_dispatch` (task_store.py:232-241) 需接收 `env_step` 参数：

```python
def create_physical_dispatch(
    self, logical_id: str, worker_id: str, env_step: int = 0
) -> PhysicalDispatch | None:
    if self._runtime is None:
        return None
    dispatch = self._runtime.create_dispatch(logical_id, worker_id)
    dispatch.dispatched_at_step = env_step  # NEW
    # ... rest unchanged
```

调用点（`SendMessageTool._handle_assign_task` / `_handle_runtime_assign_task`）需传入当前 env_step（从 barrier 读取）。

### 4.2 数据模型

在 `src/Agent/router_agent/context.py` 中新增 Pydantic 模型：

```python
class TaskFeedbackEntry(BaseModel):
    """Single task lifecycle feedback entry."""
    task_id: str                    # dispatch_id (coordinator 侧)
    worker_task_id: str = ""        # worker 侧 task_id
    worker_id: str
    objective_preview: str = ""     # 任务目标摘要 (<= 80 chars)
    dispatched_at_step: int = 0     # dispatch 时的 env_step
    current_state: str = "UNKNOWN"  # DISPATCHED | RUNNING | COMPLETED | FAILED | CANCELED | INPUT_REQUIRED
    last_update_ts: float = 0.0     # 最后一次状态变化的 Unix timestamp
    steps_elapsed: int = 0          # env_step - dispatched_at_step
    result_preview: str = ""        # 完成/失败时的文本摘要 (<= 120 chars)
    is_stale: bool = False          # steps_elapsed > stale_threshold 且 state == RUNNING

class CoordinatorPinnedState(BaseModel):
    # ... 现有字段 ...
    task_feedback: list[TaskFeedbackEntry] = Field(default_factory=list)
    feedback_stale_threshold: int = 5  # 超过 N 步无更新视为 stale
```

### 4.3 State Provider 构建

在 `sar_orch/coordinator_state_provider.py` 新增 `_build_task_feedback()`:

```python
def _build_task_feedback(self, env_step: int) -> list[dict[str, Any]]:
    """Build structured per-task feedback from MissionRuntime + EventStore.
    
    Sources:
    - MissionRuntime.dispatches: PhysicalDispatch records (worker_id, state, result, dispatched_at_step)
    - EventStore.get_task_state(): latest state + result text + updated_at
    - MissionGraph nodes: objective text (via logical_node_id lookup)
    
    Cheap: in-memory reads only, no I/O.
    """
    if self._runtime is None:
        return []
    
    feedback = []
    for dispatch_id, dispatch in self._runtime.dispatches.items():
        # Query EventStore for latest state
        state_info = {"state": "UNKNOWN", "text": "", "updated_at": 0.0}
        if self._event_store is not None:
            state_info = self._event_store.get_task_state(dispatch_id, dispatch.worker_id)
        
        # Resolve objective from MissionGraph node if available
        objective = ""
        if dispatch.logical_node_id and self._mission_graph:
            node = self._mission_graph.get_node(dispatch.logical_node_id)
            if node:
                objective = getattr(node, "objective", "") or ""
        
        steps_elapsed = max(0, env_step - dispatch.dispatched_at_step)
        current_state = state_info["state"]
        
        entry = {
            "task_id": dispatch_id,
            "worker_task_id": dispatch.worker_task_id or "",
            "worker_id": dispatch.worker_id,
            "objective_preview": objective[:80],
            "dispatched_at_step": dispatch.dispatched_at_step,
            "current_state": current_state,
            "last_update_ts": state_info.get("updated_at", 0.0),
            "steps_elapsed": steps_elapsed,
            "result_preview": (state_info.get("text") or "")[:120],
            "is_stale": steps_elapsed > 5 and current_state == "RUNNING",
        }
        feedback.append(entry)
    
    # Sort: stale first, then by steps_elapsed desc
    feedback.sort(key=lambda e: (not e["is_stale"], -e["steps_elapsed"]))
    return feedback[:10]  # Cap at 10 entries to bound context size
```

**挂载点**：在 `snapshot()` 方法中，将 `payload["task_feedback"] = self._build_task_feedback(env_step)` 加入 payload。

**同步修改** `_project_runtime_state_to_pinned()`：将 `"task_feedback"` 加入 key 列表。

### 4.4 Context 渲染

在 `CoordinatorContextManager` 新增 `_render_task_feedback()`:

```python
def _render_task_feedback(self) -> str:
    ps = self._pinned_state
    if not isinstance(ps, CoordinatorPinnedState):
        return ""
    if not ps.task_feedback:
        return ""
    
    lines = ["### Task Feedback"]
    for entry in ps.task_feedback:
        state = entry.current_state
        stale_marker = " ⚠️ STALE" if entry.is_stale else ""
        lines.append(
            f"- {entry.task_id} → {entry.worker_id}: {state}{stale_marker} "
            f"(step {entry.dispatched_at_step}, elapsed {entry.steps_elapsed})"
        )
        if entry.objective_preview:
            lines.append(f"  objective: {entry.objective_preview}")
        if entry.result_preview and state in ("COMPLETED", "FAILED"):
            lines.append(f"  result: {entry.result_preview}")
    return "\n".join(lines)
```

**插入位置**：在 `_render_current_state()` 中，放在 `### Physical Dispatches` 之后、`### Map Summary` 之前。

**state_digest 更新**：在 `_render_current_state` 的 digest 构建中，添加 `len(ps.task_feedback)` 确保任务反馈变化触发状态刷新：

```python
if ps.task_feedback:
    lines.append(f"### Task Feedback")
    state_digest.append(len(ps.task_feedback))
    # ... render entries
```

### 4.5 文件改动清单

| 文件 | 改动 | 行数估计 |
|------|------|---------|
| `src/a2a/coordinator/mission_runtime.py` | `PhysicalDispatch` 添加 `dispatched_at_step: int = 0` | +1 |
| `src/a2a/coordinator/task_store.py` | `create_physical_dispatch` 添加 `env_step` 参数并注入 | +3 |
| `src/a2a/builtin_tools/send_message.py` | 调用 `create_physical_dispatch` 时传入当前 env_step | +2 |
| `src/Agent/router_agent/context.py` | 新增 `TaskFeedbackEntry` 模型；`CoordinatorPinnedState` 添加 `task_feedback` + `feedback_stale_threshold`；新增 `_render_task_feedback()`；`_render_current_state()` 中插入调用 + digest | +70 |
| `sar_orch/coordinator_state_provider.py` | 新增 `_build_task_feedback()`；`snapshot()` 中添加 payload key；`_project_runtime_state_to_pinned()` 添加 key | +55 |

### 4.6 验证方案

**单元测试**（新增 `tests/test_task_feedback.py`）：
- `test_task_feedback_empty_when_no_dispatches`
- `test_task_feedback_state_mapping` — EventStore 状态正确映射到 feedback entry
- `test_task_feedback_stale_detection` — `steps_elapsed > threshold` 时 `is_stale=True`
- `test_task_feedback_caps_at_10_entries`
- `test_physical_dispatch_serialization` — 新增 `dispatched_at_step` 字段 JSON 序列化/反序列化正确

**集成验证**（按用户偏好 5 scenes × 2 agent counts = 10 组）：
```bash
# max_steps=20, seed=42, scene ∈ {1..5}, agents ∈ {2, 3}
# 收集指标: coverage, transport_rate, error_counts
# 特别关注: task_feedback 是否出现在 <task>.ndjson 的 context 快照中
```

**通过标准**：
- 所有 10 组实验 `worker_busy` / `unknown_task_id` 错误数为 0
- 随机抽查 3 个 `<task>.ndjson`，确认 `### Task Feedback` 区块存在且内容正确
- `steps_elapsed` 随 env step 递增

---

## 5. Phase 2 — Task-Map 因果关联

### 5.1 设计

当 Worker `finish_task` 时，上报该任务影响的对象列表。Coordinator 收到后，在下一次 `map_delta` 中关联这些对象的变化。

**Worker 侧改动**（`sar_orch/tools/worker/finish_task.py` 或等价位置）：

```python
# finish_task 工具新增可选参数
{
    "affected_objects": ["CaldorFire_Region_1", "Timmy"],  # 任务直接影响的对象 ID
    "outcome": "extinguished"  # extinguished | rescued | supplied | explored
}
```

**Coordinator 侧改动**：

`SARCoordinatorStateProvider._build_task_feedback()` 中，对 COMPLETED 任务：

```python
if state_info["state"] == "COMPLETED":
    affected = state_info.get("affected_objects", [])
    # 从最近一次 map_delta 中提取这些对象的变化
    if self._last_map_delta and affected:
        effects = self._extract_effects_for_objects(affected, self._last_map_delta)
        entry["map_effects"] = effects  # e.g. ["CaldorFire_Region_1: high→none"]
```

**渲染**：
```
- task-001 → Alice: COMPLETED (step 8, elapsed 4)
  objective: Extinguish CaldorFire region 1
  result: Used 2 sand, fire extinguished
  effects: CaldorFire_Region_1 intensity high → none ✓
```

### 5.2 依赖

- Phase 1 已完成
- Worker `finish_task` 工具支持 `affected_objects` 参数（可能涉及 prompt 更新让 Worker LLM 学会填写）

### 5.3 风险

- Worker LLM 可能不填或乱填 `affected_objects`，导致关联错误。兜底：如果 `affected_objects` 为空，回退到启发式匹配（任务目标文本中出现的对象名）。

---

## 6. Phase 3 — Prompt 反馈协议（可选）

在 `prompts/coordinator/system.semantic.md` 末尾追加：

```markdown
## Task Feedback Interpretation

Your Context Memory includes a `### Task Feedback` section showing the 
lifecycle of every task you dispatched. Key fields:

- **current_state**: DISPATCHED (sent, not started) → RUNNING → COMPLETED / FAILED
- **steps_elapsed**: How many env steps since you dispatched this task
- **is_stale**: True if RUNNING for > 5 steps with no updates — consider 
  cancelling or querying the worker
- **result**: Worker's completion summary (for COMPLETED/FAILED tasks)
- **effects**: Map changes caused by this task (for COMPLETED tasks)

Decision rules:
- If a task is STALE, do NOT dispatch a duplicate. Either cancel it or 
  send a status query.
- If a task is COMPLETED with expected effects, mark the corresponding 
  mission objective as achieved.
- If a task FAILED, read the result text to decide: retry, reassign, or 
  change strategy.
```

**仅在 Phase 1+2 实验数据显示 Coordinator 仍误读反馈时实施。**

---

## 7. 实施工作流

按用户多 phase 偏好：

| Phase | Subagent | 内容 | 验证 | 提交 |
|-------|----------|------|------|------|
| 1a | TBD | 4.1 PhysicalDispatch 新增字段 + 4.2 数据模型 + 4.3 State Provider 基础字段 | 4.6 单元测试 | `feat(coordinator): Phase 1a — task feedback data model` |
| 1b | TBD | 4.4 Context 渲染 + 4.5 集成 + objective_preview 来源 | 4.6 单元测试 + 10 组交叉实验 | `feat(coordinator): Phase 1b — task feedback rendering` |
| 2 | TBD | 5.1-5.2 代码改动 | 同 Phase 1 验证 + effects 字段正确性抽查 | `feat(coordinator): Phase 2 — task-map causal linking` |
| 3 | TBD | 6. prompt 更新 | 仅当 Phase 1+2 不足时 | `docs(coordinator): Phase 3 — feedback protocol prompt` |

每个 phase 完成后：
1. 父 Agent 重读 diff，独立运行测试
2. 更新本计划的跟踪表
3. 提交代码 + 计划更新

---

## 8. 已知边界与风险

| 风险 | 缓解 |
|------|------|
| `PhysicalDispatch` 新增字段需同步 JSON 序列化/反序列化 | Phase 1a 单元测试覆盖 `_persist()` 和 `_restore()` 路径 |
| `objective_preview` 来源依赖 MissionGraph 或 EventStore，可能为空 | 允许为空字符串，渲染时跳过 |
| EventStore.get_task_state() 是 O(events)，频繁调用可能有性能开销 | 每 step 只调用一次（在 snapshot() 中），且 events 有 max_events_per_task=500 上限 |
| task_feedback 增加 context token 消耗 | 上限 10 条，每条 ~60 tokens，总计 ~600 tokens，在 80k token_limit 下可接受 |
| Worker 多任务并发时反馈列表过长 | sort 中 stale 优先 + cap 10，确保最重要的信息不被截断 |
| `dispatched_at_step` 需要从 barrier 读取当前 step，但 dispatch 发生在 step 中间 | 使用 dispatch 时的 `_step_counter` 快照，允许 ±1 误差 |

---

## 9. 审核记录

| 日期 | 审核人 | 结论 | 关键发现 |
|------|--------|------|---------|
| 2026-07-21 | 自审核（主会话） | APPROVE_WITH_COMMENTS | PhysicalDispatch 缺少 `dispatched_at_step` 和 `objective` 字段；EventStore.get_task_state() 返回结构确认；建议拆分 Phase 1a/1b |

---

## 10. 备选方案（被否决）

| 方案 | 否决原因 |
|------|---------|
| 让 `send_message` 同步阻塞等待 Worker 完成 | 破坏异步并行设计，Worker 执行期间 Coordinator 无法做其他决策 |
| Coordinator 每轮主动调用 `query_task_status` 工具 | 依赖 LLM 记得调用，不确定性高；且增加 LLM 调用次数 |
| 在 SemanticMapStore 中保存完整历史快照 | 内存开销大，且 Coordinator 真正需要的是"任务-效果"关联，不是"地图历史" |
| 复用现有 `physical_dispatches_view` 而不新增字段 | `physical_dispatches_view` 缺少 `steps_elapsed`、`is_stale`、`result_preview` 等决策关键字段，渲染格式也不是面向"反馈"语义；扩展它需改 schema + 渲染 + digest，工作量相当但语义清晰度更差 |

---

## 11. 事件驱动推理（Event-Driven Inference）扩展议题

> 用户新增需求：当前每个 step 允许 agent 进行多次推理，对已派发任务的 Coordinator 而言，无新信息时的重复推理是浪费。希望实现**只有当特定事件发生时，Coordinator 才进行下一轮推理**。

### 11.1 触发事件清单

| 事件类型 | 来源 | 说明 |
|---------|------|------|
| 任务终态 | EventStore | 任务 COMPLETED / FAILED / CANCELED |
| 超时警告 | TaskWatchdog | TASK_STALE / TASK_DEADLINE_WARNING / TASK_DEADLINE_EXCEEDED |
| Worker 中断询问 | EventStore | INPUT_REQUIRED (help_request) |
| 环境突变 | SemanticMapStore / MapSummarizer | map_delta 触发 summarizer 的特定条件（fire_change, person_change, conflict, stale） |
| Worker 上报 | EventStore | observation_report / artifact_update |
| 用户请求 | 外部输入 | 用户通过 UI / API 提交新任务或查询 |
| 固定程序检查 | 自定义检查器 | 如机器人电量不足、资源阈值告警等（需新增检查器框架） |

### 11.2 架构影响

当前 Coordinator 的推理循环是**轮询驱动**（poll-driven）：
```
while not mission_finished:
    refresh_runtime_state()  # 每轮都刷新
    if state_changed:
        llm_generate()
```

事件驱动模型改为**回调驱动**（callback-driven）：
```
# Coordinator 主循环挂起，等待事件
event = await event_queue.get()
if event.triggers_inference:
    refresh_runtime_state()
    llm_generate()
```

### 11.3 与现有机制的兼容性

| 现有机制 | 兼容性 | 说明 |
|---------|--------|------|
| `AsyncStatePreparer.prepare_for_llm()` | 需调整 | 当前在每次 LLM 调用前执行；事件驱动后只在事件触发时执行 |
| `ContextManager.refresh_runtime_state()` | 兼容 | 仍负责状态刷新，但调用频率从"每轮"降为"事件触发时" |
| `TaskWatchdog` | 天然兼容 | 已是事件源（产生 supervision alerts），可直接接入事件队列 |
| `MapSummarizer` | 需调整 | 当前由 `prepare_for_llm` 调用；事件驱动后可由 map_delta 事件直接触发 |
| `SARBarrier` threading 模型 | 需注意 | ADR-011：barrier 使用 threading.Event，事件队列需用 `queue.Queue` 或 `asyncio.Queue` 跨线程传递 |

### 11.4 实施建议

事件驱动推理是**架构级改动**，建议作为独立 Phase 4（或并行项目）实施，不与 Phase 1-3 耦合：

```
Phase 4a: 事件总线框架
  - 新增 CoordinatorEventQueue (threading-safe)
  - 事件类型枚举 + 优先级
  - EventStore / TaskWatchdog / SemanticMapStore 接入事件发布

Phase 4b: 推理循环改造
  - CoordinatorAgentExecutor 主循环从轮询改为事件等待
  - 保持现有 state_provider 机制不变，仅改变触发时机

Phase 4c: 固定程序检查器
  - 可插拔检查器接口（如 battery_checker, resource_checker）
  - 检查器作为事件源接入事件总线
```

**当前计划（Phase 1-3）先解决反馈显式化问题，事件驱动可作为后续优化。**

---

*修订完成，待进入 Phase 1a 实施。*

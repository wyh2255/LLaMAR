---
日期: 2026-07-04
文档类型: 系统架构文档
文档概述: ContextManager 三层记忆系统设计（Pinned State + Episodic Memory + Recent Window），
  涵盖 ContextConfig 策略、Memory Block 渲染、Snapshot 持久化及子类实现
---

# ContextManager 三层记忆系统

## 1. 概述

ContextManager 位于 `src/Agent/router_agent/context.py` 和 `src/Agent/worker_agent/context.py`，提供 **三层递进记忆模型**，在每次 LLM 调用前组装 memory block 注入 system prompt：

```
┌──────────────────────────────────────────────────┐
│  System Prompt                                   │
├──────────────────────────────────────────────────┤
│  Context Memory (assembled by ContextManager)     │
│  ┌─ Environment Layer ─────────────────────────┐  │
│  │  宏观场景信息（fires / persons / agents）    │  │
│  ├─ Current State Layer ───────────────────────┤  │
│  │  自身状态（position / inventory / step）     │  │
│  ├─ Action History Layer ──────────────────────┤  │
│  │  最近 N 步操作记录（episode summaries）      │  │
│  └─────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────┤
│  Raw Messages (最近 K 条 assistant/tool 消息)     │
└──────────────────────────────────────────────────┘
```

## 2. ContextConfig

```python
@dataclass
class ContextConfig:
    strategy: str = "hybrid"
    # "none"     — 不 prune，但 observe() 正常执行（pinned + episodic 仍然更新）
    # "summary"  — 只做 episodic summary，pinned 提取受 `pinned_enabled` 控制（与 hybrid 行为相同）
    # "hybrid"   — pinned + episodic + recent window（默认）
    # "raw"      — 透传模式：跳过 observe/prune/assemble 全部处理
    state_mode: str = "semantic"         # "semantic" | "oracle" — 决定 pinned state 的 Environment 层渲染策略（仅 router 副本支持）
    recent_messages: int = 12            # 保留最近 N 条 assistant/tool 消息
    summary_trigger_ratio: float = 0.8   # token_limit 的 80% 触发 summary
    pinned_enabled: bool = True          # 是否启用 pinned state 提取
    episodic_max_items: int = 20         # episodic 最大条目数
```

> **注意**：`state_mode` 字段仅存在于 router 端的 `ContextConfig`（`src/Agent/router_agent/context.py`），Worker 端的副本（`src/Agent/worker_agent/context.py`）没有此字段。

### 2.1 Strategy 对比

| Strategy | observe() | prune_history() | assemble() |
|----------|-----------|-----------------|------------|
| `none`   | 正常执行   | 跳过            | 注入 memory block |
| `summary`| 正常执行   | 正常 prune      | 注入 memory block |
| `hybrid` | 正常执行   | 正常 prune      | 注入 memory block |
| `raw`    | 立即返回   | 跳过            | 透传 system + raw messages |

## 3. ContextManager 基类

### 3.1 数据结构

```python
class ContextManager:
    pinned: dict[str, Any]                    # 旧式 dict pinned state（向后兼容）
    _pinned_state: BaseModel | None            # 新式 typed pinned state
    episodic: list[_Episode]                   # 总结化的历史操作
    _task_snapshots: dict[str, tuple[list, dict | None]]  # 内存 snapshot
    _log_dir: Path | None                      # 磁盘 snapshot 目录
```

### 3.2 observe() — 工具结果处理

每次 Agent 执行完工具后调用：

```python
def observe(self, tool_name: str, content: str, success: bool, state_mode: str | None = None):
    if self.config.strategy == "raw":
        return                               # raw 模式跳过

    if self.config.pinned_enabled:
        extracted = self._extract_pinned(tool_name, content, success)
        if extracted:
            self.pinned.update(extracted)     # 更新 dict
            if self._pinned_state:            # 更新 typed state
                for k, v in extracted.items():
                    if hasattr(self._pinned_state, k):
                        setattr(self._pinned_state, k, v)

    # 记录 episode
    status = "success" if success else "failure"
    truncated = content[:500] + ("..." if len(content) > 500 else "")
    self.episodic.append(_Episode(
        step=self._episode_counter,
        tool_name=tool_name,
        summary=f"{tool_name} → {status}: {truncated}",
    ))
```

### 3.3 prune_history() — 消息剪枝

从 message list 中移除较早的 assistant/tool 消息，被移除的 assistant 消息自动转为 episodic 条目：

```python
# 提取所有 exec 索引（assistant/tool），但跳过 system 消息（索引 0）
exec_indices = [i for i, m in enumerate(messages)
                if m.role in ("assistant", "tool") and i > 0]
to_remove = set(exec_indices[:-recent])       # 保留最近 K 条
# 如果 assistant 被剪枝，其后紧跟的孤儿 tool 消息也一并移除
remaining = [i for i in exec_indices if i not in to_remove]
orphaned = set()
for i in to_remove:
    if messages[i].role == "assistant":
        # 移除紧随其后的 tool 消息
        for j in exec_indices:
            if j > i and messages[j].role == "tool" and j not in remaining:
                orphaned.add(j)
to_remove |= orphaned
# 被移除的 assistant 消息 → 追加到 episodic
# 被移除的 tool 消息 → 跳过（已在 observe 中记录）
messages[:] = [msg for i, msg in enumerate(messages)
               if i not in to_remove]
```

### 3.4 assemble() — 最终消息组装

```python
def assemble(self, system_prompt, messages):
    if self.config.strategy == "raw":
        return [Message(role="system", content=system_prompt)] + messages[1:]  # 透传

    result = [Message(role="system", content=system_prompt)]
    result.extend(messages[1:])               # 原始消息优先（DeepSeek prefix caching）
    memory_text = self._render_memory_block() # 三层 memory block
    if memory_text:
        result.append(Message(role="user", content=memory_text))  # memory block 在末尾
    return result
```

## 4. 三层 Memory Block 渲染

### 4.1 _render_memory_block()

```python
def _render_memory_block(self) -> str:
    parts = []
    env = self._render_environment_view()
    if env:
        parts.append(f"### Environment\n{env}")
    state = self._render_current_state()
    if state:
        parts.append(f"### Current State\n{state}")
    if self.episodic:
        lines = [f"- step {e.step}: {e.summary}" for e in self.episodic[-10:]]
        parts.append(f"### Action History (last {len(lines)} steps)\n" + "\n".join(lines))
    # Router 端追加 EventStore 摘要（如 help_request）
    pending = self._render_pending_worker_events()
    if pending:
        parts.append(f"### Pending Worker Events\n{pending}")
    return "\n---\n".join(parts)

def _render_pending_worker_events(self) -> str:
    # Router 端覆写：从 EventStore 读取 help_request 事件
    return ""
```

输出示例：
```
---
## Context Memory
---
### Environment            ← _render_environment_view()
Active workers: 3
Total fires: 12
Persons: 5 total, 2 rescued
---
### Current State          ← _render_current_state()
Step: 5 / 20 (remaining: 15)
Mission finished: False
Dispatched: 3 tasks
---
### Action History (last N steps)  ← self.episodic[-10:]
- step 1: navigate_to → success: Arrived at (3,2,0)
- step 2: extinguish_fire → success: Fire extinguished
---
### Pending Worker Events  ← router 端追加 EventStore 摘要（如 help_request）
---
```

### 4.2 可覆写方法

父类定义两个空方法，子类覆写以提供领域特定内容：

```python
def _render_environment_view(self) -> str:   # 环境层 → 场景宏观信息
    return ""

def _render_current_state(self) -> str:       # 状态层 → 自身位置/库存/步数
    if not self._pinned_state:
        if self.config.pinned_enabled and self.pinned:
            return "\n".join(f"- {k}: {v}" for k, v in self.pinned.items()
                             if v is not None)
    return ""
```

## 5. Typed Pinned State（BaseModel）

### 5.1 CoordinatorPinnedState

**Router 端（完整版）：**

```python
class CoordinatorPinnedState(BaseModel):
    version: int = 1                          # schema 版本
    global_snapshot: dict = {}                # 全场景快照（agents/fires/persons）
    semantic_summary: dict = {}               # 语义地图摘要（semantic 模式）
    team_status_summary: dict = {}            # 团队状态摘要（semantic 模式）
    state_mode: str = "semantic"              # "semantic" | "oracle"
    step_budget: dict = {                     # 步数预算
        "current_step": 0,
        "max_steps": 0,
        "remaining": 0,
    }
    mission_finished: bool = False
    dispatched_tasks: list[dict] = []         # 已分发的任务
    worker_results: list[dict] = []           # 收集到的结果
```

**Worker 端（精简版）：** 由于 Worker 不执行 coordinator 逻辑，其副本缺少 `semantic_summary`、`team_status_summary`、`state_mode` 三个字段。

### 5.2 WorkerPinnedState

```python
class WorkerPinnedState(BaseModel):
    version: int = 1
    position: tuple[int, int, int] | None = None   # 当前位置
    inventory: list[str] = []                       # 库存
    step: int = 0                                   # 当前步数
    known_fires: list[dict] = []                    # 已发现的火点
    known_persons: list[dict] = []                  # 已发现的人员
    mission_status: str = "in_progress"             # 任务状态
```

### 5.3 双写策略

```
_extract_pinned() → dict
    → self.pinned.update(extracted)          # dict 写入（向后兼容）
    → self._pinned_state 逐个字段 setattr    # BaseModel 写入（类型安全）
```

## 6. Snapshot 持久化

### 6.1 保存

```python
def save_snapshot(self, task_id, messages):
    pinned_data = self._snapshot_pinned_data()    # 序列化 pinned
    self._task_snapshots[task_id] = (deepcopy(messages), pinned_data)  # 内存
    path = self._log_dir / f"snapshot_{task_id}.json"
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pinned": pinned_data, "messages": [...]}, f)   # 磁盘
```

_snapshot_pinned_data() 优先级：`_pinned_state.model_dump()` > `dict(self.pinned)` > `None`

### 6.2 加载

```python
def load_snapshot(self, task_id):
    # ① 优先查内存
    if task_id in self._task_snapshots:
        return msgs
    # ② 回退到磁盘
    path = self._log_dir / f"snapshot_{task_id}.json"
    if path exists:
        payload = json.load(path)
        os.remove(path)                           # 用完即删
        if isinstance(payload, dict):
            self._restore_pinned_data(payload["pinned"])
            return [Message.model_validate(m) for m in payload["messages"]]
        # 兼容旧格式：payload 本身是 message list
        return [Message.model_validate(m) for m in payload]
```

_restore_pinned_data() 优先级：BaseModel.model_validate() > dict update > 静默失败

### 6.3 路径规则

```
{log_dir}/snapshot_{task_id}.json
```

## 7. 子类实现

### 7.1 CoordinatorContextManager

| 覆写方法 | 数据来源 | 输出内容 |
|----------|----------|----------|
| `_render_environment_view()` | `global_snapshot` + `semantic_summary` + `team_status_summary` | Active workers, Total fires, Persons rescued (受 `state_mode` 影响) |
| `_render_current_state()` | `step_budget` / `mission_finished` / `dispatched_tasks` | Step N/M, Mission status, Recent dispatches |
| `_extract_pinned()` | `query_semantic_map` / `query_team_status` / `query_sar_state` / `dispatch_task` / `collect_results` / `finish_task` | 提取语义摘要、全局快照、步数预算、已完成标记 |

### 7.2 WorkerContextManager

Worker 的线程模式与 Router 不同，`_render_environment_view()` 仅使用 `global_snapshot` 字段，没有 `state_mode` 切换逻辑。`CoordinatorContextManager` 在 Worker 端也有精简副本。

| 覆写方法 | 数据来源 | 输出内容 |
|----------|----------|----------|
| `_render_environment_view()` | `known_fires` / `known_persons` (Worker) 或 `global_snapshot` (Worker 端的 CoordinatorCtx) | 已知火点和人员 |
| `_render_current_state()` | `position` / `inventory` / `step` / `mission_status` | 位置、库存、步数、任务状态 |
| `_extract_pinned()` | 正则提取位置/库存/步数 + JSON 解析 + `no_op` 特殊处理 | (x,y,z) / 库存列表 / mission complete |

## 8. 与 AgentController 的集成

```
AgentController.submit()
    → session_factory() → ContextManager 实例
        → agent.attach_context(ctx)          # 注入观察钩子
            → agent.run()
                → post_tool() → ctx.observe(tool_name, result, success)
                → pre_llm() → ctx.prune_history(messages)
                → ctx.assemble(prompt, messages) → LLM
```

### 8.1 构建链中的 log_dir 传递

```
RouterBuildOptions.log_dir  ─→  build_router_agent()
    └→ Agent(log_dir=...)   ─→  AgentController 内部创建 ContextManager
RouterControllerBuildOptions  ─→  build_router_controller()
    └→ session_factory()    ─→  CoordinatorContextManager(..., log_dir)
```

## 9. 文件位置

| 文件 | 包含 | 备注 |
|------|------|------|
| `src/Agent/router_agent/context.py` | ContextConfig, ContextManager, CoordinatorPinnedState, CoordinatorContextManager | Coordinator 端 |
| `src/Agent/worker_agent/context.py` | ContextConfig, ContextManager, WorkerPinnedState, WorkerContextManager, (冗余: CoordinatorPinnedState, CoordinatorContextManager) | Worker 端 + 跨包依赖；注意: Worker 的 `ContextConfig` 缺少 `state_mode` 字段，`CoordinatorPinnedState` 副本缺少 `semantic_summary`/`team_status_summary`/`state_mode` 三个字段 |
| `src/Agent/router_agent/build.py` | build_router_agent, build_router_controller, _tool_descriptions_text | 组装入口 |
| `src/Agent/worker_agent/build.py` | build_agent, build_controller, _tool_descriptions_text | 组装入口 |

---
日期: 2026-07-26
文档类型: 系统架构文档
文档概述: ContextManager 三层记忆系统设计（Pinned State + Episodic Memory + Recent Window），
  涵盖 ContextConfig 策略、Memory Block 渲染、Snapshot 持久化及子类实现
---

# ContextManager 三层记忆系统

## 1. 概述

ContextManager 位于 `src/Agent/router_agent/context.py` 和 `src/Agent/worker_agent/context.py`，提供 **三层递进记忆模型**，在每次 LLM 调用前组装 Environment State 块（role=user 尾部 state block），并把输出契约并入稳定 system prompt：

```
system prompt (稳定前缀)
  ├─ 稳定规则（Environment / Step Mechanics / Strategy ...）
  ├─ 工具契约
  └─ Output / Response Contract      ← _render_output_schema()（永不进入 user state block）

conversation history
  └─ 原始 user / assistant / tool 消息

role=user（每次 pre_llm 追加的尾部 state block）:
  ## Environment State              ← _render_memory_block()
  ├─ Environment Layer（宏观场景信息：fires / persons / agents）
  ├─ Current State Layer（自身状态：position / inventory / step）
  └─ Task Plan & Progress Layer（任务状态投影）
```

> `assemble()` 在追加 Environment State 前会验证 history 中不存在未闭合的 assistant tool call；不满足时拒绝本轮追加，由现有 controller/NeedInput 回填路径闭合协议。
>
> **与 canonical Memory 的关系（2026-08-13 对齐）**：本文件讲 ContextManager 三层窗口（pinned / episodic / recent）。官方世界事实读路径是 canonical Memory `read_port`（`docs/system_docs/memory.md`）。`long_term_mode=read` 时 coordinator 视图另有预算内 `### Long-term Memory` 段；worker 永不注入。本文件日期 2026-07-26，其余字段默认仍与代码一致；读路径默认以 memory.md / 代码为准。

## 2. ContextConfig

```python
@dataclass
class ContextConfig:
    strategy: str = "hybrid"
    # "none"     — 不 prune，但 observe() 正常执行（pinned + episodic 仍然更新）
    # "summary"  — 只保留 pinned + episodic + recent window；prune_history 做 Phase 1 压缩 + 计数剪枝
    # "hybrid"   — pinned + episodic + recent window（默认，同 summary 行为）
    # "raw"      — 透传模式：跳过 observe/prune/assemble 全部处理
    state_mode: str = "semantic"         # "semantic" 唯一取值 — 决定 pinned state 的 Environment 层渲染策略
    recent_messages: int = 12            # 保留最近 N 条 assistant/tool 消息
    summary_trigger_ratio: float = 0.8   # token_limit 的 80% 触发 Phase 3 LLM 压缩
    pinned_enabled: bool = True          # 是否启用 pinned state 提取
    episodic_max_items: int = 20         # episodic 最大条目数
    output_schema: str = ""              # 向 LLM 描述预期输出格式（并入稳定 system prompt 的 Output / Response Contract）
    memory_read_mode: str = "read_port"  # 读路径；H3 retirement（2026-08-10）后默认 read_port。legacy 仅作 rollback；详见 docs/system_docs/memory.md §12 / AGENTS.md

```

> **注意**：`state_mode` 字段同时存在于 router 端和 worker 端的 `ContextConfig` 中。`ContextManager.__init__` 接受 `state_provider` 参数用于运行时状态注入。

### 2.1 Strategy 对比

| Strategy | observe() | prune_history() | assemble() |
|----------|-----------|-----------------|------------|
| `none`   | 正常执行   | 跳过            | 注入 memory block |
| `summary`| 正常执行   | 正常 prune      | 注入 memory block |
| `hybrid` | 正常执行   | 正常 prune      | 注入 memory block |
| `raw`    | 立即返回   | 跳过            | 透传 system + raw messages |

## 3. ContextManager 基类

### 3.1 数据结构

Router 端和 Worker 端的 `ContextManager` 基类共享相同的核心状态属性，但 **Worker 端额外持有 episodic 记忆**：

```python
# ── Router 端 ContextManager ──
class ContextManager:
    pinned: dict[str, Any]                       # 旧式 dict pinned state（向后兼容）
    _pinned_state: BaseModel | None               # 新式 typed pinned state
    _task_snapshots: dict[str, tuple[list, list[LoadedSkillRef]]]  # 内存 snapshot (messages + skill refs)
    _log_dir: Path | None                         # 磁盘 snapshot 目录
    _skills_dir: Path | None                      # 配置的 skill root（ContextSnapshotV2 恢复重读根）
    _state_provider: StateProvider | None          # 运行时状态注入器
    _runtime_state: RuntimeState | None            # 缓存的最新运行时状态
    _loaded_skills: dict[str, str]                 # get_skill 加载的技能内容
    _loaded_skill_refs: dict[str, LoadedSkillRef]  # snapshot 持久化用 skill 引用（name + path + sha256）
    _previous_summary: str | None                  # Phase 3 压缩的上一轮摘要

# ── Worker 端额外字段 ──
    episodic: list[_Episode]                       # 总结化的历史操作
    _episode_counter: int                          # 操作序号计数器
```

> Router 端的 episodic 记忆由 `CoordinatorContextManager` 通过 `_render_task_plan()` 中的 `task_status_view` 间接管理；Worker 端则由 `observe()` 直接记录 `_Episode`。

### 3.2 observe() — 工具结果处理

每次 Agent 执行完工具后调用。Router 端和 Worker 端的 `observe()` 签名有所不同：

**Router 端 `observe()`** — 包含 `state_mode` 参数，**不**记录 episodic 条目：
```python
def observe(self, tool_name: str, content: str, success: bool, state_mode: str | None = None):
    if self.config.strategy == "raw":
        return                               # raw 模式跳过

    if state_mode is not None and self._pinned_state is not None:
        if hasattr(self._pinned_state, "state_mode"):
            setattr(self._pinned_state, "state_mode", state_mode)

    if self.config.pinned_enabled:
        extracted = self._extract_pinned(tool_name, content, success)
        if extracted:
            self.pinned.update(extracted)     # 更新 dict
            if self._pinned_state:            # 更新 typed state
                for k, v in extracted.items():
                    if hasattr(self._pinned_state, k):
                        setattr(self._pinned_state, k, v)
```

**Worker 端 `observe()`** — 无 `state_mode` 参数，始终记录 episodic 条目：
```python
def observe(self, tool_name: str, content: str, success: bool):
    if self.config.strategy == "raw":
        return

    if self.config.pinned_enabled:
        extracted = self._extract_pinned(tool_name, content, success)
        if extracted:
            self.pinned.update(extracted)
            if self._pinned_state:
                for k, v in extracted.items():
                    if hasattr(self._pinned_state, k):
                        setattr(self._pinned_state, k, v)

    # 记录 episode
    status = "success" if success else "failure"
    truncated = content[:500] + ("..." if len(content) > 500 else "")
    self._episode_counter += 1
    self.episodic.append(_Episode(
        step=self._episode_counter,
        tool_name=tool_name,
        summary=f"{tool_name} → {status}: {truncated}",
    ))
    self._prune_episodic()                    # 保持 episodic 不超过 episodic_max_items
```

### 3.3 prune_history() — 消息剪枝

Router 端和 Worker 端的实现在此方法上差异最大：

**Router 端 `prune_history()`** — 仅做 Phase 1 基于 token 的压缩（截断过长的旧 tool result，不删除消息）：
```python
def prune_history(self, messages: list[Message]) -> None:
    if self.config.strategy in ("none", "raw"):
        return
    self._compress_phase1(messages)
```

**Worker 端 `prune_history()`** — Phase 1 压缩 + 计数剪枝（移除较早的 assistant/tool 消息，被移除的 assistant 消息自动转为 episodic 条目）：
```python
def prune_history(self, messages: list[Message]) -> None:
    if self.config.strategy in ("none", "raw"):
        return
    self._compress_phase1(messages)             # Phase 1: token-based

    # 计数剪枝：保留最近 K 条 exec 消息
    recent = self.config.recent_messages
    exec_indices = [i for i, msg in enumerate(messages)
                    if msg.role in ("assistant", "tool") and i > 0]
    if len(exec_indices) <= recent:
        return
    to_remove = set(exec_indices[:-recent])

    # 移除孤儿 tool 消息（assistant 被剪后紧随其后的 tool）
    remaining_exec = [i for i in exec_indices if i not in to_remove]
    found_assistant = False
    for i in remaining_exec:
        msg = messages[i]
        if msg.role == "assistant":
            found_assistant = True
        elif msg.role == "tool" and not found_assistant:
            to_remove.add(i)

    # 被移除的 assistant 消息 → 追加到 episodic
    for i in sorted(to_remove):
        msg = messages[i]
        if msg.role == "tool":
            continue                           # 已在 observe 中记录
        if msg.role == "assistant" and msg.content:
            self._episode_counter += 1
            self.episodic.append(_Episode(
                step=self._episode_counter,
                tool_name="assistant",
                summary=f"assistant: {str(msg.content)[:200]}",
            ))

    messages[:] = [msg for i, msg in enumerate(messages)
                   if i not in to_remove]
    self._prune_episodic()
```

### 3.4 assemble() — 最终消息组装

```python
def assemble(self, system_prompt, messages):
    if self.config.strategy == "raw":
        return [Message(role="system",
                        content=self._build_stable_system_prompt(system_prompt))] + messages[1:]  # 透传

    result = [Message(role="system",
                      content=self._build_stable_system_prompt(system_prompt))]  # 含 Output / Response Contract
    result.extend(messages[1:])               # 原始消息优先（DeepSeek prefix caching）
    memory_text = self._render_memory_block() # Environment State block（不含输出契约）
    if memory_text and not self._has_unclosed_tool_call(messages):
        result.append(Message(role="user", content=memory_text))  # 尾部 role=user state block
    return result
```

> `_build_stable_system_prompt()` 把 `_render_output_schema()`（含 `ContextConfig.output_schema` 与
> 模式默认值）并入稳定 system prompt 的 `## Output / Response Contract` 段；user state block 永不携带
> 输出契约。`_has_unclosed_tool_call()` 检测最近 assistant turn 是否存在未闭合的 tool call，存在时拒绝
> 本轮 Environment State 追加，由 controller/NeedInput 回填路径闭合协议。

## 4. 三层 Memory Block 渲染

### 4.1 _render_memory_block()

```python
def _render_memory_block(self) -> str:
    """Render the Environment State block (role=user state projection).

    Layout: environment → current state → task plan & progress.
    The output contract is NOT rendered here — it lives in the stable system
    prompt (see _build_stable_system_prompt).
    """
    lines: list[str] = ["---", "## Environment State", "---"]

    env_text = self._render_environment_view()
    if env_text:
        lines.append("### Environment")
        lines.append(env_text)
        lines.append("---")

    state_text = self._render_current_state()
    if state_text:
        lines.append("### Current State")
        lines.append(state_text)
        lines.append("---")

    plan_text = self._render_task_plan()
    if plan_text:
        lines.append("### Task Plan & Progress")
        lines.append(plan_text)
        lines.append("---")

    return "\n".join(lines)
```

输出示例：
```
---
## Environment State
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
### Task Plan & Progress    ← _render_task_plan()（Router 端，展示 dispatched_tasks / task_status_view）
Total tasks: 7
- Active: 3
  ▶️ dispatch_02 (worker_1): running
  🆘 dispatch_05 (worker_3): INPUT_REQUIRED
    ⚠️ Need help at (3,5,1)
- Completed: 3
- Failed: 1
---
```

> 输出契约（`_render_output_schema()`）不再出现在该 user state block 中：`ContextConfig.output_schema` 的动态渲染已迁入稳定 system prompt 构造（`_build_stable_system_prompt`，以 `## Output / Response Contract` 呈现），永不注入末尾 role=user state block。

> Worker 端额外在末尾 appends mailbox section（通过 `_render_mailbox_reminder()`）。Router 端的 `CoordinatorContextManager` 重写 `_render_memory_block()` 以包含完整的 Coordinator 字段。

### 4.2 可覆写方法

父类定义四个空方法，子类覆写以提供领域特定内容：

```python
def _render_environment_view(self) -> str:   # 环境层 → 场景宏观信息
    return ""

def _render_current_state(self) -> str:       # 状态层 → 自身位置/库存/步数
    if not self._pinned_state:
        if self.config.pinned_enabled and self.pinned:
            return "\n".join(f"- {k}: {v}" for k, v in self.pinned.items()
                             if v is not None)
    return ""

def _render_task_plan(self) -> str:           # 任务计划与进度（Router 端展示 task_status_view；Worker 端展示 current_task）
    return ""

def _render_output_schema(self) -> str:       # 输出格式指令（若 config.output_schema 为空则返回 ""）
    if self.config.output_schema:
        return self.config.output_schema
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
    state_mode: str = "semantic"              # "semantic" 唯一取值
    step_budget: dict = {                     # 步数预算
        "current_step": 0,
        "max_steps": 0,
        "remaining": 0,
    }
    mission_finished: bool = False
    dispatched_tasks: list[dict] = []         # 已分发的任务
    worker_results: list[dict] = []           # 收集到的结果
    task_status_view: list[dict] = []         # 所有任务的当前状态视图（用于 Task Plan & Progress 渲染）
    recent_changes: list[str] = []            # 最近的变动摘要
    supervision: dict = {}                    # 监管事件（alerts、unacknowledged_events）
```

> Worker 端不运行 coordinator 逻辑，`worker_agent/context.py` 中没有 `CoordinatorPinnedState` 或 `CoordinatorContextManager` 的副本。

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
    state_mode: str = "semantic"                    # 与 ContextConfig.state_mode 同步
    current_task: dict | None = None                # 当前分配的任务（含 description / status / progress / result）
```

### 5.3 双写策略

```
_extract_pinned() → dict
    → self.pinned.update(extracted)          # dict 写入（向后兼容）
    → self._pinned_state 逐个字段 setattr    # BaseModel 写入（类型安全）
```

## 6. Snapshot 持久化（ContextSnapshotV2）

> ContextSnapshotV2 **只**保存 messages 与 `LoadedSkillRef{name, source_relative_path, content_sha256}`；
> 绝不序列化 `pinned`、RuntimeState payload 或任何领域 projection。恢复时只允许从配置的
> skill root 重读同路径、同 hash 的内容；hash 不匹配或缺文件时跳过并渲染 `SKILL_RELOAD_REQUIRED`，
> 绝不把旧 skill content 当作领域数据复制。

### 6.1 保存

```python
def save_snapshot(self, task_id: str, messages: list) -> None:
    self._task_snapshots[task_id] = (                # 内存
        copy.deepcopy(messages),
        list(self._loaded_skill_refs.values()),      # 仅 LoadedSkillRef（name + path + sha256）
    )
    path = self._snapshot_path(task_id)              # {log_dir}/snapshot_{task_id}.json
    if path is not None:
        try:
            os.makedirs(path.parent, exist_ok=True)
            payload = {
                "version": 2,
                "loaded_skills": [ref.to_dict() for ref in self._loaded_skill_refs.values()],
                "messages": [m.model_dump() for m in messages],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)  # 磁盘
        except OSError:
            pass                                    # 磁盘写入失败时静默跳过
```

`on_skill_loaded(name, content)` 会从渲染内容解析 `**Skill Root Directory:**`，生成
`LoadedSkillRef`（相对 skill root 的 canonical source path + content sha256）。

### 6.2 加载

```python
def load_snapshot(self, task_id: str) -> list | None:
    """Load and remove a snapshot. Checks memory first, then disk."""
    # ① 优先查内存
    if task_id in self._task_snapshots:
        msgs, skill_refs = self._task_snapshots.pop(task_id)
        self._restore_loaded_skills(skill_refs)
        return msgs
    # ② 回退到磁盘
    path = self._snapshot_path(task_id)
    if path is not None and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            os.remove(path)                          # 用完即删
            if isinstance(payload, dict) and "messages" in payload:
                refs = [LoadedSkillRef.from_dict(r)
                        for r in payload.get("loaded_skills", []) if isinstance(r, dict)]
                self._restore_loaded_skills(refs)
                return [Message.model_validate(m) for m in payload["messages"]]
            # 兼容旧格式：payload 本身是 message list
            return [Message.model_validate(m) for m in payload]
        except (OSError, json.JSONDecodeError):
            pass
    return None
```

`_restore_loaded_skills()` 仅通过 `_skills_dir`（配置的 skill root）+ 相对路径重读源文件，
并以 `_reload_skill_content()` 校验 digest；无法验证的技能以 `SKILL_RELOAD_REQUIRED` 占位。

_restore_pinned_data() 优先级：BaseModel.model_validate() > dict update > 静默失败

### 6.3 路径规则

```
{log_dir}/snapshot_{task_id}.json
```

## 7. 子类实现

### 7.1 CoordinatorContextManager

| 覆写方法 | 数据来源 | 输出内容 |
|----------|----------|----------|
| `_render_environment_view()` | `semantic_summary` + `team_status_summary` | Active workers, Total fires, Persons rescued |
| `_render_current_state()` | `step_budget` / `mission_finished` / `dispatched_tasks` / `recent_changes` / `supervision` | Step N/M, Mission status, Recent dispatches, Recent changes, Supervision alerts; 附带状态未改变提示 |
| `_render_task_plan()` | `task_status_view` / `supervision` | 按 planned / active / completed / failed 分组展示任务，标注 help_request 和 alerts |
| `_render_output_schema()` | `config.output_schema` | 给出默认工具调用指令 |
| `_extract_pinned()` | `query_semantic_map` / `query_team_status` / `query_sar_state` / `dispatch_task` / `collect_results` / `finish_task` | 提取语义摘要、团队状态、全局快照、步数预算、已完成标记；**`dispatch_task`/`collect_results` 两个 `tool_name` 分支（`context.py:1204-1219`）在当前工具集下是死代码**——Agentic 模式下 LLM 只调用 `send_message`/`query_task_events`（§3.5），从未产生 `tool_name=="dispatch_task"` 或 `"collect_results"` 的 `post_tool` 事件，导致 `CoordinatorPinnedState.dispatched_tasks`/`worker_results` 字段永远为空，`_render_current_state()` 中 "Dispatched: N tasks" 一行实际不会出现 |

### 7.2 WorkerContextManager

Worker 的上下文管理与 Router 不同：不管理 dispatches 或 team_status，专注于自身状态。

| 覆写方法 | 数据来源 | 输出内容 |
|----------|----------|----------|
| `_render_environment_view()` | `known_fires` / `known_persons` (WorkerPinnedState) | 已知火点和人员（最多显示 5 fires / 3 persons） |
| `_render_current_state()` | `position` / `inventory` / `step` / `mission_status` / `runtime_state.age_ms` | 位置、库存、步数、任务状态、状态陈旧度 |
| `_render_task_plan()` | `current_task` (WorkerPinnedState) | 当前任务描述、状态、进度、结果 |
| `_render_team_coordination()` | `runtime_state.team_coordination.teammates` | 队友位置/库存/任务状态（`### Team Coordination` 小节，位于 Current State 之后、Mailbox 之前） |
| `_render_mailbox_reminder()` | `runtime_state.mailbox_summary` | 未读消息提醒（末尾追加，仅 Worker 端） |
| `_extract_pinned()` | 正则提取位置/库存/步数 + JSON 解析 + `no_op` 特殊处理 | (x,y,z) / 库存列表 / known_fires / known_persons / mission complete |

## 8. 与 AgentController 的集成

```
AgentController.submit()
    → session_factory() → ContextManager 实例
        → agent.attach_context(ctx)          # 注入观察钩子
            → agent.run()
                → post_tool() → ctx.observe(tool_name, result.content, success)
                → pre_llm() → ctx.refresh_runtime_state()
                            → ctx.prune_history(messages)
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
| `src/Agent/router_agent/context.py` | ContextConfig, ContextManager, CoordinatorPinnedState, CoordinatorContextManager | Coordinator 端；`_project_runtime_state_to_pinned()` 注入 step_budget / semantic_summary / team_status_summary / global_snapshot / mission_finished / supervision 等协调器字段 |
| `src/Agent/worker_agent/context.py` | ContextConfig, ContextManager, _Episode, WorkerPinnedState, WorkerContextManager | Worker 端；`_project_runtime_state_to_pinned()` 注入 position / inventory / step / known_fires / known_persons / mission_status / current_task 等自身状态字段；额外包含 `_render_mailbox_reminder()` 和 episodic 管理逻辑 |
| `src/Agent/router_agent/build.py` | build_router_agent, build_router_controller, _tool_descriptions_text | 组装入口 |
| `src/Agent/worker_agent/build.py` | build_agent, build_controller, _tool_descriptions_text | 组装入口 |

> 两端的 `ContextConfig` 完全同构（均包含 `state_mode` 和 `output_schema` 字段），`worker_agent/context.py` 中不存在 `CoordinatorPinnedState` 或 `CoordinatorContextManager` 的冗余副本。

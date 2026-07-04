---
日期: 2026-07-04
文档类型: 技术方案
文档概述: ContextManager 分层重构方案 — Base Block / Pinned / Episodic / Prompt 结构
---

# ContextManager 分层重构方案

## 0. 背景与总览

当前 `ContextManager` 的三层模型（pinned + episodic + recent window）骨架已经就位，但内容层存在五个缺口：
1. **Base 不完整** — system prompt + tools + skills 分散在 LLM API 参数和提示词中，无统一文本表示
2. **Pinned 脆弱** — 正则/json 解析无 schema 校验，无版本控制，snapshot restore 后不刷新
3. **Episodic 粗糙** — `content[:500]` 截断无压缩，无跨 session 持久化
4. **Worker 分层未实现** — base/environment/pinned/goal/history 五层结构
5. **Coordinator 同样问题** — 缺乏统一的分层 prompt 结构

本方案在每个层面提供 **增量迁移路径**，不破坏现有 `strategy="raw"` / `strategy="hybrid"` 行为。

---

## 1. Base Block — 统一系统级描述

### 1.1 问题分析

当前 `assemble()` 输出为：
```
[system: system_prompt]                          ← 纯文本，混着角色描述和风格
[user: "## Agent Context Memory\n..."]            ← memory block
[user: subtask query]
[assistant/tool: ...]                             ← raw messages
```

tools 通过 LLM API `tools` 参数传递**不在 prompt 文本里**；skills 也不在 prompt 里；输出格式（JSON schema、回复风格）没显式声明。LLM 看到的"上下文"是割裂的。

### 1.2 设计方案：`PromptBase` 数据类

新增 `src/Agent/worker_agent/prompt_base.py`（以及 `src/Agent/router_agent/prompt_base.py` 镜像）：

```python
@dataclass
class PromptBase:
    """统一的结构化 base block，替代原始 system_prompt 字符串。"""
    # 角色定义（从 system_prompt 中拆出）
    role: str                           # "You are a search and rescue robot..."
    # 输出风格约束
    output_style: str = ""              # "Reply in Chinese. Be concise."
    # 工具描述文本（自动从 Tool.to_schema() 生成）
    tool_descriptions: str = ""
    # 技能描述文本
    skill_descriptions: str = ""
    # 输出 schema / 回复格式要求
    output_schema: str = ""             # JSON Schema 或 markdown 格式说明

    def render(self) -> str:
        """渲染为完整 text block 供 assemble() 使用。"""
        parts = [self.role]
        if self.output_style:
            parts.append(f"\n## Output Style\n{self.output_style}")
        if self.tool_descriptions:
            parts.append(f"\n## Available Tools\n{self.tool_descriptions}")
        if self.skill_descriptions:
            parts.append(f"\n## Available Skills\n{self.skill_descriptions}")
        if self.output_schema:
            parts.append(f"\n## Output Format\n{self.output_schema}")
        return "\n\n".join(parts)
```

### 1.3 文件改动

| 文件 | 改动 | 迁移策略 |
|------|------|---------|
| **新文件** `src/Agent/worker_agent/prompt_base.py` | `PromptBase` 数据类 + `render()` | — |
| **新文件** `src/Agent/router_agent/prompt_base.py` | 镜像副本 | — |
| `context.py` `ContextManager.assemble()` | 新增 `base_block: PromptBase` 参数；如果提供了 `PromptBase`，用 `base_block.render()` 替换 `system_prompt`，否则 fallback 到原始 `system_prompt` | **向后兼容**：`base_block=None` 时行为不变 |
| `context.py` `ContextManager.__init__()` | 新增 `base_block: PromptBase \| None = None` 字段 | 默认 None |
| `hooks.py` `WorkerSARHooks.pre_llm()` | 从 `agent` 获取 `base_block` 传给 `assemble()` | 仅当 agent 有 `base_block` 属性时生效 |
| `agent.py` `Agent.__init__()` | 新增 `base_block: PromptBase \| None = None` 参数，存储为 `self.base_block` | 默认 None |
| `build.py` `AgentBuildOptions` | 新增 `base_block: PromptBase \| None = None` | 默认 None |

### 1.4 `assemble()` 新签名与逻辑

```python
def assemble(
    self,
    system_prompt: str,
    messages: list[Message],
    base_block: PromptBase | None = None,
) -> list[Message]:
    if self.config.strategy == "raw":
        return [Message(role="system", content=system_prompt), *messages[1:]]

    final_system = base_block.render() if base_block else system_prompt
    result = [Message(role="system", content=final_system)]
    memory_text = self._render_memory_block()
    if memory_text:
        result.append(Message(role="user", content=memory_text))
    result.extend(messages[1:])
    return result
```

### 1.5 `tool_descriptions` 自动生成

在 `PromptBase` 上增加 @classmethod：

```python
@classmethod
def from_tools(
    cls,
    role: str,
    tools: list[Tool],
    output_style: str = "",
    output_schema: str = "",
) -> "PromptBase":
    lines = []
    for t in tools:
        lines.append(f"- **{t.name}**: {t.description}")
        lines.append(f"  Parameters: {json.dumps(t.parameters, indent=2, ensure_ascii=False)}")
    return cls(
        role=role,
        tool_descriptions="\n".join(lines),
        output_style=output_style,
        output_schema=output_schema,
    )
```

### 1.6 优先级 & 实现顺序

**P0**（不做 Base Block 会导致后续所有分层设计缺乏载体）：

1. 新建 `prompt_base.py`（worker + router 双副本）
2. 修改 `ContextManager.__init__` + `assemble()` 接受 `base_block` 参数
3. 修改 `Agent.__init__` + `hooks.pre_llm` 传递 `base_block`
4. 修改 `build.py` 暴露 `base_block` 配置

---

## 2. Pinned Redesign — 可验证的 typed schema

### 2.1 问题分析

当前 `self.pinned: dict[str, Any]` 是**无类型 dict**：
- `_extract_pinned()` 返回 `dict[str, Any] | None`，无 schema 校验
- `WorkerContextManager.__init__` 用硬编码 dict 初始化默认值
- snapshot restore 后 pinned 仍然是旧的（不重新 query 环境）
- 无版本号，schema 变更时无法检测不兼容

### 2.2 设计方案：Pydantic 或 dataclass 的 PinnedState

**选择 `pydantic.BaseModel`**（理由：已有 `pydantic` 依赖，自带 `model_validate` / `model_dump` / 类型校验）。

#### WorkerPinnedState

```python
# 新建 context.py 同级文件 pinned_state.py（worker + router 共享上层抽象）

from pydantic import BaseModel, Field

class WorkerPinnedState(BaseModel):
    """Worker 侧 pinned state schema。"""
    version: int = Field(default=1, ge=1)       # schema 版本号
    position: tuple[int, int, int] | None = None
    inventory: list[str] = Field(default_factory=list)
    step: int = 0
    known_fires: list[dict] = Field(default_factory=list)
    known_persons: list[dict] = Field(default_factory=list)
    mission_status: str = "in_progress"

    def needs_refresh(self) -> bool:
        """是否缺少关键字段，需要 get_agent_state 刷新。"""
        return self.position is None
```

#### CoordinatorPinnedState

```python
class CoordinatorPinnedState(BaseModel):
    """Coordinator 侧 pinned state schema。"""
    version: int = Field(default=1, ge=1)
    global_snapshot: dict = Field(default_factory=dict)
    step_budget: dict = Field(default_factory=lambda: {
        "current_step": 0, "max_steps": 0, "remaining": 0
    })
    mission_finished: bool = False
    dispatched_tasks: list[dict] = Field(default_factory=list)
    worker_results: list[dict] = Field(default_factory=list)
```

### 2.3 版本兼容策略

```python
class WorkerPinnedState(BaseModel):
    version: int = Field(default=1, ge=1)

    @classmethod
    def migrate(cls, data: dict) -> "WorkerPinnedState":
        version = data.get("version", 0)
        if version < 1:
            # 从旧 dict 格式升级
            return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
        return cls.model_validate(data)
```

### 2.4 Snapshot restore 后刷新机制

在 `ContextManager` 上增加方法：

```python
class ContextManager:
    def needs_state_refresh(self) -> bool:
        """snapshot restore 后调用，判断是否需要重新获取环境状态。"""
        if not hasattr(self, "_pinned_state"):
            return False
        return self._pinned_state.needs_refresh()
```

`WorkerSARHooks.on_run_start()` 中检查：

```python
async def on_run_start(self, agent, user_message):
    if self._ctx.needs_state_refresh():
        # 自动注入 get_agent_state 调用
        agent._pending_state_refresh = True
```

注意：这一机制在 **P2** 阶段实现，**P1** 先完成 typed schema 的切换。

### 2.5 文件改动

| 文件 | 改动 | 迁移策略 |
|------|------|---------|
| **新文件** `src/Agent/worker_agent/pinned_state.py` | `WorkerPinnedState` + `CoordinatorPinnedState` | — |
| **新文件** `src/Agent/router_agent/pinned_state.py` | 镜像副本 | — |
| `context.py` `ContextManager.__init__()` | `self.pinned: dict` → `self._pinned_state: BaseModel \| None = None`；保留 `pinned` property 向后兼容 | **向后兼容**：`self._pinned_state` 为 None 时，`pinned` property 返回空 dict |
| `context.py` `_extract_pinned()` | 签名不变；子类返回 dict 后再做 `_pinned_state.model_validate(updates)` 校验 | 先完成 typed schema 内部化，`pinned` property 逐步弃用 |
| `context.py` `_render_memory_block()` | 从 `_pinned_state` 读取字段改为 typed access | 双写阶段：同时更新 `pinned` dict 和 `_pinned_state` |
| `context.py` `save_snapshot()` | 额外序列化 `_pinned_state.model_dump()` + `version` 到 snapshot JSON | |

### 2.6 优先级 & 实现顺序

**P1**（Pinned 是三层中最频繁被访问的，脆弱的解析直接影响 LLM 感知的上下文质量）：

1. 新建 `pinned_state.py`（双副本）
2. 修改 `WorkerContextManager.__init__()` 初始化 `_pinned_state = WorkerPinnedState()`
3. 修改 `_extract_pinned()` 返回后做 `_pinned_state.model_validate()` 级联更新
4. 修改 `_render_memory_block()` 从 `_pinned_state` 读取字段
5. 修改 `save_snapshot()` 和 `load_snapshot()` 序列化 / 反序列化 `_pinned_state`
6. 修改 `CoordinatorContextManager` 同理

---

## 3. Episodic Enhancement — LLM 压缩 + 持久化

### 3.1 问题分析

当前 `_Episode` 用 `content[:500]` 截断：
```python
summary=f"{tool_name} → {status}: {truncated}"  # truncation at 500 chars
```

问题：
- 无语义压缩，500 字符仍然可能丢失关键信息
- 无跨 session 持久化（重新 dispatch 后 episodic 为空）
- `_render_memory_block()` 只展示最后 10 条
- 无去重（NoOp 工具连续返回类似内容时，episodic 被稀释）

### 3.2 设计方案：三步迁移路径

#### Step 1：增强 `_Episode`（P1 同步做）

```python
@dataclass
class _Episode:
    step: int = 0
    tool_name: str = ""
    summary: str = ""
    tool_args: dict | None = None          # 新增：记录关键参数
    token_count: int = 0                   # 新增：原始消息的 token 数（为 LLM 压缩决策用）
    compressed: bool = False               # 新增：是否已经过 LLM 压缩
```

#### Step 2：LLM 压缩选项（P2）

在 `ContextConfig` 新增：

```python
@dataclass
class ContextConfig:
    strategy: str = "hybrid"
    recent_messages: int = 12
    summary_trigger_ratio: float = 0.8
    pinned_enabled: bool = True
    episodic_max_items: int = 20
    episodic_llm_compress: bool = False     # 新增：是否启用 LLM 压缩
    episodic_compress_interval: int = 5     # 新增：每 N 条做一次批量压缩
```

新增 `ContextManager._llm_compress_episodic()`：

```python
async def _llm_compress_episodic(self, llm_client) -> None:
    """对未压缩的 episodic 条目做 LLM 批量压缩。"""
    uncompressed = [ep for ep in self.episodic if not ep.compressed]
    if len(uncompressed) < self.config.episodic_compress_interval:
        return
    # 调用 LLM 做摘要：输入 N 条未压缩条目 → 输出 N 条压缩摘要
    ...
```

**注意**：`observe()` 当前是同步方法。LLM 压缩需要在异步上下文中完成。有两种方案：

**方案 A（推荐）**：`observe()` 保持同步不做压缩；在 `pre_llm hook` 中异步调用压缩：

```python
# hooks.py
async def pre_llm(self, agent, messages):
    self._ctx.prune_history(agent.messages)
    if self._ctx.config.episodic_llm_compress:
        await self._ctx._llm_compress_episodic(agent.llm)
    return self._ctx.assemble(agent.system_prompt, agent.messages)
```

#### Step 3：跨 session 持久化（P3）

在 `ContextManager` 中新增 episodic 持久化方法：

```python
def _episodic_path(self) -> Path | None:
    if self._log_dir is None:
        return None
    return self._log_dir / "episodic_memory.json"

def _save_episodic(self) -> None:
    path = self._episodic_path()
    if path is None:
        return
    data = [
        {"step": e.step, "tool_name": e.tool_name, "summary": e.summary,
         "tool_args": e.tool_args, "compressed": e.compressed}
        for e in self.episodic
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def _load_episodic(self) -> None:
    path = self._episodic_path()
    if path and path.exists():
        data = json.loads(path.read_text())
        self.episodic = [_Episode(**d) for d in data]
        if self.episodic:
            self._episode_counter = max(e.step for e in self.episodic)
```

在 `__init__()` 末尾调用 `_load_episodic()`，在每次 `observe()` 后调用 `_save_episodic()`。

### 3.3 文件改动

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `context.py` `_Episode` 数据类 | 新增 `tool_args` / `token_count` / `compressed` 字段 | P1 |
| `context.py` `ContextConfig` | 新增 `episodic_llm_compress` / `episodic_compress_interval` | P2 |
| `context.py` `ContextManager` | 新增 `_llm_compress_episodic()` | P2 |
| `context.py` `ContextManager` | 新增 `_save_episodic()` / `_load_episodic()` | P3 |
| `hooks.py` `pre_llm()` | 异步调用 `_llm_compress_episodic()`（条件性） | P2 |
| `context.py` `observe()` | 末尾调用 `_save_episodic()`（条件性，仅在 `_log_dir` 非 None 时） | P3 |

### 3.4 去重策略（P2 可选）

在 `observe()` 中，对同工具名连续相同摘要去重：

```python
def observe(self, tool_name: str, content: str, success: bool):
    ...
    # 去重检查：连续两个 NoOp 或相同工具名称+相同摘要不重复记录
    if self.episodic and self.episodic[-1].tool_name == tool_name:
        last_summary = self.episodic[-1].summary
        new_summary = f"{tool_name} → {status}: {truncated}"
        if last_summary == new_summary:
            return  # 跳过完全重复
    ...
```

---

## 4. Worker Prompt Structure — 分层渲染

### 4.1 设计目标

输出格式：
```
┌─ base (system prompt + output style + tools + skills)   ← PromptBase.render()
├─ environment view (other workers, global map)            ← 动态 query 结果（agent 工具返回）
├─ current state (GPS, battery, load, inventory)           ← WorkerPinnedState
├─ subtask goal                                            ← 当前 user query
└─ execution history (subtask interactions)                ← 最近 N 条原始 message
```

### 4.2 `_render_memory_block()` 新实现

当前只输出 pinned + episodic。新版分层：

```python
def _render_memory_block(self) -> str:
    """渲染 memory block（不包含 base，因为 base 已放入 system prompt）。"""
    lines: list[str] = ["---", "## Context Memory", "---"]

    # ── layer 1: environment view ──
    env_text = self._render_environment_view()
    if env_text:
        lines.append("### Environment")
        lines.append(env_text)
        lines.append("---")

    # ── layer 2: current state ──
    state_text = self._render_current_state()
    if state_text:
        lines.append("### Current State")
        lines.append(state_text)
        lines.append("---")

    # ── layer 3: episodic memory ──
    if self.episodic:
        lines.append(f"### Action History (last {len(self.episodic)} steps)")
        for ep in self.episodic[-10:]:
            icon = "✓" if "success" in ep.summary else "✗"
            lines.append(f"- {icon} step {ep.step}: {ep.summary}")
        lines.append("---")

    return "\n".join(lines)

def _render_environment_view(self) -> str:
    """Worker: 从 pinned state 中提取已知环境信息。"""
    if not hasattr(self, "_pinned_state"):
        return ""
    ps = self._pinned_state
    parts = []
    if ps.known_fires:
        parts.append(f"Known fires: {len(ps.known_fires)} locations")
        for f in ps.known_fires[:5]:
            parts.append(f"  - {f}")
    if ps.known_persons:
        parts.append(f"Known persons: {len(ps.known_persons)}")
        for p in ps.known_persons[:3]:
            parts.append(f"  - {p}")
    return "\n".join(parts)

def _render_current_state(self) -> str:
    """Worker: 当前位置、库存、步骤。"""
    if not hasattr(self, "_pinned_state"):
        # fallback to old dict-style pinned
        return "\n".join(f"- {k}: {v}" for k, v in self.pinned.items() if v is not None)
    ps = self._pinned_state
    lines = []
    if ps.position:
        lines.append(f"- Position: ({ps.position[0]}, {ps.position[1]}, {ps.position[2]})")
    if ps.inventory:
        lines.append(f"- Inventory: {ps.inventory}")
    lines.append(f"- Step: {ps.step}")
    lines.append(f"- Mission: {ps.mission_status}")
    return "\n".join(lines)
```

### 4.3 `assemble()` 最终输出示例

```
[system]
You are a search and rescue robot...
## Output Style
Reply in Chinese...
## Available Tools
- navigate_to: Move to a position...
- get_agent_state: Query current state...
...

[user]
---
## Context Memory
---
### Environment
Known fires: 2 locations
  - [1,4,0] intensity=medium
  - [5,1,0] intensity=low
Known persons: 1
  - [2,3,0] rescued=False
---
### Current State
- Position: (3, 2, 0)
- Inventory: ['Water']
- Step: 8
- Mission: in_progress
---
### Action History (last 5 steps)
- ✓ step 4: navigate_to → success: Arrived at (1,4)...
- ✓ step 5: get_supply → success: Obtained Water...
---

[user]
Your subtask is to explore the northeast quadrant...

[assistant]
...

[tool]
...
```

### 4.4 文件改动

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `context.py` `_render_memory_block()` | 重写为分层结构 | P1 |
| `context.py` | 新增 `_render_environment_view()` / `_render_current_state()` | P1 |
| `context.py` `render()` 体系 | `_render_memory_block()` 改为调用子方法 | P1 |

---

## 5. Coordinator Prompt Structure

### 5.1 设计

Coordinatord 的分层不同于 Worker：

```
┌─ base (orchestration role, tools)                       ← PromptBase.render()
├─ global snapshot / step budget                           ← CoordinatorPinnedState
├─ worker status / dispatched tasks                        ← CoordinatorPinnedState
├─ current goal                                            ← 当前 user query
└─ execution history (subtask dispatch + results)          ← 最近 N 条消息
```

### 5.2 Override `_render_memory_block()` 方法

```python
class CoordinatorContextManager(ContextManager):
    def _render_memory_block(self) -> str:
        lines: list[str] = ["---", "## Mission Context", "---"]

        # ── layer 1: step budget ──
        budget = self._pinned_state.step_budget if hasattr(self, "_pinned_state") else {}
        if budget:
            lines.append(f"### Step Budget: {budget.get('current_step', 0)} / {budget.get('max_steps', 0)} "
                         f"(remaining: {budget.get('remaining', 0)})")

        # ── layer 2: global snapshot ──
        snapshot_text = self._render_global_snapshot()
        if snapshot_text:
            lines.append("### Global State")
            lines.append(snapshot_text)
            lines.append("---")

        # ── layer 3: dispatched tasks ──
        tasks_text = self._render_dispatched_tasks()
        if tasks_text:
            lines.append("### Dispatched Tasks")
            lines.append(tasks_text)
            lines.append("---")

        # ── layer 4: episodic ──
        if self.episodic:
            lines.append(f"### Recent Actions (last {len(self.episodic)})")
            for ep in self.episodic[-10:]:
                lines.append(f"- step {ep.step}: {ep.summary}")
            lines.append("---")

        return "\n".join(lines)

    def _render_global_snapshot(self) -> str:
        ps = getattr(self, "_pinned_state", None)
        if not ps or not ps.global_snapshot:
            return ""
        snap = ps.global_snapshot
        parts = []
        agents = snap.get("agents", [])
        parts.append(f"Active workers: {len(agents)}")
        fires = snap.get("fires", [])
        parts.append(f"Total fires: {len(fires)}")
        persons = snap.get("persons", [])
        rescued = sum(1 for p in persons if p.get("rescued"))
        parts.append(f"Persons: {len(persons)} total, {rescued} rescued")
        return "\n".join(parts)

    def _render_dispatched_tasks(self) -> str:
        ps = getattr(self, "_pinned_state", None)
        if not ps or not ps.dispatched_tasks:
            return ""
        return "\n".join(
            f"- {t['agent_id']}: {t['task_id']}"
            for t in ps.dispatched_tasks[-5:]
        )
```

### 5.3 文件改动

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `router_agent/context.py` | `CoordinatorContextManager._render_memory_block()` 重写 | P1（随 worker 同步） |
| `router_agent/context.py` | 新增 `_render_global_snapshot()` / `_render_dispatched_tasks()` | P1 |

---

## 6. 汇总：实施路线图

### 阶段总览

```
P0: Base Block          ─  prompt_base.py + assemble() 改造
P1: Pinned Redesign     ─  pinned_state.py + typed schema + 分层 render
P2: Episodic LLM Compress ─ _llm_compress_episodic() + 去重
P3: Episodic Persist    ─  _save_episodic / _load_episodic
```

### 完整文件改动清单

| 文件 | P0 | P1 | P2 | P3 |
|------|:--:|:--:|:--:|:--:|
| `src/Agent/worker_agent/prompt_base.py` | **NEW** | — | — | — |
| `src/Agent/router_agent/prompt_base.py` | **NEW** | — | — | — |
| `src/Agent/worker_agent/pinned_state.py` | — | **NEW** | — | — |
| `src/Agent/router_agent/pinned_state.py` | — | **NEW** | — | — |
| `worker_agent/context.py` | `__init__` + `assemble()` | `_Episode` + `_render_*` + snapshot | `_llm_compress` + 去重 | `_save/_load_episodic` |
| `router_agent/context.py` | `__init__` + `assemble()` | `_Episode` + `_render_*` + snapshot | `_llm_compress` + 去重 | `_save/_load_episodic` |
| `worker_agent/hooks.py` | `pre_llm` 传 base_block | — | `pre_llm` 调 compress | — |
| `router_agent/hooks.py` | 同步 | — | 同步 | — |
| `worker_agent/agent.py` | `__init__` + `base_block` | — | — | — |
| `router_agent/agent.py` | 同步 | — | — | — |
| `worker_agent/build.py` | `AgentBuildOptions` 加 base_block | — | — | — |
| `router_agent/build.py` | 同步 | — | — | — |

### 验证策略

每个阶段完成后运行：

```bash
# P0 验证：backward compat — strategy="raw" 完全无变化
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 3

# P1 验证：检查 summary.csv 中 token 消耗和 trajectory
# 对比 P0 baseline，确保 pinned 字段正确提取

# ruff check 贯穿所有阶段
uv run --with ruff ruff check src/ sar_orch/
```

### 风险与缓解

| 风险 | 缓解 |
|------|------|
| `PromptBase.render()` 产生的 system prompt 超过 token 限额 | 在 `ContextConfig` 中增加 `max_base_tokens`，超出时自动修剪工具列表 |
| Pinned typed schema 迁移破坏现有 snapshot 恢复 | `migrate()` 类方法处理旧格式升级；`load_snapshot()` 中 try-except fallback |
| `_llm_compress_episodic()` 在 pre_llm 中增加延迟 | 仅在 `episodic_llm_compress=True` 且未压缩条目数超过阈值时才触发；设默认 False |
| 双副本同步负担 | 每个阶段后用 `diff` 命令对比 `worker_agent/` 和 `router_agent/` 中的对应文件，确保一致 |

---
日期: 2026-07-02
文档类型: 技术设计文档
文档概述: 为 LLaMAR Agent 框架设计上下文管理机制，包含长期记忆、会话持久化、Hook 机制和 Agent Loop 退出条件改造。
---

# Agent 上下文管理机制设计方案

## 1. 背景与现状

### 1.1 Agent 上下文现状

当前 Agent 框架（`src/Agent/`）的上下文管理存在以下问题：

- **上下文载体单一**：`self.messages: list[Message]` 是唯一上下文载体，无独立 memory 对象。
- **压缩策略粗糙**：仅 `_summarize_messages()` 在 token > 80000 时用 LLM 摘要，丢失 SAR 关键状态（坐标/库存/火势）。
- **无长期记忆**：每次 A2A 任务新建 Agent 实例，跨子任务无记忆延续。
- **退出条件脆弱**：`if not response.tool_calls: return response.content`（agent.py:504），纯文本回复即视为任务结束。
- **双副本同步负担**：`worker_agent/agent.py` 与 `router_agent/agent.py` 完全镜像（650 行×2），任何改动须手动同步。

### 1.2 关键文件与位置

| 组件 | 文件 | 关键行号 |
|------|------|---------|
| Worker Agent 类 | `src/Agent/worker_agent/agent.py` | L48-650 |
| Router Agent 类 | `src/Agent/router_agent/agent.py` | L48-650（镜像） |
| ToolResult (SAR工具用) | `src/Agent/router_agent/tools/base.py` | L8-13 |
| ToolResult (Worker Agent用) | `src/Agent/worker_agent/tools/base.py` | L8-13 |
| AgentAdapter (Worker侧) | `src/a2a/worker/agent_adapter.py` | L37, L80-124, L141-201 |
| CoordinatorAgentExecutor | `src/a2a/coordinator/agent_executor.py` | L83, L217-316 |
| RouterAgent | `src/a2a/coordinator/router.py` | L360-371, L459-512, L636-656 |
| TaskStore | `src/a2a/coordinator/task_store.py` | L37-58 |
| DispatchTaskTool | `src/a2a/builtin_tools/dispatch_task.py` | L15-110 |
| push_task | `src/a2a/coordinator/router.py` | L636-656 |
| SAR Worker | `sar_orch/worker.py` | L14-174 |
| SAR Coordinator | `sar_orch/coordinator.py` | L10-160 |
| Experiment runner | `sar_orch/experiment.py` | L44-261 |

### 1.3 重要发现

1. **SAR 工具统一使用 router_agent 的 ToolResult**：所有 `sar_orch/tools/worker/*.py` 和 `sar_orch/tools/coordinator/*.py` 都 `from Agent.router_agent.tools.base import Tool, ToolResult`。Worker Agent 类则 import 自己的 `worker_agent.tools.base`。两者是 pydantic BaseModel，字段一致时鸭子类型兼容。
2. **context_id 传播缺失**：`push_task`（router.py:640-644）创建 Message 时不设 context_id，导致 `new_task_from_user_message` 每次生成新 UUID，子任务间无会话延续。
3. **DAG 模式也需要 context_id**：`_dispatch_layer`（agent_executor.py:810）也调 `push_task`，需同步改造。
4. **SAR 使用 agentic 模式**：`orchestration_mode="agentic"`（coordinator.py:92），主要改造 `_execute_agentic` 路径。

---

## 2. 设计决策（已确认）

| 决策点 | 选择 | 理由 |
|--------|------|------|
| run() 返回值 | 改为 `RunResult` 对象 | success 是一等公民，语义清晰 |
| 双副本处理 | 保持 worker_agent / router_agent 双副本，手动同步 | 遵循现有约定（AGENTS.md 已注明） |
| 持久化范围 | Worker + Coordinator 两端都持久化 | 跨整个 SAR 周期保持记忆 |
| 完成信号 | 显式 `finish_task` 工具 + ToolResult.task_complete 标志 | 双保险，结构化回传 coordinator |
| ContextManager 位置 | 分别放各自框架目录 | `worker_agent/context.py` + `router_agent/context.py` |
| 会话持久化机制 | 不缓存 Agent，用 context_id 维持会话 | 每次任务新建 Agent，注入已有会话记忆 |
| context_id 传播 | 改 push_task 签名 + dispatch_task 工具 | 接受改工具方式 |

---

## 3. 架构设计

### 3.1 会话持久化机制

**核心思路**：AgentAdapter / CoordinatorAgentExecutor 维护 `dict[context_id, ContextManager]` 会话存储。每次 execute() 仍新建 Agent，但注入已有会话的记忆。

```
                    SAR Experiment (context_id = C0)
                           │
                    ┌──────┴──────┐
                    │ Coordinator │
                    │ sessions[C0]│── CoordinatorContextManager
                    │ (新建Agent) │     ├ pinned: global_snapshot, step_budget
                    │             │     ├ episodic: dispatched_tasks, worker_results
                    └──────┬──────┘
                           │ dispatch_task(prompt, context_id=C0)
                           │ push_task(agent_id, prompt, context_id=C0)
                    ┌──────┴──────┐
                    │  Worker A   │
                    │ sessions[C0]│── WorkerContextManager
                    │ (新建Agent) │     ├ pinned: position, inventory, step
                    │             │     ├ episodic: action_history
                    └─────────────┘
```

**context_id 传播链**：

1. experiment.py → coordinator.submit_task() → A2A JSON-RPC（不设 context_id → 自动生成 C0）
2. CoordinatorAgentExecutor.execute() → `context_id = task.context_id`（= C0）
3. _execute_agentic() → TaskStore(context_id=C0)
4. DispatchTaskTool.execute() → `router.push_task(agent_id, prompt, context_id=self._store.context_id)`
5. push_task → `Message(context_id=context_id, ...)`
6. Worker AgentAdapter.execute() → `context_id = task.context_id`（= C0）
7. `self._sessions[C0]` → 复用 WorkerContextManager

**Worker AgentAdapter.execute() 流程**：

```python
async def execute(self, context, event_queue):
    context_id = task.context_id
    # 查会话
    ctx = self._sessions.get(context_id)
    if ctx is None:
        ctx = WorkerContextManager(self._context_config, self._token_limit)
        self._sessions[context_id] = ctx
    # 新建 Agent（不缓存）
    agent = self._build_agent()
    agent.attach_context(ctx)           # 注入会话记忆 + hooks
    agent.add_user_message(query)
    result = await agent.run(step_callback=...)
    # 会话保留在 self._sessions 中，下次同 context_id 复用
```

### 3.2 ContextManager 三层记忆结构

每个 ContextManager 管理三层记忆：

| 层 | 内容 | 生命周期 | 更新时机 |
|----|------|---------|---------|
| **Pinned State** | 结构化关键状态（dict） | 实时更新，单条 | post_tool hook |
| **Episodic Memory** | 动作历史摘要列表 | 整个 SAR 周期 | post_tool hook + prune |
| **Recent Window** | 最近 N 条原始 message | 滑动窗口 | pre_llm hook 裁剪 |

**组装后实际上下文**（pre_llm 返回给 LLM 的 message 列表）：

```
[system_prompt]
[memory_block — 注入为一条 user message]
  ## Current State
  position: (3,2,0), inventory: [Water×1], step: 8/15
  ## Known Fires: [(1,4):medium, (5,1):low]
  ## Known Persons: [(2,3):unrescued]
  ## Mission: in_progress
  ## Recent Episodes (last 10)
  - step3: NavigateTo(1,4) → arrived
  - step4: GetSupply(Water) → success
  ...
[user: 原始子任务指令]
[recent N 条 assistant/tool 原始消息]
```

**关键设计：pre_llm 会修改 self.messages**

pre_llm hook 不仅是只读组装，还会 prune self.messages：
1. 如果 `len(self.messages) > recent_window`，将超出窗口的旧 assistant/tool 消息移入 episodic memory（摘要化）。
2. 保留 system + 所有 user messages + 最近 N 条。
3. 渲染 memory_block（pinned + episodic 摘要）。
4. 返回 `[system_msg, memory_block_msg, ...self.messages]`。

这样 self.messages 保持有界，不会无限增长。

### 3.3 Worker vs Coordinator ContextManager 对比

```python
# src/Agent/worker_agent/context.py
class WorkerContextManager(ContextManager):
    """Worker 侧：管理本地状态。"""
    # pinned schema:
    #   position: tuple(x, y, z)
    #   inventory: list[str]  — Sand/Water/Person
    #   step: int
    #   known_fires: list[dict]  — [{pos, intensity}]
    #   known_persons: list[dict]  — [{pos, rescued}]
    #   mission_status: str  — "in_progress" | "complete"
    #
    # 提取来源:
    #   navigate_to      → position
    #   get_agent_state  → position, inventory, step
    #   explore          → known_fires, known_persons
    #   no_op            → mission_status
```

```python
# src/Agent/router_agent/context.py
class CoordinatorContextManager(ContextManager):
    """Coordinator 侧：管理全局状态。"""
    # pinned schema:
    #   global_snapshot: dict  — {agents, fires, persons, reservoirs, deposits}
    #   step_budget: dict  — {current_step, max_steps, remaining}
    #   mission_finished: bool
    #   dispatched_tasks: list[dict]  — [{task_id, agent_id, prompt, status}]
    #   worker_results: list[dict]  — [{task_id, success, summary}]
    #
    # 提取来源:
    #   query_sar_state  → global_snapshot, step_budget, mission_finished
    #   dispatch_task    → dispatched_tasks
    #   collect_results  → worker_results
```

### 3.4 Hook 机制

```python
# src/Agent/worker_agent/hooks.py (和 router_agent/hooks.py 镜像)

class AgentHooks(Protocol):
    async def on_run_start(self, agent, user_message: str) -> None: ...
    async def on_run_end(self, agent, result: RunResult) -> None: ...
    async def pre_llm(self, agent, messages: list) -> list:
        """可改写 messages（含 prune self.messages），返回发给 LLM 的最终列表。"""
    async def post_llm(self, agent, response) -> None: ...
    async def pre_tool(self, agent, tool_name: str, args: dict) -> dict: ...
    async def post_tool(self, agent, tool_name: str, result: ToolResult) -> ToolResult:
        """可改写 result。ContextManager 在此 observe。"""
    async def should_continue(self, agent, step: int) -> bool:
        """返回 False 则结束 loop。替代 'no tool_calls → return'。"""
```

```python
class WorkerSARHooks(AgentHooks):
    def __init__(self, ctx: WorkerContextManager): ...
    async def pre_llm(self, agent, messages):
        self._ctx.prune_history(agent.messages)      # 有界化
        return self._ctx.assemble(agent.system_prompt, agent.messages)
    async def post_tool(self, agent, tool_name, result):
        self._ctx.observe(tool_name, result.content, result.success)
        return result
    async def should_continue(self, agent, step):
        return not agent._task_complete
```

**Hook 插入点**（agent.py run loop）：

| Hook | 插入位置 | 当前代码行号 |
|------|---------|-------------|
| on_run_start | run() 开始，L399 后 | agent.py:399 |
| pre_llm | `self.llm.generate()` 前，L438 | agent.py:438 |
| post_llm | token 累加后，L462 后 | agent.py:462 |
| pre_tool | `tool.execute()` 前，L565 | agent.py:565 |
| post_tool | `tool.execute()` 后，L565 后 | agent.py:565 |
| should_continue | 替换 `if not response.tool_calls: return`，L504 | agent.py:504 |
| on_run_end | run() return 前 | agent.py:510/646 |

### 3.5 RunResult

```python
@dataclass
class RunResult:
    content: str                       # 最终文本
    success: bool | None = None        # True=成功, False=失败, None=未判定/超时
    steps_used: int = 0
    task_description: str = ""         # 来自 finish_task 的 task_description
```

**受影响的调用方**（4 处）：

| 文件 | 行号 | 适配方式 |
|------|------|---------|
| `src/a2a/worker/agent_adapter.py` | L110-114 | `result = await agent.run(...); final_text = result.content` + 写 `result.success` 到 artifact metadata |
| `src/a2a/coordinator/router.py` | L370 | `result = await agent.run(); return self._parse_dag_result(result.content)` |
| `src/a2a/coordinator/router.py` | L423 | 同上 |
| `src/a2a/coordinator/agent_executor.py` | L282 | `result = await agent.run(...); final_text = result.content` + 读 `result.success` |

### 3.6 ToolResult 扩展

```python
class ToolResult(BaseModel):
    success: bool
    content: str = ""
    error: str | None = None
    task_complete: bool = False              # 新增：工具是否标记任务完成
    mission_success: bool | None = None      # 新增：任务成功/失败/未判定
```

**注意**：SAR 工具统一 import `router_agent.tools.base.ToolResult`，所以 `router_agent` 的改动是关键路径。`worker_agent` 的改动用于 Agent 类自身的类型一致性。

### 3.7 finish_task 工具

#### Worker 侧

```python
# sar_orch/tools/worker/finish_task.py
class FinishTaskTool(Tool):
    name = "finish_task"
    description = (
        "Call this when your assigned subtask is complete. "
        "Sends a completion signal to the coordinator with the result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether the subtask succeeded"},
            "summary": {"type": "string", "description": "What you did and the outcome"},
            "task_description": {"type": "string", "description": "Description of the task you completed"},
        },
        "required": ["success", "summary", "task_description"],
    }
    async def execute(self, success, summary, task_description):
        return ToolResult(
            success=True,
            content=f"[TASK COMPLETE] {summary}",
            task_complete=True,
            mission_success=success,
        )
```

调用后：
1. Agent loop 检测 `task_complete=True` → 退出 → 返回 `RunResult(success, content=summary, task_description)`
2. AgentAdapter 将 RunResult 序列化为 artifact 回传 coordinator
3. Coordinator 的 collect_results 解析，写入 CoordinatorContextManager.episodic

#### Coordinator 侧

```python
# sar_orch/tools/coordinator/finish_task.py
class FinishTaskTool(Tool):
    name = "finish_task"
    description = "Call this when the overall SAR mission is complete."
    parameters = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether the overall mission succeeded"},
            "summary": {"type": "string", "description": "Mission summary"},
        },
        "required": ["success", "summary"],
    }
```

注入方式：加入 `_execute_agentic` 的 tools 列表（agent_executor.py:236-242），需要访问 store 来标记编排结束。

### 3.8 Agent Loop 退出逻辑改造

**当前**（agent.py:504-510）：
```python
if not response.tool_calls:
    return response.content
```

**改造后**：
```python
if not response.tool_calls:
    # 检查是否有工具设置了 task_complete
    if self._task_complete:
        return RunResult(
            content=response.content,
            success=self._mission_success,
            steps_used=step + 1,
            task_description=self._task_description,
        )
    # 未完成 → 不退出
    if self.require_explicit_completion:
        # 注入 nudge，重试（有限次）
        self._inject_continue_nudge()
        self._nudge_count += 1
        if self._nudge_count > self._max_nudges:
            return RunResult(content=response.content, success=None, steps_used=step + 1)
        continue
    else:
        # 向后兼容：无 finish_task 机制时，文本回复即结束
        return RunResult(content=response.content, success=None, steps_used=step + 1)
```

**新增配置**：
```python
require_explicit_completion: bool = False   # SAR 设 True，其他场景 False
_max_nudges: int = 3
```

### 3.9 Agent.__init__ 新增参数

```python
def __init__(
    self,
    llm_client: LLMClient,
    system_prompt: str,
    tools: list[Tool],
    max_steps: int = 50,
    workspace_dir: str = "./workspace",
    token_limit: int = 80000,
    log_dir: str | Path | None = None,
    # —— 新增 ——
    context_strategy: str = "hybrid",
    context_recent_messages: int = 12,
    context_summary_trigger_ratio: float = 0.8,
    context_pinned_enabled: bool = True,
    hooks: AgentHooks | None = None,
    require_explicit_completion: bool = False,
):
```

```python
def attach_context(self, ctx: ContextManager):
    """注入会话记忆，创建 SARHooks 绑定。"""
    self._context = ctx
    self.hooks = SARHooks(ctx)  # WorkerSARHooks 或 CoordinatorSARHooks
```

---

## 4. 自审：发现的问题与修正

### 4.1 DAG 模式也需要 context_id 传播

**问题**：原方案只覆盖 agentic 模式的 `_execute_agentic`。但 `_execute_dag_loop` → `_dispatch_layer`（agent_executor.py:810）也调 `push_task`，需要同步传 context_id。

**修正**：
- `_dispatch_layer` 接收 `context_id` 参数，传递给 `push_task`。
- `_execute_dag_loop` 从自身参数中取 `context_id` 传给 `_dispatch_layer`。
- SAR 使用 agentic 模式，DAG 模式为次要路径，但需一并改造以防不一致。

### 4.2 ContextManager 并发安全

**问题**：如果 coordinator 并发派发两个相同 context_id 的子任务到同一 worker，两个 execute() 调用共享同一 ContextManager，可能并发读写。

**修正**：
- AgentAdapter 增加 `asyncio.Lock` per context_id（或 per session）。
- SAR 中 barrier 串行化动作，并发概率低，但 A2A 层理论上可能并发 dispatch。
- 实现：`self._session_locks: dict[str, asyncio.Lock]`，execute() 入口获取锁。

### 4.3 pre_llm 会修改 self.messages（prune）

**问题**：如果 pre_llm 只返回组装后的列表但不修改 self.messages，self.messages 会无限增长，tiktoken 估算会越来越大。

**修正**：
- pre_llm hook 会调用 `ctx.prune_history(agent.messages)`，将超出 recent_window 的旧消息移入 episodic memory（摘要化）。
- self.messages 保持有界：system + user messages + 最近 N 条 assistant/tool。
- memory_block 不存入 self.messages，每次 pre_llm 时从 pinned + episodic 实时渲染。

### 4.4 非 SAR 场景的向后兼容

**问题**：`require_explicit_completion=True` 会让纯文本回复不退出 loop。非 SAR 场景（如通用 agent）需要文本回复即结束。

**修正**：
- 新增 `require_explicit_completion: bool = False` 参数，默认 False（向后兼容）。
- SAR 场景在 `_build_agent()` 时设 True。
- `hooks=None` 时所有 hook 不生效，`_summarize_messages()` 保留作为 fallback。

### 4.5 ToolResult 双副本一致性

**问题**：SAR 工具 import `router_agent.tools.base.ToolResult`，但 Worker Agent 类 import `worker_agent.tools.base.ToolResult`。Agent 检查 `result.task_complete` 时，实际访问的是 router_agent 的 ToolResult 实例。

**修正**：
- 两个 `base.py` 都加 `task_complete` 和 `mission_success` 字段（A7+A8）。
- 由于 pydantic BaseModel 鸭子类型兼容，字段一致即无问题。
- 文档中明确标注：SAR 工具统一使用 router_agent 的 ToolResult。

### 4.6 Coordinator finish_task 需要注入到 agentic executor

**问题**：coordinator 的 finish_task 不是普通 extra_tool（需要标记编排结束），需要加入 `_execute_agentic` 的 tools 列表。

**修正**：
- `FinishTaskTool` 在 coordinator 侧接收一个 `store: TaskStore` 或 callback，调用时标记 `store.mark_finished(success)`。
- 注入位置：`agent_executor.py:236-242` 的 tools 列表。

### 4.7 SAR_WORKER_TOOLS 需注册 finish_task

**问题**：worker 的 finish_task 需要在 `sar_orch/tools/worker/__init__.py` 的 `SAR_WORKER_TOOLS` 列表中注册。

**修正**：
- D1 阶段在 `__init__.py` 中添加 `FinishTaskTool` 到列表和 `__all__`。
- `sar_orch/worker.py:73-76` 的工具实例化逻辑需适配（finish_task 不需要 barrier 参数）。

### 4.8 token 估算一致性

**问题**：ContextManager 需要估算 token 来决定压缩时机。Agent 有 `_estimate_tokens()`（tiktoken cl100k_base），但 ContextManager 是独立对象。

**修正**：
- ContextManager 内部使用同样的 tiktoken 估算逻辑（复制 `_estimate_tokens` 的核心部分）。
- 或者 pre_llm hook 调用 `agent._estimate_tokens()` 获取当前估算值传给 ContextManager。
- 采用后者更简洁，避免重复代码。

---

## 5. 文件改动清单

### 新增文件

| 文件 | 内容 |
|------|------|
| `src/Agent/worker_agent/context.py` | ContextConfig + ContextManager(基类) + WorkerContextManager |
| `src/Agent/worker_agent/hooks.py` | AgentHooks Protocol + WorkerSARHooks |
| `src/Agent/router_agent/context.py` | ContextConfig + ContextManager(基类) + CoordinatorContextManager |
| `src/Agent/router_agent/hooks.py` | AgentHooks Protocol + CoordinatorSARHooks |
| `sar_orch/tools/worker/finish_task.py` | Worker 的 finish_task 工具 |
| `sar_orch/tools/coordinator/finish_task.py` | Coordinator 的 finish_task 工具 |

### 修改文件

| 文件 | 改动内容 |
|------|---------|
| `src/Agent/worker_agent/agent.py` | __init__ 加参数 + attach_context() + hooks 插入点 + RunResult 返回 + 退出逻辑 + _task_complete 状态 |
| `src/Agent/router_agent/agent.py` | 同步镜像 |
| `src/Agent/worker_agent/schema/schema.py` | +RunResult dataclass |
| `src/Agent/router_agent/schema/schema.py` | 同步 |
| `src/Agent/worker_agent/tools/base.py` | ToolResult +task_complete/mission_success |
| `src/Agent/router_agent/tools/base.py` | 同步（SAR 工具关键路径） |
| `src/a2a/worker/agent_adapter.py` | +sessions dict + session_locks + execute() 查会话 + RunResult 适配 + clear_sessions() |
| `src/a2a/coordinator/router.py` | push_task 加 context_id 参数 + Message 设 context_id + _build_agent 传 context 参数 + RunResult 适配 |
| `src/a2a/coordinator/agent_executor.py` | +sessions dict + _execute_agentic 查会话 + TaskStore 传 context_id + RunResult 适配 + finish_task 注入 |
| `src/a2a/coordinator/task_store.py` | +context_id 字段 |
| `src/a2a/builtin_tools/dispatch_task.py` | push_task 调用传 context_id |
| `src/a2a/coordinator/agent_executor.py:810` | _dispatch_layer 传 context_id（DAG 模式） |
| `sar_orch/tools/worker/__init__.py` | SAR_WORKER_TOOLS 加 FinishTaskTool |
| `sar_orch/tools/worker/no_op.py` | finished 时设 task_complete=True |
| `sar_orch/worker.py` | 注入 WorkerContextConfig + WorkerSARHooks + finish_task 工具适配 |
| `sar_orch/coordinator.py` | 注入 CoordinatorContextConfig + CoordinatorSARHooks + finish_task 工具 |
| `sar_orch/experiment.py` | finally 调 clear_sessions() |

---

## 6. 实施阶段

### 阶段 A：基础设施（不改运行时行为）

**目标**：搭建所有新文件和数据结构，hooks=None 时行为完全不变。

| # | 文件 | 内容 |
|---|------|------|
| A1 | `worker_agent/context.py` | ContextConfig + ContextManager 基类（pinned/episodic/recent 三层，assemble/observe/prune_history/_maybe_compress 接口）+ WorkerContextManager（pinned schema + _extract_pinned 骨架） |
| A2 | `router_agent/context.py` | 同步 + CoordinatorContextManager（pinned schema + _extract_pinned 骨架） |
| A3 | `worker_agent/hooks.py` | AgentHooks Protocol（7 个钩子）+ WorkerSARHooks（pre_llm→assemble, post_tool→observe, should_continue→task_complete） |
| A4 | `router_agent/hooks.py` | 同步 + CoordinatorSARHooks |
| A5 | `worker_agent/schema/schema.py` | +RunResult dataclass |
| A6 | `router_agent/schema/schema.py` | 同步 |
| A7 | `worker_agent/tools/base.py` | ToolResult +task_complete:bool=False, +mission_success:bool\|None=None |
| A8 | `router_agent/tools/base.py` | 同步（SAR 工具关键路径） |
| A9 | `worker_agent/agent.py` | __init__ 加 context_*/hooks/require_explicit_completion 参数 + attach_context() 方法 + _task_complete/_mission_success/_task_description 状态字段 + hooks 插入点（暂不改变退出逻辑，hooks=None 时走旧路径） |
| A10 | `router_agent/agent.py` | 同步镜像 |

**验证**：
```bash
uv run --with ruff ruff check src/ sar_orch/
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 3
```
- ruff 通过
- 实验跑通（hooks=None，向后兼容）
- 行为与改造前一致

### 阶段 B：Loop 改造 + RunResult

**目标**：run() 返回 RunResult，退出逻辑改为 task_complete 驱动，hooks 实际生效。

| # | 文件 | 内容 |
|---|------|------|
| B1 | 两份 `agent.py` | run() 返回 RunResult；退出逻辑改 task_complete 驱动 + nudge 重试（require_explicit_completion 控制）；hooks 各插入点实际调用 pre_llm/post_llm/pre_tool/post_tool/should_continue |
| B2 | `agent_adapter.py:110-114` | `result = await agent.run(...)` + `final_text = result.content` + 写 `result.success` 到 artifact metadata |
| B3 | `router.py:370,423` | `result = await agent.run()` + `self._parse_dag_result(result.content)` |
| B4 | `agent_executor.py:282` | `result = await agent.run(...)` + `final_text = result.content` + 读 `result.success` |

**验证**：
```bash
uv run --with ruff ruff check src/ sar_orch/
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 5
```
- RunResult 正常返回
- agent 不因纯文本退出（require_explicit_completion=True 时）
- token_usage.csv 正常记录

### 阶段 C：会话持久化 + context_id 传播

**目标**：context_id 贯通 coordinator→worker，AgentAdapter 维护会话存储。

| # | 文件 | 内容 |
|---|------|------|
| C1 | `router.py:636-656` | push_task 签名加 `context_id: str \| None = None`，Message 设 context_id |
| C2 | `task_store.py:44-58` | +`self.context_id: str` 字段 |
| C3 | `agent_executor.py:229` | TaskStore 初始化传 context_id |
| C4 | `dispatch_task.py:97` | `router.push_task(agent_id, prompt, context_id=self._store.context_id)` |
| C5 | `agent_executor.py:808-810` | _dispatch_layer 传 context_id（DAG 模式兼容） |
| C6 | `agent_adapter.py` | +`self._sessions: dict[str, ContextManager]` + `self._session_locks: dict[str, asyncio.Lock]` + execute() 查会话→attach_context→run + clear_sessions() |
| C7 | `agent_executor.py` | +`self._sessions` + _execute_agentic 查会话→注入编排 Agent |
| C8 | `experiment.py:246-260` | finally 调 `worker.clear_sessions()` / `coordinator.clear_sessions()` |
| C9 | `a2a_server.py:80` | AgentAdapter 构造时传入 context_config + token_limit |
| C10 | `sar_orch/worker.py` | 传 context_config 到 create_worker_a2a_server |
| C11 | `sar_orch/coordinator.py` | 传 context_config 到 create_server |

**验证**：
- 日志确认同 context_id 的多次 dispatch 复用同一 ContextManager 实例
- worker 跨子任务保留 pinned state（位置/库存不丢）

### 阶段 D：finish_task + SAR 接入

**目标**：finish_task 工具生效，pinned_state 提取规则完整实现，SAR 层全量接入。

| # | 文件 | 内容 |
|---|------|------|
| D1 | `sar_orch/tools/worker/finish_task.py` | FinishTaskTool(success, summary, task_description) → ToolResult(task_complete=True) |
| D2 | `sar_orch/tools/worker/__init__.py` | SAR_WORKER_TOOLS 加 FinishTaskTool + __all__ |
| D3 | `sar_orch/tools/worker/no_op.py` | finished 时设 task_complete=True（提示性） |
| D4 | `sar_orch/tools/coordinator/finish_task.py` | FinishTaskTool(success, summary) → 标记编排结束 |
| D5 | `agent_executor.py:236` | agentic tools 列表加 coordinator FinishTaskTool |
| D6 | `worker_agent/context.py` | WorkerContextManager._extract_pinned 完整实现（正则/JSON 提取 position/inventory/step/fires/persons/mission_status） |
| D7 | `router_agent/context.py` | CoordinatorContextManager._extract_pinned 完整实现（提取 global_snapshot/step_budget/dispatched_tasks/worker_results） |
| D8 | `sar_orch/worker.py` | require_explicit_completion=True + 注入 WorkerContextConfig + finish_task 工具不需要 barrier |
| D9 | `sar_orch/coordinator.py` | require_explicit_completion=True + 注入 CoordinatorContextConfig + finish_task 工具 |

**验证**：
```bash
# 先跑小场景冒烟
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 5

# 再跑完整场景
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42
```

---

## 7. 验证标准

### 7.1 定量指标

| 指标 | 期望 | 观测文件 |
|------|------|---------|
| PromptTokens 增长 | 不再无限线性增长，压缩后下降或趋平 | token_usage.csv |
| worker 重复 get_agent_state | 不因丢状态重复查询 | agent_interactions.csv |
| finish_task 调用 | 出现且带 task_description | agent_interactions.csv |
| RunResult.success | 正常完成=True，超时=None | agent_interactions.csv / summary.csv |
| 跨子任务记忆 | 第二次 dispatch 不重新探索已知区域 | agent_interactions.csv |
| coverage/transport | 不低于旧 baseline | summary.csv / trajectory.csv |

### 7.2 对照实验

建议支持环境变量控制策略：
```bash
AGENT_CONTEXT_STRATEGY=none      # 无压缩（对照基线）
AGENT_CONTEXT_STRATEGY=summary   # 旧 LLM 摘要策略
AGENT_CONTEXT_STRATEGY=hybrid    # 新三层记忆策略
```

同一组参数跑三次，比较 FinalCoverage / TransportRate / TotalTokens / PromptTokens 曲线。

### 7.3 测试场景

| 场景 | 命令 | 目的 |
|------|------|------|
| 冒烟 | `--scene 1 --agents 2 --seed 42 --max-steps 5` | 快速验证不崩溃 |
| 小场景 | `--scene 1 --agents 2 --seed 42` | 验证完整流程 |
| 长上下文 | `--scene 2 --agents 3 --seed 42` | 观察更长上下文压缩效果 |
| 压力测试 | `--scene 3 --agents 4 --seed 42` | 多 agent 协作下的记忆管理 |

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 双副本不同步 | worker/router 行为不一致 | 每次改动后 ruff check 两份文件，diff 对比 |
| pinned_state 提取正则不准 | 丢失关键状态 | 先从确定性高的工具提取（get_agent_state/navigate_to），逐步扩展 |
| finish_task 不被 LLM 调用 | agent 跑满 max_steps | nudge 机制 + system prompt 引导 + no_op 的 task_complete 提示 |
| context_id 未传播 | 会话不延续 | C 阶段验证日志确认 context_id 贯通 |
| tiktoken 估算偏差 | 压缩时机不准 | 双阈值触发（本地估算 + API total_tokens），trigger_ratio=0.8 留余量 |
| 并发写 ContextManager | 记忆损坏 | per-session asyncio.Lock |
| pre_llm prune 误删消息 | 丢失当前任务上下文 | 只删 recent_window 之外的 assistant/tool 消息，保留所有 user 消息 |

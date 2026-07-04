---
日期: 2026-07-03
文档类型: 接口设计文档
文档概述: Agent Build 模块接口设计，参考 DeepSeek-Reasonix boot.Build() 模式，
  为每个 Agent 子包提供单一组装函数，收敛当前分散的构造逻辑。
---

# Agent Build 模块接口设计

## 1. 动机

当前 Agent 构造逻辑散落在多个模块中，每新增一个前端就要重复拼装代码：

```
src/a2a/worker/agent_adapter.py    ← LLMClient + Agent + Controller 创建
src/a2a/coordinator/agent_executor.py ← Controller 创建
src/a2a/coordinator/router.py       ← RouterAgent._build_agent()
sar_orch/worker.py                  ← SAR 工具绑定 + AgentAdapter 初始化
sar_orch/coordinator.py             ← QuerySARStateTool + create_server()
```

目标：为 `worker_agent` 和 `router_agent` 各提供一个 **`build.py`**，将 LLM 客户端创建、
工具组装、System Prompt 加载、ContextManager 创建、Agent 和 Controller 实例化
归纳到单一组装函数中。上层只需传入 `Options` dataclass，不再关心内部拼装细节。

---

## 2. Worker Agent: `src/Agent/worker_agent/build.py`

### 2.1 Options 定义

```python
@dataclass
class AgentBuildOptions:
    """构建 Agent 实例的全部参数。"""

    # LLM
    model: str
    provider: str                          # "anthropic" | "openai"
    api_base: str
    api_key: str
    temperature: float = 0.0               # 0.0 = 使用 provider 默认

    # Prompt
    system_prompt: str                     # 最终 system prompt 文本

    # Tools
    tools: list[Tool]                      # 领域工具（SAR NavigateTo 等）
    extra_tools: list[Tool] | None = None  # 运行时注入的额外工具
    include_base_tools: bool = True        # 是否加入 ReadTool/WriteTool/BashTool

    # Agent 参数
    max_steps: int = 50
    workspace_dir: str = "./workspace"
    token_limit: int = 80000
    log_dir: str | Path | None = None

    # 上下文管理
    context_strategy: str = "hybrid"
    context_recent_messages: int = 12
    context_summary_trigger_ratio: float = 0.8
    context_pinned_enabled: bool = True

    # 完成语义
    require_explicit_completion: bool = False


@dataclass
class ControllerBuildOptions:
    """构建 AgentController 的全部参数。"""

    agent: AgentBuildOptions
    context_config: ContextConfig | None = None
    token_limit: int = 80000
    require_explicit_completion: bool = False
```

### 2.2 组装函数

```python
def build_agent(opts: AgentBuildOptions) -> Agent:
    """单点组装函数：创建 LLMClient → 组装 tools → 返回 Agent 实例。

    LLMClient 由 model/provider/api_base/api_key 创建。
    tools 组装顺序：base_tools（若 include_base_tools）+ opts.tools + opts.extra_tools。
    """

def build_controller(
    opts: ControllerBuildOptions,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> AgentController:
    """单点组装函数：build_agent 包一层工厂 → 挂 session_factory → 返回 Controller。

    默认 session_factory 创建 WorkerContextManager，调用方可传自定义工厂。
    agent_factory 不接受运行时 kwargs（Worker 端无此需求）。
    """
```

### 2.3 辅助函数

```python
def load_system_prompt(
    prompts_dir: Path | None,
    *,
    override: str | None = None,
    append: str | None = None,
    default: str = "",
) -> str:
    """从 prompts_dir/system.md 加载 prompt。

    加载优先级：override > file > default。
    返回最终文本，附带可选的 append 内容。
    """

def load_custom_tools(
    tools_dir: Path | None,
    *,
    module_prefix: str = "_a2a_worker_tool_",
) -> list[Tool]:
    """从 tools_dir/*.py 动态导入 Tool 实例。

    跳过以 _ 开头的文件。动态加载的模块使用 module_prefix 命名
    以避免导入冲突。
    """

def discover_skills_dir(scope: str = "worker") -> Path | None:
    """从约定路径发现 skills 目录。

    搜索顺序：cwd/skills/{scope}/ → 包目录下的 skills/{scope}/。
    """

def build_worker_context_manager(
    config: ContextConfig | None = None,
    token_limit: int = 80000,
) -> WorkerContextManager:
    """快捷创建 WorkerContextManager。"""
```

### 2.4 调用示例

```python
# 改前（agent_adapter.py）
self._controller = AgentController(
    agent_factory=self._build_agent,
    session_factory=lambda: WorkerContextManager(
        self._context_config, self._token_limit
    ),
)

def _build_agent(self):
    llm = LLMClient(...)
    tools = [ReadTool(), WriteTool()]
    tools.extend(self._extra_tools)
    ...
    return Agent(llm_client=llm, system_prompt=..., tools=tools, ...)

# 改后
from Agent.worker_agent.build import (
    build_controller,
    load_system_prompt,
    ControllerBuildOptions,
    AgentBuildOptions,
)

self._controller = build_controller(ControllerBuildOptions(
    agent=AgentBuildOptions(
        model=self._model,
        provider=self._provider,
        api_base=self._api_base,
        api_key=os.environ.get(self._api_key_env, ""),
        temperature=self._temperature,
        system_prompt=load_system_prompt(
            self._prompts_dir, override=self._system_prompt
        ),
        tools=list(self._extra_tools),
        include_base_tools=self._include_base_tools,
        max_steps=self._max_steps,
        workspace_dir=str(self._workspace_dir),
        log_dir=self._log_dir,
        token_limit=self._token_limit,
        require_explicit_completion=self._require_explicit_completion,
    ),
    context_config=self._context_config,
    token_limit=self._token_limit,
    require_explicit_completion=self._require_explicit_completion,
))
```

---

## 3. Router Agent: `src/Agent/router_agent/build.py`

### 3.1 Options 定义

```python
@dataclass
class RouterBuildOptions:
    """构建 Router Agent 实例的全部参数。"""

    # LLM
    model: str
    provider: str
    api_base: str
    api_key: str
    temperature: float = 0.0

    # Prompt
    system_prompt: str                     # 最终 system prompt（内置默认或从文件加载）

    # Tools —— 三源合并（见 3.4 的合并规则）
    builtin_tools: list[Tool] | None = None  # 内置工具（默认 QueryWorkersTool）
    custom_tools: list[Tool] | None = None   # 持久化工具（从 custom_tools_dir 加载）
    extra_tools: list[Tool] | None = None    # 构造时注入的工具

    # Agent 参数
    max_steps: int = 15
    workspace_dir: str = "./workspace/coordinator"
    token_limit: int = 80000
    log_dir: str | Path | None = None

    # Skills
    skills_dir: str | Path | None = None

    # 上下文管理（当前 RouterAgent._build_agent 未传递这些参数，
    # build_router_agent 将首次正确传递它们 —— 行为改善）
    context_strategy: str = "hybrid"
    context_recent_messages: int = 12
    context_summary_trigger_ratio: float = 0.8
    context_pinned_enabled: bool = True

    require_explicit_completion: bool = False


@dataclass
class RouterControllerBuildOptions:
    """构建 Router Controller 的全部参数。"""

    agent: RouterBuildOptions
    context_config: ContextConfig | None = None
    token_limit: int = 80000
    require_explicit_completion: bool = False
```

### 3.2 组装函数

```python
def build_router_agent(opts: RouterBuildOptions) -> Agent:
    """单点组装函数：LLMClient → 三源合并 tools → 返回 Agent。

    与当前 RouterAgent._build_agent 的差异：
    - 首次传递 context_strategy / context_recent_messages 等上下文参数
    - tools 三源合并顺序见 3.4
    - 支持 builtin_tools 替换默认的 QueryWorkersTool
    """

def build_router_controller(
    opts: RouterControllerBuildOptions,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> AgentController:
    """单点组装函数：返回 AgentController，agent_factory 自动处理运行时 kwargs。

    AgentController.submit() 可能传入 extra_tools 和 system_prompt_override（
    由 CoordinatorAgentExecutor.execute() 传入）。内部 agent_factory 会合并
    这些运行时参数到 opts 中再调用 build_router_agent。
    """

def build_coordinator_context_manager(
    config: ContextConfig | None = None,
    token_limit: int = 80000,
) -> CoordinatorContextManager:
    """快捷创建 CoordinatorContextManager。"""
```

### 3.3 运行时 kwargs 合并机制

`AgentController.submit()` 通过 `**factory_kwargs` 向 agent_factory 传递
`extra_tools` 和 `system_prompt_override`。`build_router_controller` 内部
的默认 agent_factory 必须合并这些运行时参数：

```python
# build_router_controller 内部（伪代码）
def _make_agent_factory(base_opts: RouterBuildOptions) -> Callable:
    def factory(**kwargs) -> Agent:
        # 浅拷贝 opts，合并运行时覆盖
        merged = dataclasses.replace(base_opts)
        if "extra_tools" in kwargs:
            merged.extra_tools = (merged.extra_tools or []) + list(kwargs["extra_tools"])
        if "system_prompt_override" in kwargs:
            merged.system_prompt = kwargs["system_prompt_override"]
        return build_router_agent(merged)
    return factory
```

这样 `CoordinatorAgentExecutor.execute()` 传入的 `extra_tools=[dispatch_tool]`
和 `system_prompt_override=agentic_prompt` 就能正确覆盖到每次请求。

### 3.4 Tools 三源合并规则

`build_router_agent` 内部 tools 合并顺序：

```python
tools = []
tools.extend(opts.builtin_tools or [QueryWorkersTool(registry)])  # 内置
tools.extend(opts.custom_tools or [])                              # 持久化
tools.extend(opts.extra_tools or [])                               # 构造时注入
```

与当前 `RouterAgent._build_agent` 行为一致，但更清晰：
- `builtin_tools` = 当前 router.py 的 `[QueryWorkersTool(self._registry)]`
- `custom_tools` = 当前 router.py 的 `self._custom_tools`
- `extra_tools` = 当前 router.py 的 `self._extra_tools`（构造函数注入）
- 运行时 `extra_tools` kwargs ≠ opts.extra_tools，由 `_make_agent_factory` 合并

### 3.5 调用示例

```python
# 改前（agent_executor.py）
self._controller = AgentController(
    agent_factory=lambda **kw: self._router._build_agent(**kw),
    session_factory=lambda: CoordinatorContextManager(
        self._context_config, self._token_limit
    ),
)

# 改后
from Agent.router_agent.build import (
    build_router_controller,
    RouterControllerBuildOptions,
    RouterBuildOptions,
)

self._controller = build_router_controller(RouterControllerBuildOptions(
    agent=RouterBuildOptions(
        model=self._router._model,
        provider=self._router._provider,
        api_base=self._router._api_base,
        api_key=os.environ.get(self._router._api_key_env, ""),
        temperature=self._router._temperature,
        system_prompt=self._router._system_prompt,
        builtin_tools=None,  # 使用默认的 QueryWorkersTool
        custom_tools=self._router._custom_tools,
        extra_tools=self._router._extra_tools,
        max_steps=self._router._max_steps,
        workspace_dir=str(self._router._workspace_dir),
        log_dir=self._router._log_dir,
        skills_dir=self._router._skills_dir,
        context_strategy="hybrid",
        context_recent_messages=12,
        context_summary_trigger_ratio=0.8,
        context_pinned_enabled=True,
        require_explicit_completion=self._require_explicit_completion,
    ),
    context_config=self._context_config,
    token_limit=self._token_limit,
    require_explicit_completion=self._require_explicit_completion,
))
```

---

## 4. 迁移路径

### 阶段一：新增 build.py（不修改现有代码）

```
src/Agent/worker_agent/
├── build.py          ← 新增，纯新文件
├── agent.py          ← 不动
├── context.py
└── ...

src/Agent/router_agent/
├── build.py          ← 新增，纯新文件
├── agent.py          ← 不动
├── context.py
└── ...
```

### 阶段二：逐个替换调用方

| 调用方 | 旧方式 | 新方式 |
|--------|--------|--------|
| `agent_adapter.py` | `self._build_agent()` + `AgentController(...)` | `build_controller(opts)` |
| `agent_executor.py` | `AgentController(agent_factory=lambda... )` | `build_router_controller(opts)` |
| `sar_orch/worker.py` | 创建工具 → `AgentAdapter(...)` | 创建工具 → `build_controller(opts)` |
| `sar_orch/coordinator.py` | `create_server(extra_tools=...)` | `create_server(agent_opts=...)` |

### 阶段三：移除冗余代码

- `AgentAdapter._build_agent()` 方法体 → 委托给 `worker_agent.build.build_agent()`
- `RouterAgent._build_agent()` 方法体 → 委托给 `router_agent.build.build_router_agent()`
- `AgentAdapter.__init__()` 中的 `AgentController(...)` → 委托给 `build_controller()`

---

## 5. 不变的设计约束

1. **AgentController 不 import a2a** — build.py 也一样，只依赖 `Agent.*` 内核
2. **Agent 类构造函数不改变** — build.py 只是外部编排，不修改现有类的接口
3. **调用方仍可传自定义 factory** — `build_controller()` 的 `session_factory` 参数为可选覆盖
4. **SAR 工具绑定在调用方完成** — build.py 不感知 barrier，工具列表由上层（`sar_orch/`）传入
5. **Worker 端无运行时 kwargs** — Worker 的 AgentController 不接受 extra_tools 等运行时覆盖，与现状一致

---

## 6. 接口全景

```
                    ┌─────────────────────────────────────┐
                    │           上层调用方                 │
                    │  sar_orch/worker.py                 │
                    │  a2a/worker/agent_adapter.py        │
                    │  a2a/coordinator/agent_executor.py  │
                    └──────────┬──────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│  build_agent()  │   │ build_router_   │   │ load_system_    │
│  build_         │   │ agent()         │   │ prompt()        │
│  controller()   │   │ build_router_   │   │ load_custom_    │
│                 │   │ controller()    │   │ tools()         │
│  worker_agent/  │   │  (+ 内部运行时   │   │ discover_       │
│  build.py       │   │    kwargs 合并)  │   │ skills_dir()   │
│                 │   │                 │   │                 │
└────────┬────────┘   └────────┬────────┘   └────────┬────────┘
         │                     │                      │
         ▼                     ▼                      │
┌─────────────────┐   ┌─────────────────┐             │
│  Agent (worker) │   │  Agent (router) │             │
│  WorkerContext  │   │  Coordinator    │             │
│  Manager        │   │  ContextManager │             │
│  AgentController│   │  AgentController│             │
└─────────────────┘   └─────────────────┘             │
         │                     │                      │
         └─────────────────────┼──────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   agent.run()       │
                    │   ReAct Loop        │
                    └─────────────────────┘
```

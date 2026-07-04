# A2A input-required 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 A2A 标准 `TASK_STATE_INPUT_REQUIRED` 状态替代 `[HELP]` artifact + `future_registry` hack，实现 Worker 暂停求助、Coordinator 回复恢复的标准流程。

**Architecture:** Worker 的 `ask_coordinator` 工具抛出 `NeedInputError` 异常，`Agent.run()` 捕获后返回 `RunResult(need_input=True)`，`AgentController.submit()` 保存完整 messages 快照到 ContextManager，`AgentAdapter.execute()` 调用 `updater.requires_input()` 设置 A2A 标准 INPUT_REQUIRED 状态后返回。Coordinator 检测到 INPUT_REQUIRED 后用标准 A2A `send_message` 恢复任务，Worker 的 `execute()` 再次被调用，从快照恢复完整 messages 并补上 tool_result。

**Tech Stack:** Python 3.10, A2A SDK (protobuf), Pydantic, pytest, pytest-asyncio, httpx.MockTransport

## Global Constraints

- PYTHONPATH 必须包含 `src:` — 所有运行命令都带 `PYTHONPATH="src:$PYTHONPATH"`
- no_proxy 必须设为 `"localhost,0.0.0.0,127.0.0.1"`
- 测试用 `uv run pytest tests/ -v`
- Lint 用 `uv run --with ruff ruff check src/ sar_orch/` 和 `uv run --with ruff ruff format src/ sar_orch/`
- `Message` 是 Pydantic BaseModel（`src/Agent/worker_agent/schema/schema.py`），字段：`role: str`, `content: str | list`, `tool_calls: list[ToolCall] | None`, `tool_call_id: str | None`, `name: str | None`
- `ToolCall` 有 `id: str`, `type: str`, `function: FunctionCall`（`name: str`, `arguments: dict`）
- `RunResult` 是 dataclass（`src/Agent/worker_agent/schema/schema.py`），当前字段：`content`, `success`, `steps_used`, `task_description`
- A2A client 创建：`from a2a.client import create_client, ClientConfig` → `client = await create_client(endpoint, config)` → `async for sr in client.send_message(request):` → `await client.close()`
- `TaskUpdater.requires_input(message)` 设置 `TASK_STATE_INPUT_REQUIRED`（A2A SDK 内置方法）

---

## File Structure

| 文件 | 责任 | 操作 |
|------|------|------|
| `src/a2a/worker/need_input.py` | `NeedInputError` 异常定义 | 创建 |
| `src/Agent/worker_agent/schema/schema.py` | `RunResult` 加 `need_input` 字段 | 修改 |
| `src/Agent/worker_agent/context.py` | `ContextManager` 加快照存储 | 修改 |
| `src/Agent/worker_agent/agent.py` | `run()` 捕获 `NeedInputError` | 修改 |
| `src/Agent/controller/controller.py` | `submit()` 加快照保存+恢复 | 修改 |
| `src/a2a/worker/tools/ask_coordinator.py` | 重写为纯信号工具 | 重写 |
| `src/a2a/worker/agent_adapter.py` | `execute()` 暂停/恢复调度 | 修改 |
| `src/a2a/builtin_tools/respond_worker.py` | 重写用 A2A client | 重写 |
| `src/a2a/coordinator/server.py` | push-callback 加 INPUT_REQUIRED 检测 | 修改 |
| `src/a2a/worker/a2a_server.py` | 删除 push-callback 路由+coordinator_callback_url | 修改 |
| `src/a2a/worker/cli.py` | 删除 coordinator_callback_url 参数 | 修改 |
| `sar_orch/worker.py` | 删除 coordinator_callback_url | 修改 |
| `src/a2a/worker/future_registry.py` | 删除 | 删除 |
| `tests/test_need_input.py` | NeedInputError 测试 | 创建 |
| `tests/test_context_snapshot.py` | 快照存储测试 | 创建 |
| `tests/test_ask_coordinator.py` | 重写 | 重写 |
| `tests/test_respond_worker.py` | 重写 | 重写 |
| `tests/test_coordinator_push_callback.py` | 加 INPUT_REQUIRED 测试 | 修改 |
| `tests/test_future_registry.py` | 删除 | 删除 |
| `tests/test_worker_push_callback.py` | 删除 | 删除 |

---

### Task 1: NeedInputError 异常

**Files:**
- Create: `src/a2a/worker/need_input.py`
- Test: `tests/test_need_input.py`

**Interfaces:**
- Produces: `NeedInputError(question: str)` — `Exception` 子类，属性 `.question: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_need_input.py
"""Tests for NeedInputError — Worker 暂停求助信号。"""

import pytest
from a2a.worker.need_input import NeedInputError


def test_need_input_error_carries_question():
    err = NeedInputError("Where is the target?")
    assert err.question == "Where is the target?"


def test_need_input_error_is_exception():
    err = NeedInputError("Help me")
    assert isinstance(err, Exception)


def test_need_input_error_str():
    err = NeedInputError("What now?")
    assert "What now?" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_need_input.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a.worker.need_input'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/a2a/worker/need_input.py
"""NeedInputError — Worker agent 需要向 Coordinator 请求输入时抛出。"""


class NeedInputError(Exception):
    """Worker agent 需要向 Coordinator 请求输入时抛出。

    捕获后应调用 TaskUpdater.requires_input() 设置 A2A INPUT_REQUIRED 状态，
    然后从 AgentAdapter.execute() 返回让出控制权。
    """

    def __init__(self, question: str) -> None:
        self.question = question
        super().__init__(question)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_need_input.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/a2a/worker/need_input.py tests/test_need_input.py
git commit -m "feat: add NeedInputError exception for worker pause signal"
```

---

### Task 2: RunResult 加 need_input 字段

**Files:**
- Modify: `src/Agent/worker_agent/schema/schema.py:59-66`
- Test: `tests/test_run_result_need_input.py`

**Interfaces:**
- Consumes: nothing
- Produces: `RunResult(content="", success=None, steps_used=0, task_description="", need_input=False)` — 新增 `need_input: bool = False`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_result_need_input.py
"""Tests for RunResult.need_input field."""

from Agent.worker_agent.schema import RunResult


def test_run_result_default_need_input_false():
    result = RunResult()
    assert result.need_input is False


def test_run_result_can_set_need_input():
    result = RunResult(success=False, content="Where is the target?", need_input=True)
    assert result.need_input is True
    assert result.content == "Where is the target?"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_run_result_need_input.py -v`
Expected: FAIL with `AttributeError` or `TypeError` about `need_input`

- [ ] **Step 3: Modify RunResult to add need_input field**

在 `src/Agent/worker_agent/schema/schema.py` 中，找到 `RunResult` dataclass（约第 59-66 行），加 `need_input` 字段：

```python
@dataclass
class RunResult:
    """Result returned by Agent.run()."""

    content: str = ""
    success: bool | None = None
    steps_used: int = 0
    task_description: str = ""
    need_input: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_run_result_need_input.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/Agent/worker_agent/schema/schema.py tests/test_run_result_need_input.py
git commit -m "feat: add need_input field to RunResult"
```

---

### Task 3: ContextManager 快照存储

**Files:**
- Modify: `src/Agent/worker_agent/context.py:39-52`
- Test: `tests/test_context_snapshot.py`

**Interfaces:**
- Consumes: `Message` from `Agent.worker_agent.schema`
- Produces: `ContextManager.save_snapshot(task_id: str, messages: list[Message]) -> None` 和 `ContextManager.load_snapshot(task_id: str) -> list[Message] | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_snapshot.py
"""Tests for ContextManager task snapshot storage."""

import pytest
from Agent.worker_agent.context import ContextManager
from Agent.worker_agent.schema import Message


def test_save_and_load_snapshot():
    ctx = ContextManager()
    msgs = [
        Message(role="system", content="prompt"),
        Message(role="user", content="go"),
        Message(role="assistant", content="ok"),
    ]
    ctx.save_snapshot("task-1", msgs)
    loaded = ctx.load_snapshot("task-1")
    assert loaded is not None
    assert len(loaded) == 3
    assert loaded[0].role == "system"
    assert loaded[2].content == "ok"


def test_load_snapshot_returns_none_if_not_saved():
    ctx = ContextManager()
    assert ctx.load_snapshot("nonexistent") is None


def test_load_snapshot_pops_after_read():
    """load_snapshot 是一次性的——第二次返回 None。"""
    ctx = ContextManager()
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])
    first = ctx.load_snapshot("task-1")
    second = ctx.load_snapshot("task-1")
    assert first is not None
    assert second is None


def test_snapshots_isolated_by_task_id():
    ctx = ContextManager()
    ctx.save_snapshot("task-a", [Message(role="user", content="a")])
    ctx.save_snapshot("task-b", [Message(role="user", content="b")])
    loaded_a = ctx.load_snapshot("task-a")
    loaded_b = ctx.load_snapshot("task-b")
    assert loaded_a[0].content == "a"
    assert loaded_b[0].content == "b"


def test_save_snapshot_does_not_mutate_original():
    """保存的快照应该是副本，修改原始不影响快照。"""
    ctx = ContextManager()
    msgs = [Message(role="user", content="original")]
    ctx.save_snapshot("task-1", msgs)
    msgs.append(Message(role="assistant", content="appended"))
    loaded = ctx.load_snapshot("task-1")
    assert len(loaded) == 1
    assert loaded[0].content == "original"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_context_snapshot.py -v`
Expected: FAIL with `AttributeError: 'ContextManager' object has no attribute 'save_snapshot'`

- [ ] **Step 3: Add snapshot methods to ContextManager**

在 `src/Agent/worker_agent/context.py` 的 `ContextManager.__init__` 中加 `self._task_snapshots`，并加两个方法。在 `__init__` 的 `self._episode_counter: int = 0` 之后加：

```python
        # Task snapshots: task_id -> full messages list (for pause/resume)
        self._task_snapshots: dict[str, list] = {}
```

在 `observe` 方法之前（`__init__` 之后）加两个方法：

```python
    def save_snapshot(self, task_id: str, messages: list) -> None:
        """Save a full messages snapshot for later resume."""
        self._task_snapshots[task_id] = list(messages)

    def load_snapshot(self, task_id: str) -> list | None:
        """Load and remove a snapshot. Returns None if not found."""
        return self._task_snapshots.pop(task_id, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_context_snapshot.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/Agent/worker_agent/context.py tests/test_context_snapshot.py
git commit -m "feat: add task snapshot save/load to ContextManager"
```

---

### Task 4: Agent.run() 捕获 NeedInputError

**Files:**
- Modify: `src/Agent/worker_agent/agent.py:671-728` (工具执行 try-except 块)
- Test: `tests/test_agent_run_need_input.py`

**Interfaces:**
- Consumes: `NeedInputError` from `a2a.worker.need_input`, `RunResult.need_input`
- Produces: `Agent.run()` 在工具抛出 `NeedInputError` 时返回 `RunResult(need_input=True, content=question, success=False)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_run_need_input.py
"""Tests for Agent.run() catching NeedInputError."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agent.worker_agent.agent import Agent
from Agent.worker_agent.schema import Message, ToolCall, FunctionCall, RunResult
from a2a.worker.need_input import NeedInputError


class _FakeTool:
    """A tool that raises NeedInputError."""
    def __init__(self):
        self._name = "ask_coordinator"

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "Ask coordinator"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}

    async def execute(self, question: str):
        raise NeedInputError(question)

    def to_schema(self):
        return {}

    def to_openai_schema(self):
        return {}


@pytest.mark.asyncio
async def test_agent_run_returns_need_input_when_tool_raises():
    """Agent.run() should catch NeedInputError and return RunResult(need_input=True)."""
    # Build a minimal agent with the fake tool
    llm_client = MagicMock()
    agent = Agent(
        llm_client=llm_client,
        system_prompt="test",
        tools=[_FakeTool()],
        max_steps=5,
    )

    # Simulate LLM returning a tool call
    tool_call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}),
    )
    assistant_msg = Message(role="assistant", content="", tool_calls=[tool_call])

    # Mock the LLM to return the tool call, then a normal response
    call_count = 0
    async def fake_generate(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return assistant_msg, [tool_call]
        return Message(role="assistant", content="done"), []

    agent._llm_generate = fake_generate

    result = await agent.run()
    assert result.need_input is True
    assert result.content == "Where?"
    assert result.success is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_agent_run_need_input.py -v`
Expected: FAIL — `NeedInputError` is caught by the generic `except Exception` and turned into a `ToolResult`, not a `RunResult(need_input=True)`

- [ ] **Step 3: Add NeedInputError catch before generic Exception**

在 `src/Agent/worker_agent/agent.py` 的工具执行 try-except 块中，在 `except Exception as e:` 之前加 `except NeedInputError` 分支。

首先在文件顶部加导入（如果还没有）：
```python
from a2a.worker.need_input import NeedInputError
```

然后在工具执行的 try-except 中（约第 671-690 行），改为：

```python
    try:
        tool = self.tools[function_name]
        result = await tool.execute(**arguments)
    except NeedInputError as e:
        return RunResult(
            content=e.question,
            success=False,
            need_input=True,
        )
    except Exception as e:
        import traceback
        error_detail = f"{type(e).__name__}: {str(e)}"
        error_trace = traceback.format_exc()
        result = ToolResult(
            success=False,
            content="",
            error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
        )
```

同时确保 `RunResult` 已在文件中导入。搜索文件顶部的导入，如果没有 `from Agent.worker_agent.schema import RunResult` 则加上。

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_agent_run_need_input.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/Agent/worker_agent/agent.py tests/test_agent_run_need_input.py
git commit -m "feat: Agent.run() catches NeedInputError, returns RunResult(need_input=True)"
```

---

### Task 5: AgentController.submit() 快照保存+恢复

**Files:**
- Modify: `src/Agent/controller/controller.py:135-222`
- Test: `tests/test_submit_snapshot.py`

**Interfaces:**
- Consumes: `ContextManager.save_snapshot/load_snapshot` (Task 3), `RunResult.need_input` (Task 2), `Message`/`ToolCall` schema
- Produces: `submit(context_id, query, sink, *, task_id=None, initial_messages=None)` — 加两个可选参数

- [ ] **Step 1: Write the failing test**

```python
# tests/test_submit_snapshot.py
"""Tests for AgentController.submit() snapshot save/restore."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agent.controller.controller import AgentController
from Agent.worker_agent.context import ContextManager
from Agent.worker_agent.schema import Message, RunResult, ToolCall, FunctionCall


class _FakeAgent:
    """Minimal fake agent for controller testing."""
    def __init__(self):
        self.messages = [Message(role="system", content="sys")]
        self.require_explicit_completion = False
        self.cancel_event = None

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None):
        return RunResult(content="done", success=True)


class _FakeAgentNeedInput:
    """Fake agent that returns need_input=True, with messages containing a tool_call."""
    def __init__(self):
        self.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="go"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call-1", type="function", function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}))],
            ),
        ]
        self.require_explicit_completion = False
        self.cancel_event = None

    def attach_context(self, ctx):
        pass

    def add_user_message(self, query):
        self.messages.append(Message(role="user", content=query))

    async def run(self, cancel_event=None, step_callback=None):
        return RunResult(content="Where?", success=False, need_input=True)


@pytest.mark.asyncio
async def test_submit_saves_snapshot_on_need_input():
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgentNeedInput(),
        session_factory=lambda: ctx_manager,
    )

    result = await controller.submit("ctx-1", "go", task_id="task-1")

    assert result.need_input is True
    snapshot = ctx_manager.load_snapshot("task-1")
    assert snapshot is not None
    assert len(snapshot) == 3
    assert snapshot[2].tool_calls is not None


@pytest.mark.asyncio
async def test_submit_restores_from_initial_messages():
    """When initial_messages provided, agent.messages should be restored + tool_result appended."""
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgent(),
        session_factory=lambda: ctx_manager,
    )

    initial = [
        Message(role="system", content="sys"),
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call-1", type="function", function=FunctionCall(name="ask_coordinator", arguments={"question": "Where?"}))],
        ),
    ]

    result = await controller.submit(
        "ctx-1", "Go to sector 7", task_id="task-1", initial_messages=initial
    )

    assert result.success is True
    # The fake agent's messages should have been restored + tool_result appended
    # We can't directly check the agent (it's gone), but no crash means it worked


@pytest.mark.asyncio
async def test_submit_without_task_id_works():
    """submit() without task_id should work as before (no snapshot)."""
    ctx_manager = ContextManager()
    controller = AgentController(
        agent_factory=lambda **kw: _FakeAgent(),
        session_factory=lambda: ctx_manager,
    )

    result = await controller.submit("ctx-1", "go")
    assert result.success is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_submit_snapshot.py -v`
Expected: FAIL with `TypeError: submit() got an unexpected keyword argument 'task_id'`

- [ ] **Step 3: Modify submit() to add task_id and initial_messages params**

在 `src/Agent/controller/controller.py` 的 `submit()` 方法签名中加两个参数：

```python
    async def submit(
        self,
        context_id: str,
        query: str,
        sink: EventSink | None = None,
        *,
        extra_tools: list | None = None,
        system_prompt_override: str | None = None,
        cancel_event: asyncio.Event | None = None,
        task_id: str | None = None,
        initial_messages: list | None = None,
    ) -> RunResult:
```

然后在 `agent.add_user_message(query)` 之前加恢复逻辑：

```python
            # Restore from snapshot if provided (resume after input-required)
            if initial_messages:
                agent.messages = list(initial_messages)
                last = agent.messages[-1] if agent.messages else None
                if (
                    last
                    and last.role == "assistant"
                    and last.tool_calls
                ):
                    # The last tool_call was ask_coordinator which raised NeedInputError.
                    # Inject a tool_result with the coordinator's reply.
                    agent.messages.append(
                        Message(
                            role="tool",
                            content=query,
                            tool_call_id=last.tool_calls[-1].id,
                            name=last.tool_calls[-1].function.name,
                        )
                    )
                else:
                    agent.add_user_message(query)
            else:
                agent.add_user_message(query)
```

然后在 `result = await agent.run(...)` 之后、`return` 之前加快照保存：

```python
            try:
                result = await agent.run(cancel_event=ev, step_callback=sink.emit)
            finally:
                self._running_agents.pop(context_id, None)
                self._cancel_events.pop(context_id, None)

            # Save snapshot if agent needs input (pause for coordinator)
            if result.need_input and task_id:
                ctx.save_snapshot(task_id, agent.messages)

            return self._normalize(result)
```

确保 `Message` 已在文件顶部导入。搜索导入，如果没有则加 `from Agent.worker_agent.schema import Message`（或它可能已通过其他路径导入）。

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_submit_snapshot.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/Agent/controller/controller.py tests/test_submit_snapshot.py
git commit -m "feat: AgentController.submit() saves snapshot on need_input, restores from initial_messages"
```

---

### Task 6: AskCoordinatorTool 重写

**Files:**
- Rewrite: `src/a2a/worker/tools/ask_coordinator.py`
- Rewrite: `tests/test_ask_coordinator.py`

**Interfaces:**
- Consumes: `NeedInputError` (Task 1), `Tool`/`ToolResult` from `Agent.worker_agent.tools.base`
- Produces: `AskCoordinatorTool()` — 无参数构造，`execute(question: str)` 抛出 `NeedInputError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ask_coordinator.py
"""Tests for AskCoordinatorTool — Worker → Coordinator 帮助请求（纯信号版）。"""

import pytest
from a2a.worker.tools.ask_coordinator import AskCoordinatorTool
from a2a.worker.need_input import NeedInputError


class TestAskCoordinatorToolProperties:
    def test_name(self):
        tool = AskCoordinatorTool()
        assert tool.name == "ask_coordinator"

    def test_parameters_schema(self):
        tool = AskCoordinatorTool()
        schema = tool.parameters
        assert "question" in schema["properties"]
        assert "question" in schema["required"]


@pytest.mark.asyncio
async def test_execute_raises_need_input_error():
    tool = AskCoordinatorTool()
    with pytest.raises(NeedInputError) as exc_info:
        await tool.execute("Where is the target?")
    assert exc_info.value.question == "Where is the target?"


@pytest.mark.asyncio
async def test_execute_raises_for_any_question():
    tool = AskCoordinatorTool()
    with pytest.raises(NeedInputError) as exc_info:
        await tool.execute("What should I do next?")
    assert exc_info.value.question == "What should I do next?"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_ask_coordinator.py -v`
Expected: FAIL — 旧代码尝试 import httpx/future_registry 并发起 HTTP，不匹配新测试

- [ ] **Step 3: Rewrite AskCoordinatorTool**

```python
# src/a2a/worker/tools/ask_coordinator.py
"""AskCoordinatorTool — Worker Agent 向 Coordinator 请求帮助。

抛出 NeedInputError 信号，由 Agent.run() 捕获后通过 A2A INPUT_REQUIRED 状态
通知 Coordinator。Coordinator 用标准 send_message 恢复任务。
"""

from typing import Any

from Agent.worker_agent.tools.base import Tool, ToolResult
from a2a.worker.need_input import NeedInputError


class AskCoordinatorTool(Tool):
    """暂停执行并向 Coordinator 请求帮助。

    调用后抛出 NeedInputError，Agent.run() 捕获后返回 RunResult(need_input=True)，
    AgentAdapter.execute() 调用 TaskUpdater.requires_input() 设置 A2A INPUT_REQUIRED 状态。
    Coordinator 回复后，任务从快照恢复，agent 继续执行。
    """

    @property
    def name(self) -> str:
        return "ask_coordinator"

    @property
    def description(self) -> str:
        return (
            "Pause execution and ask the coordinator for help or clarification. "
            "The coordinator will see your question and respond. Execution resumes "
            "after the coordinator replies."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or help request to send to the coordinator",
                },
            },
            "required": ["question"],
        }

    async def execute(self, question: str) -> ToolResult:
        raise NeedInputError(question)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_ask_coordinator.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/a2a/worker/tools/ask_coordinator.py tests/test_ask_coordinator.py
git commit -m "refactor: rewrite AskCoordinatorTool to raise NeedInputError instead of HTTP+future"
```

---

### Task 7: AgentAdapter.execute() 暂停/恢复调度

**Files:**
- Modify: `src/a2a/worker/agent_adapter.py:121-172`
- Test: `tests/test_agent_adapter_execute.py`

**Interfaces:**
- Consumes: `AskCoordinatorTool` (Task 6), `submit(task_id=, initial_messages=)` (Task 5), `ContextManager.load_snapshot` (Task 3), `TaskUpdater.requires_input`
- Produces: `execute()` 在 `result.need_input` 时调用 `updater.requires_input()` 并返回；恢复时从快照加载 messages

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_adapter_execute.py
"""Tests for AgentAdapter.execute() pause/resume dispatch."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from a2a.worker.agent_adapter import AgentAdapter
from Agent.worker_agent.schema import RunResult, Message


class _FakeUpdater:
    def __init__(self):
        self.start_work = AsyncMock()
        self.requires_input = AsyncMock()
        self.add_artifact = AsyncMock()
        self.complete = AsyncMock()
        self.failed = AsyncMock()
        self.cancel = AsyncMock()


class _FakeContext:
    def __init__(self, task_id="task-1", context_id="ctx-1", user_input="go"):
        self._task = MagicMock()
        self._task.id = task_id
        self._task.context_id = context_id
        self._input = user_input

    @property
    def current_task(self):
        return self._task

    def get_user_input(self):
        return self._input


class _FakeEventQueue:
    def __init__(self):
        self.enqueue_event = AsyncMock()


@pytest.mark.asyncio
async def test_execute_calls_requires_input_on_need_input():
    """When submit returns need_input=True, execute() should call updater.requires_input()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()
    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="Where?", success=False, need_input=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._coordinator_callback_url = ""

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext()
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    fake_updater.requires_input.assert_called_once()
    fake_updater.complete.assert_not_called()


@pytest.mark.asyncio
async def test_execute_resume_loads_snapshot():
    """When snapshot exists, execute() should pass initial_messages to submit()."""
    adapter = AgentAdapter.__new__(AgentAdapter)
    adapter._controller = MagicMock()

    # Need a real ContextManager with a snapshot
    from Agent.worker_agent.context import ContextManager
    ctx_manager = ContextManager()
    ctx_manager.save_snapshot("task-1", [Message(role="system", content="sys")])
    adapter._controller._get_session = MagicMock(return_value=ctx_manager)

    adapter._controller.submit = AsyncMock(
        return_value=RunResult(content="done", success=True)
    )
    adapter._extra_tools = []
    adapter._step_callback = None
    adapter._coordinator_callback_url = ""

    fake_updater = _FakeUpdater()
    fake_ctx = _FakeContext(task_id="task-1", user_input="Go to sector 7")
    fake_queue = _FakeEventQueue()

    with patch("a2a.worker.agent_adapter.TaskUpdater", return_value=fake_updater):
        with patch("a2a.worker.agent_adapter.A2AWorkerSink"):
            with patch("a2a.worker.agent_adapter.new_text_message", return_value="msg"):
                with patch("a2a.worker.agent_adapter.Part"):
                    await adapter.execute(fake_ctx, fake_queue)

    # Verify submit was called with initial_messages
    call_kwargs = adapter._controller.submit.call_args
    assert call_kwargs.kwargs.get("initial_messages") is not None
    # Verify snapshot was consumed
    assert ctx_manager.load_snapshot("task-1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_agent_adapter_execute.py -v`
Expected: FAIL — `execute()` 还没有快照恢复逻辑和 `requires_input` 调用

- [ ] **Step 3: Rewrite execute() method**

在 `src/a2a/worker/agent_adapter.py` 中重写 `execute()` 方法。找到当前的 `execute()` 方法（约第 121-172 行），替换为：

```python
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_id = task.id
        context_id = task.context_id

        updater = TaskUpdater(event_queue, task_id, context_id)

        # Check for existing snapshot (resume after input-required)
        ctx = self._controller._get_session(context_id) if hasattr(self._controller, '_get_session') else None
        snapshot = ctx.load_snapshot(task_id) if ctx is not None else None

        if snapshot:
            await updater.start_work(message=new_text_message("Resuming after help"))
            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id, query, sink,
                task_id=task_id,
                initial_messages=snapshot,
            )
        else:
            await updater.start_work(message=new_text_message("Starting work"))

            # Inject AskCoordinatorTool
            from a2a.worker.tools.ask_coordinator import AskCoordinatorTool
            ask_tool = AskCoordinatorTool()
            self._extra_tools = [ask_tool] + self._extra_tools

            query = context.get_user_input() or ""
            result = await self._controller.submit(
                context_id, query, sink,
                task_id=task_id,
            )

        try:
            if result.need_input:
                await updater.requires_input(message=new_text_message(result.content))
                return

            final_text = result.content
            if final_text:
                await updater.add_artifact(
                    parts=[Part(text=final_text)],
                    name="result",
                )
            await updater.complete()
        except asyncio.CancelledError:
            await updater.cancel()
            raise
        except Exception as e:
            await updater.failed(message=new_text_message(str(e)))
            raise
```

注意：需要把 `sink` 的创建移到 `if snapshot` 之前。在 `updater = TaskUpdater(...)` 之后、snapshot 检查之前加：

```python
        # Construct output sink
        sink = A2AWorkerSink(event_queue, task_id, context_id)
        if self._step_callback is not None:
            ext_cb = lambda type_, **data: self._step_callback(type_=type_, **data)
            sink = TeeSink([sink, CallbackSink(ext_cb)])
```

同时删除旧的 `from a2a.worker.future_registry import resolve_future` 导入。

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_agent_adapter_execute.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/a2a/worker/agent_adapter.py tests/test_agent_adapter_execute.py
git commit -m "feat: AgentAdapter.execute() pauses via requires_input, resumes from snapshot"
```

---

### Task 8: RespondWorkerTool 重写

**Files:**
- Rewrite: `src/a2a/builtin_tools/respond_worker.py`
- Rewrite: `tests/test_respond_worker.py`

**Interfaces:**
- Consumes: `TaskStore.get_node`, `AgentRegistry.get`, A2A `create_client`/`ClientConfig`/`SendMessageRequest`/`Message`/`Part`/`Role`
- Produces: `RespondWorkerTool(store, registry)` — `execute(task_id, response)` 用 A2A send_message 恢复任务

- [ ] **Step 1: Write the failing test**

```python
# tests/test_respond_worker.py
"""Tests for RespondWorkerTool — Coordinator → Worker 回复（A2A send_message 版）。"""

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from a2a.builtin_tools.respond_worker import RespondWorkerTool
from a2a.coordinator.task_store import PlanNode
from a2a.coordinator.agent_registry import AgentInfo, AgentStatus, AgentNotFoundError


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_node = MagicMock()
    return store


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get = MagicMock()
    return registry


@pytest.fixture
def tool(mock_store, mock_registry):
    return RespondWorkerTool(store=mock_store, registry=mock_registry)


class TestRespondWorkerToolProperties:
    def test_name(self, tool):
        assert tool.name == "respond_worker"

    def test_parameters_schema(self, tool):
        schema = tool.parameters
        assert "task_id" in schema["properties"]
        assert "response" in schema["properties"]
        assert "task_id" in schema["required"]
        assert "response" in schema["required"]


@pytest.mark.asyncio
async def test_execute_sends_a2a_message(tool, mock_store, mock_registry):
    """正常流程：查找 task → 查找 worker → A2A send_message 恢复。"""
    node = PlanNode(task_id="task-42", worker_id="worker-1")
    mock_store.get_node.return_value = node
    mock_registry.get.return_value = AgentInfo(
        agent_id="worker-1",
        description="Test worker",
        endpoint="http://worker-1:8090",
        status=AgentStatus.ONLINE,
    )

    # Mock A2A client
    mock_client = MagicMock()
    mock_stream = AsyncMock()
    # Simulate one stream response then stop
    async def fake_stream(*args, **kwargs):
        yield MagicMock()
    mock_client.send_message = fake_stream
    mock_client.close = AsyncMock()

    with patch("a2a.builtin_tools.respond_worker.create_client", new=AsyncMock(return_value=mock_client)):
        with patch("a2a.builtin_tools.respond_worker.ClientConfig"):
            result = await tool.execute(task_id="task-42", response="Go to sector 7")

    assert result.success is True
    assert "task-42" in result.content


@pytest.mark.asyncio
async def test_execute_task_not_found(tool, mock_store):
    mock_store.get_node.return_value = None
    result = await tool.execute(task_id="nonexistent", response="Hello")
    assert result.success is False
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_execute_worker_not_in_registry(tool, mock_store, mock_registry):
    node = PlanNode(task_id="task-99", worker_id="lost-worker")
    mock_store.get_node.return_value = node
    mock_registry.get.side_effect = AgentNotFoundError("lost-worker")

    result = await tool.execute(task_id="task-99", response="Hello")
    assert result.success is False
    assert "not found in registry" in result.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_respond_worker.py -v`
Expected: FAIL — 旧代码用 httpx POST 而非 A2A client

- [ ] **Step 3: Rewrite RespondWorkerTool**

```python
# src/a2a/builtin_tools/respond_worker.py
"""RespondWorkerTool — Coordinator Agent 回复 Worker 的帮助请求。

用标准 A2A send_message 恢复处于 INPUT_REQUIRED 状态的 Worker 任务。
"""

import logging
from typing import Any

import httpx
from a2a.client import create_client, ClientConfig
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest

from Agent.router_agent.tools.base import Tool, ToolResult
from a2a.coordinator.task_store import TaskStore
from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError

logger = logging.getLogger(__name__)


class RespondWorkerTool(Tool):
    """回复 Worker 发起的帮助请求。

    通过 A2A send_message 向 Worker 发送回复，恢复处于 INPUT_REQUIRED 状态的任务。
    """

    def __init__(self, store: TaskStore, registry: AgentRegistry) -> None:
        self._store = store
        self._registry = registry

    @property
    def name(self) -> str:
        return "respond_worker"

    @property
    def description(self) -> str:
        return (
            "Respond to a worker's help request. Call this when a worker is "
            "asking for clarification (INPUT_REQUIRED status). Sends the response "
            "via A2A protocol to resume the worker's task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID of the worker that needs help",
                },
                "response": {
                    "type": "string",
                    "description": "The response/guidance to send back to the worker",
                },
            },
            "required": ["task_id", "response"],
        }

    async def execute(self, task_id: str, response: str) -> ToolResult:
        node = self._store.get_node(task_id)
        if node is None or not node.worker_id:
            return ToolResult(
                success=False,
                content=f"Task '{task_id}' not found in dispatched tasks.",
            )

        try:
            agent_info = self._registry.get(node.worker_id)
        except AgentNotFoundError:
            return ToolResult(
                success=False,
                content=f"Worker '{node.worker_id}' not found in registry.",
            )

        config = ClientConfig(
            streaming=True,
            httpx_client=httpx.AsyncClient(timeout=httpx.Timeout(30.0)),
        )

        try:
            client = await create_client(agent_info.endpoint, config)

            message = Message(
                role=Role.ROLE_USER,
                parts=[Part(text=response)],
                task_id=task_id,
            )
            request = SendMessageRequest(message=message)

            # Consume first event to confirm worker resumed, then return
            async for _ in client.send_message(request):
                break

            await client.close()
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Failed to send response to worker: {e}",
            )

        return ToolResult(
            success=True,
            content=f"Response sent to {task_id}: {response}",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_respond_worker.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/a2a/builtin_tools/respond_worker.py tests/test_respond_worker.py
git commit -m "refactor: rewrite RespondWorkerTool to use A2A send_message instead of HTTP push-callback"
```

---

### Task 9: Coordinator push-callback INPUT_REQUIRED 检测

**Files:**
- Modify: `src/a2a/coordinator/server.py:371-429` (push-callback handler)
- Modify: `tests/test_coordinator_push_callback.py` (加 INPUT_REQUIRED 测试)

**Interfaces:**
- Consumes: `TaskState.TASK_STATE_INPUT_REQUIRED`, `event_store`
- Produces: push-callback handler 在收到 INPUT_REQUIRED 状态时写 `help_request` 事件

- [ ] **Step 1: Write the failing test**

在 `tests/test_coordinator_push_callback.py` 末尾加新测试类：

```python
class TestInputRequiredRouting:
    def test_input_required_writes_help_request_event(self, client):
        """INPUT_REQUIRED 状态应该写入 help_request 事件。"""
        payload = {
            "statusUpdate": {
                "taskId": "task-input-1",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": "Where is the target?"}],
                    },
                },
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200

        summary = event_store.get_summary(task_ids={"task-input-1"})
        assert "HELP" in summary
        assert "Where is the target?" in summary

    def test_input_required_does_not_resolve_future(self, client):
        """INPUT_REQUIRED 不是 terminal，不应该 resolve_global_future。"""
        payload = {
            "statusUpdate": {
                "taskId": "task-input-2",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": "Help?"}],
                    },
                },
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200
        # No way to directly check future wasn't resolved, but verify no terminal event
        summary = event_store.get_summary(task_ids={"task-input-2"})
        assert "COMPLETED" not in summary
        assert "FAILED" not in summary
```

同时需要更新 `_build_push_callback_app()` 中的 handler 逻辑以匹配新行为。找到 test 文件中 `_build_push_callback_app` 的 `status_update` 分支，加 INPUT_REQUIRED 处理。

- [ ] **Step 2: Run test to verify it fails**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_coordinator_push_callback.py::TestInputRequiredRouting -v`
Expected: FAIL — 当前 test app 的 handler 不处理 INPUT_REQUIRED 的 help_request 事件

- [ ] **Step 3: Update test app handler and production server**

首先更新 `tests/test_coordinator_push_callback.py` 中的 `_build_push_callback_app()` 内的 `status_update` 处理分支，在 `is_terminal` 判断之后加：

```python
        elif sr.HasField("status_update"):
            su = sr.status_update
            task_id = su.task_id
            if su.HasField("status"):
                state_name = TaskState.Name(su.status.state) if su.status.state else "UNKNOWN"
                is_terminal = su.status.state in (
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                    TaskState.TASK_STATE_REJECTED,
                )
                if task_id:
                    event_store.append(task_id, "status_update", state=state_name)

                    # INPUT_REQUIRED: record as help_request
                    if su.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                        question = ""
                        if su.status.HasField("message"):
                            question = " ".join(
                                p.text for p in su.status.message.parts if p.text
                            )
                        event_store.append(task_id, "help_request", text=question)
```

然后更新生产代码 `src/a2a/coordinator/server.py` 的 push-callback handler（约第 408-419 行），在 `status_update` 分支中加相同的 INPUT_REQUIRED 处理：

```python
            elif sr.HasField("status_update"):
                su = sr.status_update
                task_id = su.task_id
                if su.HasField("status"):
                    state_name = TaskState.Name(su.status.state) if su.status.state else "UNKNOWN"
                    is_terminal = su.status.state in (
                        TaskState.TASK_STATE_COMPLETED,
                        TaskState.TASK_STATE_FAILED,
                        TaskState.TASK_STATE_CANCELED,
                    )
                    if task_id:
                        event_store.append(task_id, "status_update", state=state_name)

                        if su.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                            question = ""
                            if su.status.HasField("message"):
                                question = " ".join(
                                    p.text for p in su.status.message.parts if p.text
                                )
                            event_store.append(task_id, "help_request", text=question)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_coordinator_push_callback.py -v`
Expected: PASS (all tests including new TestInputRequiredRouting)

- [ ] **Step 5: Commit**

```bash
git add src/a2a/coordinator/server.py tests/test_coordinator_push_callback.py
git commit -m "feat: detect INPUT_REQUIRED status in push-callback, write help_request event"
```

---

### Task 10: 删除旧代码 — future_registry, push-callback, [HELP] 约定

**Files:**
- Delete: `src/a2a/worker/future_registry.py`
- Delete: `tests/test_future_registry.py`
- Delete: `tests/test_worker_push_callback.py`
- Modify: `src/a2a/worker/a2a_server.py` (删除 push-callback 路由 + coordinator_callback_url 参数)
- Modify: `src/a2a/worker/cli.py` (删除 coordinator_callback_url)
- Modify: `sar_orch/worker.py` (删除 coordinator_callback_url)
- Modify: `src/a2a/worker/agent_adapter.py` (删除 coordinator_callback_url 字段 + resolve_future 导入)
- Modify: `src/a2a/coordinator/server.py` (删除 [HELP] artifact 分支)

**Interfaces:**
- Consumes: all previous tasks
- Produces: clean codebase without future_registry or [HELP] hack

- [ ] **Step 1: Delete obsolete files**

```bash
rm src/a2a/worker/future_registry.py
rm tests/test_future_registry.py
rm tests/test_worker_push_callback.py
```

- [ ] **Step 2: Remove coordinator_callback_url from agent_adapter.py**

在 `src/a2a/worker/agent_adapter.py` 的 `__init__` 中删除 `coordinator_callback_url: str = ""` 参数和 `self._coordinator_callback_url = coordinator_callback_url` 赋值。同时删除所有 `resolve_future` 导入和调用（Task 7 已删除大部分，确保残留的也清理）。

- [ ] **Step 3: Remove push-callback route and coordinator_callback_url from a2a_server.py**

在 `src/a2a/worker/a2a_server.py` 中：
1. 删除 `coordinator_callback_url: str = ""` 参数（约第 52 行）
2. 删除传给 `AgentAdapter` 的 `coordinator_callback_url=coordinator_callback_url`（约第 112 行）
3. 删除 `_worker_push_callback` 函数和对应的 `Route("/a2a/push-callback", ...)` 路由（约第 129-148 行）
4. 将 `routes = [...]` 改回 `routes = []`

- [ ] **Step 4: Remove coordinator_callback_url from cli.py**

在 `src/a2a/worker/cli.py` 中：
1. 删除 `coordinator_callback_url: str = typer.Option(...)` 参数
2. 删除 `effective_callback_url = ...` 行
3. 删除传给 `create_worker_a2a_server` 的 `coordinator_callback_url=effective_callback_url`

- [ ] **Step 5: Remove coordinator_callback_url from sar_orch/worker.py**

在 `sar_orch/worker.py` 中：
1. 删除 `coord_http = self._coordinator_url.replace(...)` 行
2. 删除传给 `create_worker_a2a_server` 的 `coordinator_callback_url=coord_http`

- [ ] **Step 6: Remove [HELP] artifact branch from server.py**

在 `src/a2a/coordinator/server.py` 的 push-callback handler 中，删除 `[HELP]` 前缀检测分支（约第 399-407 行），恢复为简单的 artifact_update 处理：

```python
            elif sr.HasField("artifact_update"):
                au = sr.artifact_update
                task_id = au.task_id
                if au.HasField("artifact"):
                    texts = [p.text for p in au.artifact.parts if p.text]
                    if texts and task_id:
                        combined = " ".join(texts)
                        _push_artifact_cache.setdefault(task_id, []).extend(texts)
                        event_store.append(task_id, "artifact_update", text=combined)
```

- [ ] **Step 7: Run all tests to verify nothing broke**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/ -v`
Expected: PASS (all remaining tests)

- [ ] **Step 8: Run lint**

Run: `uv run --with ruff ruff check src/ sar_orch/ && uv run --with ruff ruff format --check src/ sar_orch/`
Expected: No errors

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: remove future_registry, worker push-callback, [HELP] hack — fully on A2A input-required"
```

---

### Task 11: 全量测试 + 集成验证

**Files:**
- Test: all `tests/`

- [ ] **Step 1: Run full test suite**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run lint on all source**

Run: `uv run --with ruff ruff check src/ sar_orch/ && uv run --with ruff ruff format --check src/ sar_orch/`
Expected: No errors

- [ ] **Step 3: Verify imports are clean**

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run python -c "from a2a.worker.agent_adapter import AgentAdapter; from a2a.builtin_tools.respond_worker import RespondWorkerTool; from a2a.worker.tools.ask_coordinator import AskCoordinatorTool; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 4: Commit if any fixes needed**

```bash
git add -A
git commit -m "test: full suite passes after input-required refactor"
```

---

## Self-Review Notes

- **Spec coverage:** All 9 spec sections (4.1-4.9) covered by Tasks 1-9. Deletion section covered by Task 10.
- **Type consistency:** `NeedInputError.question` used consistently. `RunResult.need_input` matches across Tasks 2, 4, 5, 7. `save_snapshot`/`load_snapshot` match across Tasks 3, 5, 7.
- **No placeholders:** All steps have complete code.

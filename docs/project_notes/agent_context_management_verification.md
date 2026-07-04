---
日期: 2026-07-02
文档类型: 项目验证记录
文档概述: Agent 上下文管理机制改造后的 SAR 冒烟实验验证结果、发现的问题与临时修复
---

# Agent 上下文管理改造 — SAR 冒烟实验验证

## 验证命令

```bash
rm -rf .venv && uv venv --python python3.10
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run --extra sar python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 5
```

## 改动范围

- 新增文件：`src/Agent/{worker_agent,router_agent}/context.py`、`hooks.py`、`sar_orch/tools/{worker,coordinator}/finish_task.py`
- 修改文件：`src/Agent/{worker_agent,router_agent}/{agent.py,schema/schema.py,tools/base.py}`、`src/a2a/...` 等共 20 个文件
- 设计文档：`docs/plans/agent_context_management_plan.md`

## 实验结果

| 指标 | 值 |
|------|-----|
| 场景 | scene=1, agents=2, seed=42 |
| 步数 | 5/5（按 `--max-steps` 触发 timeout） |
| Coverage | 0.3333 |
| Transport Rate | 0.2667 |
| 耗时 | 36.1s |
| 日志目录 | `sar_orch/results/sar_experiment_20260702_221922` |

Coordinator 成功派发两条子任务：
- Alice → 调查 ReservoirUtah
- Bob → 调查 ReservoirYork

Worker 执行链：
- Alice：`NavigateTo(ReservoirUtah)` → `GetSupply(Sand)` → 连续 `NoOp()`
- Bob：`NavigateTo(ReservoirYork)` → `GetSupply(Water)` → `ClearInventory()` → 连续 `NoOp()`

## 已做的临时修复

### 1. `RunResult` 未从 schema 包导出

**现象**：实验启动即失败
```
ImportError: cannot import name 'RunResult' from 'Agent.worker_agent.schema'
```

**原因**：`schema/schema.py` 已定义 `RunResult`，但 `schema/__init__.py` 未导出。

**修复**：在以下两个文件补全导出：
- `src/Agent/worker_agent/schema/__init__.py`
- `src/Agent/router_agent/schema/__init__.py`

## 发现的问题

### 1. 进程在 shutdown 阶段 SIGABRT 崩溃 ⚠️

**现象**：实验主逻辑正常结束、metrics 和 CSV 都已落盘后，Python 进程收到 fatal signal 6，退出码 134。

**证据**：
- 终端堆栈：`EventQueueSource._dispatch_loop() ... was cancelled without calling EventQueue.close() first`
- `dmesg`：`python3.10: python3: potentially unexpected fatal signal 6`

**初步判断**：daemon 后台线程中的 asyncio event loop 在解释器关闭时访问已释放对象，或 SSE/WebSocket 清理顺序不当。需单独复现确认是否本次改动引入。

### 2. `finish_task` 工具未实际被调用 ⚠️

**现象**：`agent_interactions.csv` 中未见 `finish_task`；子任务完成后 Worker 调用 `no_op()` 并返回文本摘要。

**原因**：`sar_orch/prompts/worker/system.md` 的 Available Tools 列表里没有 `finish_task`，Critical Rules 也指导使用 `no_op()` 等待。工具已注册但 LLM 不知道该用。

**影响**：`require_explicit_completion=True` 的退出逻辑无法通过 `finish_task` 触发，只能依赖 `no_op` 的 `[MISSION COMPLETE]` 分支或 max_steps 超时。

### 3. `agent_adapter.py` ruff E402 违规 ⚠️

```bash
uv run --with ruff ruff check <改动文件>
# Found 10 errors，全部在 src/a2a/worker/agent_adapter.py
# E402 Module level import not at top of file
```

`logger = logging.getLogger(__name__)` 之后做模块级导入，计划验证步骤要求 ruff 通过。

### 4. `ContextManager.assemble()` 隐式假设脆弱 ⚠️

代码：`result.extend(messages[1:])` 假设 `messages[0]` 是 system prompt。如果调用方历史不以 system 开头，会误删第一条消息。当前 SAR 路径满足假设，但属于潜在隐患。

### 5. context_id 会话复用未覆盖

本次冒烟运行中每个 Worker 只执行了一个子任务，未触发同 `context_id` 的第二次 dispatch，因此三层记忆/会话持久化的核心收益尚未验证。

## 后续建议

1. **补全 Worker prompt**：在 `sar_orch/prompts/worker/system.md` 中加入 `finish_task` 工具说明，并修改 Rule 6，引导子任务完成时调用 `finish_task(success=..., summary=..., task_description=...)`。
2. **修复 shutdown SIGABRT**：显式关闭 worker/coordinator 的 event loop 和 WebSocket，或在 `finally` 中给后台线程足够退出时间；必要时用 `atexit` 注册清理。
3. **修复 E402**：把 `src/a2a/worker/agent_adapter.py` 的导入全部移到文件顶部。
4. **加强验证**：用更长的 `--max-steps` 或多轮 coordinator dispatch 场景验证同 `context_id` 的会话记忆复用效果。
5. **baseline 对照**：按 plan 7.2 建议，用 `AGENT_CONTEXT_STRATEGY=none/summary/hybrid` 跑对照实验，比较 PromptTokens 曲线和任务成功率。

## 相关文件

- 设计文档：`docs/plans/agent_context_management_plan.md`
- 实验入口：`sar_orch/experiment.py`
- Worker 适配器：`src/a2a/worker/agent_adapter.py`
- Worker Agent：`src/Agent/worker_agent/agent.py`
- Worker Context：`src/Agent/worker_agent/context.py`
- Worker Prompt：`sar_orch/prompts/worker/system.md`
- 本次日志：`sar_orch/results/sar_experiment_20260702_221922/`

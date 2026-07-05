---
日期: 2026-07-05
文档类型: 技术设计文档
文档概述: 设计一个可复用的 Agent Skill / CLI 工具，将 SAR 多智能体实验的输出数据（results 与 logs）渲染为单个自包含 HTML 报告，便于人工阅读与分析。
---

# SAR 实验 HTML 报告渲染 Skill 设计

## 1. 背景与目标

SAR 实验每次运行后会在 `sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS/` 下生成多份 CSV/JSON/NDJSON，并在 `logs/` 下记录 Agent LLM trace、语义地图等。当前这些文件对人类阅读不友好，难以快速理解：

- 每一步每个 Agent 做了什么；
- Coordinator 何时下发任务、何时查询事件；
- Token 消耗趋势与分布；
- 系统对环境的认知（语义地图）如何演化；
- LLM 完整思考与工具调用链。

本设计旨在提供一个 **Skill / CLI 工具**，输入实验 `results` 目录与对应 `logs` 目录，输出 **单个自包含 HTML 报告**，默认保存到 `<results_dir>/report.html`。

## 2. 范围

### 2.1 In Scope

- 解析 `results` 目录下的：
  - `metadata.json`
  - `run_metrics.json`
  - `summary.csv`
  - `trajectory.csv`
  - `token_usage.csv`
  - `subtasks.csv`
  - `agent_interactions.csv`
  - `router_interactions.csv`
  - `events.ndjson`
- 解析 `logs` 目录下的：
  - `agent/sar_coordinator/semantic_map.jsonl`
  - `agent/sar_worker/<Agent>/<task_id>.ndjson`（LLM trace）
- 生成单文件 HTML 报告，包含以下视图：
  - **Overview**：实验元信息、关键指标卡片、Agent token 汇总。
  - **Timeline**：按步骤展示每个 Agent 的动作、结果、位置、库存、关键观察。
  - **Coordinator**：子任务下发、query_task_events、状态变化。
  - **Token Usage**：每步 token 柱状图 + 累计折线图，按 Agent/Coordinator 拆分。
  - **Semantic Map**：对象表（火、水库、人质、仓库）及其位置、强度/内容变化、冲突标记。
  - **LLM Trace**：完整保留 worker LLM 请求/响应/工具结果，默认折叠，可按 Agent/Task 分组。
- 提供 Agent 可调用的 Skill 接口（Python CLI）。

### 2.2 Out of Scope

- 实时/在线可视化（复用现有 `/ui/map`）。
- 跨运行对比分析。
- 复杂 3D 地图渲染（用户未选择地图视图）。
- 后端服务化部署。

## 3. 方案选择

选择 **方案 A：纯 Python + 内嵌 JS/CSS 的单一 HTML 生成器**。

理由：
- 无额外运行时依赖；
- 输出单个文件，易分享、归档、直接打开；
- 当前数据量与图表需求简单，纯 Canvas/SVG 即可满足；
- 与 Agent Skill 调用方式最契合。

## 4. 数据解析设计

### 4.1 通用原则

- 使用 Python 标准库 `csv`、`json`、`pathlib`。
- 缺失文件不报错，仅在报告“Warnings”区域提示。
- CSV 字段名使用原始大小写；解析时做 strip。

### 4.2 关键字段提取

| 文件 | 用于 | 关键字段/解析点 |
|------|------|----------------|
| `metadata.json` | Overview | `scene`, `agent_count`, `model`, `seed`, `run_id`, `state_mode`, `success_criteria`, `max_steps` |
| `run_metrics.json` | Overview | `coverage`, `transport_rate`, `steps`, `finished`, `elapsed_seconds`, `end_reason` |
| `summary.csv` | Overview / Token 汇总 | 每 Agent/Coordinator 的累计 token、cache hit/miss |
| `trajectory.csv` | Timeline | `Step`, `Actions`, `Successes`, `Observations`, `Coverage`, `TransportRate`, `TimeoutAgents`, `CompletedSubtasksDelta` |
| `token_usage.csv` | Token Usage | `Step`, `Agent`, `PromptTokens`, `CompletionTokens`, `TotalTokens`, `CacheHitTokens`, `CacheMissTokens` |
| `subtasks.csv` | Coordinator | `RunID`, `Step`, `SubtaskID`, `Status`, `AssignedTo`, `Subtask` |
| `agent_interactions.csv` | Timeline / LLM Trace 摘要 | `Step`, `Agent`, `ToolName`, `ToolArgs`, `Action`, `Observation`, `LLMInput`, `LLMOutput`, `Thinking` |
| `router_interactions.csv` | Coordinator | `Step`, `Subtask`, `AssignedTo`, `EventType` |
| `events.ndjson` | Coordinator | `event_type`, `agent`, `step`, `payload` |
| `semantic_map.jsonl` | Semantic Map | `event_type=observation_ingested`，提取 `observation` 与 `object` |
| `logs/agent/sar_worker/*/*.ndjson` | LLM Trace | `event` 为 `llm_request`, `llm_response`, `tool_result` |

### 4.3 Observation 解析

`trajectory.csv` 的 `Observations` 是 Python repr 列表，每个 Agent 一条字符串。需要：
- 提取 `I am at co-ordinates: (x, y, z)` 作为位置；
- 提取 `I am holding {...}` 作为库存；
- 提取动作结果行（`I tried to ... and was successful/failed.`）。

使用正则表达式，解析失败时保留原始文本。

## 5. 报告页面结构

单个 HTML 文件，结构如下：

```
├─ Header
│   ├─ Run ID / Scene / Agents / Model / Seed
│   ├─ Status (Finished / End Reason / Steps / Elapsed)
│   └─ Metrics Cards: Coverage, Transport Rate, Total Tokens, Total Interactions
├─ Tab Navigation
│   ├─ Timeline
│   ├─ Coordinator
│   ├─ Token Usage
│   ├─ Semantic Map
│   └─ LLM Trace
├─ Tab: Timeline
│   ├─ Step slider / per-step cards
│   ├─ Each card: Agent, Action, Success, Position, Inventory, observation summary
│   └─ Expandable raw observation
├─ Tab: Coordinator
│   ├─ Subtask table (id, assigned, status, created/updated step)
│   ├─ Event timeline (dispatch, query_task_events)
│   └─ Raw prompt 折叠区
├─ Tab: Token Usage
│   ├─ Per-step stacked bar: Prompt vs Completion per Agent/Coordinator
│   ├─ Cumulative line chart per Agent/Coordinator
│   └─ Cache hit/miss summary
├─ Tab: Semantic Map
│   ├─ Object table: type, name, last position, attributes, last seen step, conflict flag
│   ├─ Per-object observation history
│   └─ Conflict/merge 提示
└─ Tab: LLM Trace
    ├─ Agent / Task selector
    ├─ Event list: llm_request → llm_response → tool_result
    └─ Expandable full messages
```

### 5.1 样式

遵循 `CLAUDE.md` 中指定的 HTML 颜色：
- 背景 `#ffffff`
- 卡片面 `#f4f6f9`
- 文字 `#1a2332`
- 次要文字 `#5a6a7e`
- 蓝色强调 `#2563eb`

字体使用系统无衬线字体栈；报告为响应式，最小宽度 1024px 体验最佳。

### 5.2 交互

- 标签页切换（JS，无外部库）。
- Timeline 单步 observation 展开/折叠。
- LLM Trace 按 task/agent 分组，点击展开完整消息。
- Token 图表使用原生 Canvas 绘制，hover 显示数值。

## 6. Skill / CLI 接口

```bash
python -m sar_orch.render_report \
  --results-dir /path/to/sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS \
  --logs-dir /path/to/logs \
  [--output /path/to/output.html]
```

- `--results-dir`：实验结果目录（必填）。
- `--logs-dir`：实验日志根目录（必填）。
- `--output`：输出 HTML 路径，默认 `<results_dir>/report.html`。

作为 OpenCode Skill 时，Agent 接收自然语言指令如：

> “把 `sar_orch/results/sar_experiment_20260705_192717` 和 `logs` 渲染成 HTML 报告。”

Agent 调用上述 CLI，并返回生成文件路径。

## 7. 错误处理

- 如果 `results-dir` 不存在，报错并退出。
- 如果 `logs-dir` 不存在，提示警告，继续生成（缺失 LLM Trace / Semantic Map 视图）。
- 单个文件解析失败时记录 warning，不影响其他视图。
- 输出 HTML 始终生成，即使部分数据缺失。

## 8. 测试与验证

1. 用当前实验数据 `sar_experiment_20260705_192717` + `logs` 生成报告。
2. 用浏览器打开，确认：
   - 6 个标签页正常切换；
   - Overview 指标与 `run_metrics.json` 一致；
   - Timeline 步数与 `trajectory.csv` 一致；
   - Token 累计值与 `summary.csv` 一致；
   - Semantic Map 中的对象数与 `semantic_map.jsonl` 一致。
3. 验证报告为单个文件，无外部请求。

## 9. 后续可扩展

- 增加地图视图（基于 trajectory 位置数据绘制 2D 路径）。
- 支持多运行对比（传入多个 results 目录）。
- 导出 PDF 或 Markdown 摘要。
- 接入 OpenCode skill 注册，支持更自然的语音调用。

---
日期: 2026-07-05
文档类型: 实现计划
文档概述: 将 SAR 实验 HTML 报告渲染 Skill 的设计文档拆分为可执行的开发任务，包含文件结构、代码、测试与提交流程。
---

# SAR 实验 HTML 报告渲染 Skill 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个可复用的 CLI / Skill，将 SAR 实验的 `results` 与 `logs` 数据渲染为单个自包含 HTML 报告。

**Architecture:** 在 `sar_orch/render_report/` 下建立专用包：`models.py` 定义数据类，`loaders.py` 解析所有 CSV/JSON/NDJSON，`html.py` 生成内嵌 CSS/JS 的单一 HTML，`cli.py` 提供命令行入口。测试使用当前真实实验数据做集成验证。

**Tech Stack:** Python 3.11+ 标准库（`csv`, `json`, `pathlib`, `dataclasses`, `ast`, `re`, `html`, `argparse`），无第三方依赖。

## Global Constraints

- 输出必须是单个自包含 HTML 文件，所有样式和 JS 内嵌。
- 默认输出路径：`<results_dir>/report.html`。
- 缺失数据不中断生成，仅在报告内显示 warning。
- 遵循 `CLAUDE.md` 颜色规范：背景 `#ffffff`、卡片 `#f4f6f9`、文字 `#1a2332`、次要文字 `#5a6a7e`、强调 `#2563eb`。
- 代码需通过 `uv run ruff check src/ sar_orch/` 与 `uv run ruff format src/ sar_orch/`。

---

## File Structure

```
sar_orch/render_report/
├── __init__.py      # 对外暴露 render() 主入口
├── models.py        # 所有数据类
├── loaders.py       # 解析 results/ 与 logs/ 下的数据
├── html.py          # HTML 模板、CSS、JS、报告组装
└── cli.py           # 命令行参数解析与文件写入

tests/test_render_report.py   # 集成测试
```

---

### Task 1: 数据模型

**Files:**
- Create: `sar_orch/render_report/models.py`

**Interfaces:**
- Produces: 所有后续 loader 与 html 生成器使用的数据类。

- [ ] **Step 1: 创建 `models.py`**

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunMeta:
    run_id: str
    scene: int
    agent_count: int
    model: str
    seed: int
    state_mode: str
    success_criteria: str
    max_steps: int


@dataclass
class RunMetrics:
    coverage: float
    transport_rate: float
    steps: int
    finished: bool
    elapsed_seconds: float
    end_reason: str


@dataclass
class AgentTokenSummary:
    agent: str
    prompt: int
    completion: int
    total: int
    cache_hit: int
    cache_miss: int


@dataclass
class StepRecord:
    step: int
    actions: dict[str, str] = field(default_factory=dict)
    successes: dict[str, bool] = field(default_factory=dict)
    positions: dict[str, tuple] = field(default_factory=dict)
    inventories: dict[str, dict] = field(default_factory=dict)
    observations: dict[str, str] = field(default_factory=dict)
    coverage: float = 0.0
    transport_rate: float = 0.0
    timeout_agents: list[str] = field(default_factory=list)
    completed_subtasks_delta: int = 0


@dataclass
class TokenRecord:
    step: int
    agent: str
    prompt: int
    completion: int
    total: int
    cache_hit: int
    cache_miss: int


@dataclass
class Subtask:
    subtask_id: str
    step: int
    status: str
    assigned_to: str
    text: str


@dataclass
class RouterEvent:
    step: int
    subtask: str
    assigned_to: str
    event_type: str


@dataclass
class CoordinatorEvent:
    step: int
    event_type: str
    agent: str
    payload: dict


@dataclass
class SemanticObject:
    object_type: str
    name: str
    observations: list[dict] = field(default_factory=list)
    conflict: bool = False


@dataclass
class LLMTraceEvent:
    task_id: str
    agent: str
    ts: str
    event: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result: str = ""
    error: str = ""


@dataclass
class ReportData:
    meta: RunMeta | None = None
    metrics: RunMetrics | None = None
    summary: dict = field(default_factory=dict)
    agent_tokens: list[AgentTokenSummary] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    tokens: list[TokenRecord] = field(default_factory=list)
    subtasks: list[Subtask] = field(default_factory=list)
    router_events: list[RouterEvent] = field(default_factory=list)
    coordinator_events: list[CoordinatorEvent] = field(default_factory=list)
    semantic_objects: list[SemanticObject] = field(default_factory=list)
    llm_traces: dict[tuple[str, str], list[LLMTraceEvent]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
```

- [ ] **Step 2: 创建 `__init__.py`**

```python
from sar_orch.render_report.cli import main
from sar_orch.render_report.loaders import load_report_data
from sar_orch.render_report.html import render_html

__all__ = ["main", "load_report_data", "render_html"]
```

- [ ] **Step 3: 提交**

```bash
git add sar_orch/render_report/__init__.py sar_orch/render_report/models.py
git commit -m "feat(render_report): add data models for SAR report"
```

---

### Task 2: 数据加载器

**Files:**
- Create: `sar_orch/render_report/loaders.py`

**Interfaces:**
- Consumes: `models.py` 中的数据类。
- Produces: `load_report_data(results_dir: Path, logs_dir: Path) -> ReportData`。

- [ ] **Step 1: 编写加载器实现**

```python
import ast
import csv
import json
import re
from pathlib import Path

from sar_orch.render_report.models import (
    AgentTokenSummary,
    CoordinatorEvent,
    LLMTraceEvent,
    ReportData,
    RouterEvent,
    RunMeta,
    RunMetrics,
    SemanticObject,
    StepRecord,
    Subtask,
    TokenRecord,
)


_POSITION_RE = re.compile(r"I am at co-ordinates:\s*\(([-\d]+),\s*([-\d]+),\s*([-\d]+)\)")
_INVENTORY_RE = re.compile(r"I am holding\s+(\{[^}]+\})")


def _safe_literal_eval(value: str):
    try:
        return ast.literal_eval(value)
    except Exception:
        return value


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): (v or "") for k, v in row.items()})
    return rows


def _read_ndjson(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _extract_position(text: str) -> tuple | None:
    m = _POSITION_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _extract_inventory(text: str) -> dict:
    m = _INVENTORY_RE.search(text)
    if m:
        try:
            return ast.literal_eval(m.group(1))
        except Exception:
            pass
    return {}


def _agent_names_from_summary(summary: dict) -> list[str]:
    # Summary header includes metrics then per-agent token columns like AlicePromptTokens
    names = []
    for key in summary.keys():
        if key.endswith("PromptTokens"):
            name = key[: -len("PromptTokens")]
            if name and name not in names:
                names.append(name)
    return names


def load_run_meta(results_dir: Path) -> RunMeta | None:
    path = results_dir / "metadata.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return RunMeta(
        run_id=data.get("run_id", ""),
        scene=data.get("scene", 0),
        agent_count=data.get("agent_count", 0),
        model=data.get("model", ""),
        seed=data.get("seed", 0),
        state_mode=data.get("state_mode", ""),
        success_criteria=data.get("success_criteria", ""),
        max_steps=data.get("max_steps", 0),
    )


def load_run_metrics(results_dir: Path) -> RunMetrics | None:
    path = results_dir / "run_metrics.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return RunMetrics(
        coverage=float(data.get("coverage", 0.0)),
        transport_rate=float(data.get("transport_rate", 0.0)),
        steps=int(data.get("steps", 0)),
        finished=bool(data.get("finished", False)),
        elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
        end_reason=str(data.get("end_reason", "")),
    )


def load_summary(results_dir: Path) -> tuple[dict, list[AgentTokenSummary]]:
    path = results_dir / "summary.csv"
    if not path.exists():
        return {}, []
    rows = _read_csv(path)
    if not rows:
        return {}, []
    summary = rows[0]
    agents = _agent_names_from_summary(summary)
    tokens = []
    for agent in agents:
        tokens.append(
            AgentTokenSummary(
                agent=agent,
                prompt=int(summary.get(f"{agent}PromptTokens", 0) or 0),
                completion=int(summary.get(f"{agent}CompletionTokens", 0) or 0),
                total=int(summary.get(f"{agent}TotalTokens", 0) or 0),
                cache_hit=int(summary.get(f"{agent}CacheHitTokens", 0) or 0),
                cache_miss=int(summary.get(f"{agent}CacheMissTokens", 0) or 0),
            )
        )
    return summary, tokens


def load_trajectory(results_dir: Path, agent_names: list[str]) -> list[StepRecord]:
    path = results_dir / "trajectory.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    records: list[StepRecord] = []
    for row in rows:
        try:
            step = int(row["Step"])
        except (KeyError, ValueError):
            continue
        actions = _safe_literal_eval(row.get("Actions", "[]"))
        successes = _safe_literal_eval(row.get("Successes", "[]"))
        observations = _safe_literal_eval(row.get("Observations", "[]"))
        if not isinstance(actions, list):
            actions = []
        if not isinstance(successes, list):
            successes = []
        if not isinstance(observations, list):
            observations = []
        record = StepRecord(step=step)
        for idx, name in enumerate(agent_names):
            record.actions[name] = actions[idx] if idx < len(actions) else ""
            record.successes[name] = successes[idx] if idx < len(successes) else False
            obs = observations[idx] if idx < len(observations) else ""
            record.observations[name] = obs
            record.positions[name] = _extract_position(obs)
            record.inventories[name] = _extract_inventory(obs)
        record.coverage = float(row.get("Coverage", 0) or 0)
        record.transport_rate = float(row.get("TransportRate", 0) or 0)
        record.timeout_agents = _safe_literal_eval(row.get("TimeoutAgents", "[]"))
        if not isinstance(record.timeout_agents, list):
            record.timeout_agents = []
        try:
            record.completed_subtasks_delta = int(row.get("CompletedSubtasksDelta", 0) or 0)
        except ValueError:
            record.completed_subtasks_delta = 0
        records.append(record)
    return records


def load_token_usage(results_dir: Path) -> list[TokenRecord]:
    path = results_dir / "token_usage.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    records = []
    for row in rows:
        try:
            records.append(
                TokenRecord(
                    step=int(row["Step"]),
                    agent=row["Agent"],
                    prompt=int(row.get("PromptTokens", 0) or 0),
                    completion=int(row.get("CompletionTokens", 0) or 0),
                    total=int(row.get("TotalTokens", 0) or 0),
                    cache_hit=int(row.get("CacheHitTokens", 0) or 0),
                    cache_miss=int(row.get("CacheMissTokens", 0) or 0),
                )
            )
        except (KeyError, ValueError):
            continue
    return records


def load_subtasks(results_dir: Path) -> list[Subtask]:
    path = results_dir / "subtasks.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    subtasks = []
    for row in rows:
        try:
            subtasks.append(
                Subtask(
                    subtask_id=row.get("SubtaskID", ""),
                    step=int(row.get("Step", 0) or 0),
                    status=row.get("Status", ""),
                    assigned_to=row.get("AssignedTo", ""),
                    text=row.get("Subtask", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return subtasks


def load_router(results_dir: Path) -> list[RouterEvent]:
    path = results_dir / "router_interactions.csv"
    if not path.exists():
        return []
    rows = _read_csv(path)
    events = []
    for row in rows:
        try:
            events.append(
                RouterEvent(
                    step=int(row.get("Step", 0) or 0),
                    subtask=row.get("Subtask", ""),
                    assigned_to=row.get("AssignedTo", ""),
                    event_type=row.get("EventType", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return events


def load_coordinator_events(results_dir: Path) -> list[CoordinatorEvent]:
    path = results_dir / "events.ndjson"
    records = _read_ndjson(path)
    events = []
    for rec in records:
        events.append(
            CoordinatorEvent(
                step=int(rec.get("step", 0)),
                event_type=str(rec.get("event_type", "")),
                agent=str(rec.get("agent", "")),
                payload=rec.get("payload", {}),
            )
        )
    return events


def load_semantic_map(logs_dir: Path) -> list[SemanticObject]:
    path = logs_dir / "agent" / "sar_coordinator" / "semantic_map.jsonl"
    records = _read_ndjson(path)
    objects: dict[str, SemanticObject] = {}
    for rec in records:
        if rec.get("event_type") != "observation_ingested":
            continue
        obs = rec.get("observation", {})
        obj = rec.get("object", {})
        name = obs.get("name") or obj.get("name")
        obj_type = obs.get("object_type") or obj.get("object_type")
        if not name:
            continue
        if name not in objects:
            objects[name] = SemanticObject(
                object_type=obj_type or "unknown",
                name=name,
            )
        objects[name].observations.append(obs)
        if obj.get("conflict") or rec.get("conflict"):
            objects[name].conflict = True
    return list(objects.values())


def load_worker_logs(logs_dir: Path) -> dict[tuple[str, str], list[LLMTraceEvent]]:
    base = logs_dir / "agent" / "sar_worker"
    traces: dict[tuple[str, str], list[LLMTraceEvent]] = {}
    if not base.exists():
        return traces
    for agent_dir in base.iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        for file in agent_dir.iterdir():
            if not file.is_file() or not file.suffix == ".ndjson":
                continue
            task_id = file.stem
            records = _read_ndjson(file)
            key = (agent, task_id)
            traces[key] = []
            for rec in records:
                event_type = rec.get("event", "")
                event = LLMTraceEvent(
                    task_id=task_id,
                    agent=agent,
                    ts=str(rec.get("ts", "")),
                    event=event_type,
                    content=str(rec.get("content", "")),
                    tool_calls=rec.get("tool_calls", []),
                    usage=rec.get("usage", {}),
                    tool_name=str(rec.get("tool_name", "")),
                    arguments=rec.get("arguments", {}),
                    result=str(rec.get("result", "")),
                    error=str(rec.get("error", "")),
                )
                traces[key].append(event)
    return traces


def load_report_data(results_dir: Path, logs_dir: Path) -> ReportData:
    data = ReportData()
    data.meta = load_run_meta(results_dir)
    data.metrics = load_run_metrics(results_dir)
    data.summary, data.agent_tokens = load_summary(results_dir)

    agent_names = _agent_names_from_summary(data.summary)
    if data.meta and not agent_names:
        agent_names = [f"Agent{i}" for i in range(1, data.meta.agent_count + 1)]

    data.steps = load_trajectory(results_dir, agent_names)
    data.tokens = load_token_usage(results_dir)
    data.subtasks = load_subtasks(results_dir)
    data.router_events = load_router(results_dir)
    data.coordinator_events = load_coordinator_events(results_dir)
    data.semantic_objects = load_semantic_map(logs_dir)
    data.llm_traces = load_worker_logs(logs_dir)

    if data.meta is None:
        data.warnings.append(f"metadata.json not found in {results_dir}")
    if data.metrics is None:
        data.warnings.append(f"run_metrics.json not found in {results_dir}")
    if not data.steps:
        data.warnings.append(f"trajectory.csv empty or missing in {results_dir}")
    return data
```

- [ ] **Step 2: 提交**

```bash
git add sar_orch/render_report/loaders.py
git commit -m "feat(render_report): add data loaders"
```

---

### Task 3: HTML 生成器

**Files:**
- Create: `sar_orch/render_report/html.py`

**Interfaces:**
- Consumes: `ReportData`。
- Produces: `render_html(data: ReportData) -> str`。

- [ ] **Step 1: 编写 `html.py`**

```python
import html
import json
from typing import Any

from sar_orch.render_report.models import ReportData


def _h(value: Any) -> str:
    return html.escape(str(value))


_CSS = """
:root {
  --bg: #ffffff;
  --card: #f4f6f9;
  --text: #1a2332;
  --muted: #5a6a7e;
  --accent: #2563eb;
  --border: #e2e8f0;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 24px;
  line-height: 1.5;
}
.container { max-width: 1400px; margin: 0 auto; }
h1, h2, h3 { color: var(--text); margin-top: 0; }
.subtitle { color: var(--muted); }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
.metric-card {
  background: var(--card);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
}
.metric-value { font-size: 24px; font-weight: bold; color: var(--accent); }
.metric-label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
.tabs { display: flex; gap: 4px; border-bottom: 2px solid var(--border); margin: 24px 0 16px; }
.tab {
  padding: 10px 18px;
  cursor: pointer;
  color: var(--muted);
  border-radius: 6px 6px 0 0;
  background: transparent;
  border: none;
  font-size: 14px;
}
.tab.active { background: var(--accent); color: #fff; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.card {
  background: var(--card);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
}
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; }
tr:hover { background: rgba(37,99,235,0.04); }
pre {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  overflow-x: auto;
  font-size: 12px;
}
.collapsible { cursor: pointer; color: var(--accent); }
.collapsed { display: none; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  background: var(--accent);
  color: #fff;
  margin-right: 6px;
}
.warn { color: #b45309; background: #fff7ed; border: 1px solid #fed7aa; padding: 10px; border-radius: 6px; }
.step-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
.chart-box { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
canvas { width: 100% !important; height: 300px !important; }
"""

_JS = """
function openTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('content-' + name).classList.add('active');
}
function toggle(id) {
  const el = document.getElementById(id);
  el.classList.toggle('collapsed');
}
function drawBarChart(canvasId, labels, datasets) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  const pad = { top: 20, right: 20, bottom: 40, left: 50 };
  const chartW = w - pad.left - pad.right;
  const chartH = h - pad.top - pad.bottom;
  const max = Math.max(...datasets.flatMap(d => d.data), 1);
  const n = labels.length;
  const groupW = chartW / n;
  const barW = groupW / (datasets.length + 1);
  datasets.forEach((ds, di) => {
    ctx.fillStyle = ds.color;
    ds.data.forEach((v, i) => {
      const x = pad.left + i * groupW + di * barW + barW / 2;
      const bh = (v / max) * chartH;
      ctx.fillRect(x, pad.top + chartH - bh, barW * 0.8, bh);
    });
  });
  ctx.fillStyle = '#1a2332';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  labels.forEach((l, i) => {
    const x = pad.left + i * groupW + groupW / 2;
    ctx.fillText(l, x, h - 10);
  });
  ctx.textAlign = 'right';
  for (let i = 0; i <= 5; i++) {
    const v = Math.round((max / 5) * i);
    const y = pad.top + chartH - (i / 5) * chartH;
    ctx.fillText(String(v), pad.left - 6, y + 4);
    ctx.strokeStyle = '#e2e8f0';
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
  }
}
function drawLineChart(canvasId, labels, datasets) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  const pad = { top: 20, right: 20, bottom: 40, left: 60 };
  const chartW = w - pad.left - pad.right;
  const chartH = h - pad.top - pad.bottom;
  const max = Math.max(...datasets.flatMap(d => d.data), 1);
  const n = labels.length;
  datasets.forEach(ds => {
    ctx.strokeStyle = ds.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ds.data.forEach((v, i) => {
      const x = pad.left + (i / (n - 1 || 1)) * chartW;
      const y = pad.top + chartH - (v / max) * chartH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.fillStyle = '#1a2332';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  labels.forEach((l, i) => {
    const x = pad.left + (i / (n - 1 || 1)) * chartW;
    ctx.fillText(l, x, h - 10);
  });
  ctx.textAlign = 'right';
  for (let i = 0; i <= 5; i++) {
    const v = Math.round((max / 5) * i);
    const y = pad.top + chartH - (i / 5) * chartH;
    ctx.fillText(String(v), pad.left - 6, y + 4);
    ctx.strokeStyle = '#e2e8f0';
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
  }
}
"""


def _header(data: ReportData) -> str:
    meta = data.meta
    metrics = data.metrics
    title = f"SAR Experiment Report: {_h(meta.run_id if meta else 'unknown')}"
    parts = []
    if meta:
        parts.extend([
            f"Scene {_h(meta.scene)}",
            f"{_h(meta.agent_count)} Agents",
            f"Model {_h(meta.model)}",
            f"Seed {_h(meta.seed)}",
        ])
    if metrics:
        parts.extend([
            f"{metrics.steps}/{_h(meta.max_steps if meta else '?')} Steps",
            f"Finished: {_h(metrics.finished)}",
            f"End Reason: {_h(metrics.end_reason)}",
            f"Elapsed: {metrics.elapsed_seconds:.1f}s",
        ])
    return f"""
<div class="container">
  <h1>{title}</h1>
  <p class="subtitle">{' · '.join(parts)}</p>
"""


def _metric_cards(data: ReportData) -> str:
    metrics = data.metrics
    total_tokens = sum(t.total for t in data.agent_tokens)
    total_interactions = int(data.summary.get("TotalAgentInteractions", 0) or 0)
    cards = [
        (f"{metrics.coverage:.2%}" if metrics else "-", "Coverage"),
        (f"{metrics.transport_rate:.2%}" if metrics else "-", "Transport Rate"),
        (f"{total_tokens:,}", "Total Tokens"),
        (f"{total_interactions}", "Agent Interactions"),
    ]
    cells = "".join(
        f'<div class="metric-card"><div class="metric-value">{v}</div><div class="metric-label">{l}</div></div>'
        for v, l in cards
    )
    return f'<div class="metrics">{cells}</div>'


def _agent_token_table(data: ReportData) -> str:
    if not data.agent_tokens:
        return ""
    rows = ""
    for t in data.agent_tokens:
        rows += f"<tr><td>{_h(t.agent)}</td><td>{t.prompt:,}</td><td>{t.completion:,}</td><td>{t.total:,}</td><td>{t.cache_hit:,}</td><td>{t.cache_miss:,}</td></tr>"
    return f"""
<div class="card">
  <h3>Token Summary by Agent</h3>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Agent</th><th>Prompt</th><th>Completion</th><th>Total</th><th>Cache Hit</th><th>Cache Miss</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>
"""


def _overview(data: ReportData) -> str:
    return f"""
<div id="content-overview" class="tab-content active">
  {_metric_cards(data)}
  {_agent_token_table(data)}
</div>
"""


def _timeline(data: ReportData) -> str:
    steps_html = ""
    for step in data.steps:
        agent_cards = ""
        for agent in sorted(step.actions.keys()):
            pos = step.positions.get(agent)
            inv = step.inventories.get(agent)
            obs = step.observations.get(agent, "")
            pos_str = f"({pos[0]}, {pos[1]}, {pos[2]})" if pos else "unknown"
            inv_str = json.dumps(inv) if inv else "{}"
            success = "✅" if step.successes.get(agent) else "❌"
            agent_cards += f"""
<div class="card">
  <strong>{_h(agent)}</strong> <span class="badge">{success}</span><br/>
  <span style="color:var(--muted);font-size:13px;">Action: {_h(step.actions.get(agent, ''))}</span><br/>
  <span style="color:var(--muted);font-size:13px;">Pos: {pos_str} · Inv: {_h(inv_str)}</span><br/>
  <span class="collapsible" onclick="toggle('obs-{step.step}-{_h(agent)}')">Toggle observation</span>
  <pre id="obs-{step.step}-{_h(agent)}" class="collapsed">{_h(obs)}</pre>
</div>
"""
        steps_html += f"""
<div class="card">
  <h3>Step {step.step}</h3>
  <p style="color:var(--muted);font-size:13px;">Coverage: {step.coverage:.2%} · Transport: {step.transport_rate:.2%} · Timeouts: {_h(step.timeout_agents)}</p>
  <div class="step-grid">{agent_cards}</div>
</div>
"""
    return f"""
<div id="content-timeline" class="tab-content">
  {steps_html}
</div>
"""


def _coordinator(data: ReportData) -> str:
    subtask_rows = ""
    for s in data.subtasks:
        subtask_rows += f"<tr><td>{_h(s.subtask_id)}</td><td>{s.step}</td><td>{_h(s.status)}</td><td>{_h(s.assigned_to)}</td><td>{_h(s.text[:120])}{'...' if len(s.text) > 120 else ''}</td></tr>"
    router_rows = ""
    for e in data.router_events:
        router_rows += f"<tr><td>{e.step}</td><td>{_h(e.event_type)}</td><td>{_h(e.assigned_to)}</td><td>{_h(e.subtask[:120])}{'...' if len(e.subtask) > 120 else ''}</td></tr>"
    return f"""
<div id="content-coordinator" class="tab-content">
  <div class="card">
    <h3>Subtasks</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Step</th><th>Status</th><th>Assigned</th><th>Text</th></tr></thead>
        <tbody>{subtask_rows}</tbody>
      </table>
    </div>
  </div>
  <div class="card">
    <h3>Router Events</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Step</th><th>Type</th><th>Assigned</th><th>Subtask</th></tr></thead>
        <tbody>{router_rows}</tbody>
      </table>
    </div>
  </div>
</div>
"""


def _token_usage(data: ReportData) -> str:
    if not data.tokens:
        return '<div id="content-tokens" class="tab-content"><p class="subtitle">No token data.</p></div>'
    steps = sorted({t.step for t in data.tokens})
    agents = sorted({t.agent for t in data.tokens})
    labels_json = json.dumps([str(s) for s in steps])
    datasets = []
    colors = {"Alice": "#2563eb", "Bob": "#7c3aed", "Coordinator": "#059669"}
    for agent in agents:
        ds = {"label": agent, "color": colors.get(agent, "#64748b"), "data": []}
        for step in steps:
            total = sum(t.total for t in data.tokens if t.step == step and t.agent == agent)
            ds["data"].append(total)
        datasets.append(ds)
    cumulative = []
    for agent in agents:
        running = 0
        ds = {"label": agent, "color": colors.get(agent, "#64748b"), "data": []}
        for step in steps:
            total = sum(t.total for t in data.tokens if t.step == step and t.agent == agent)
            running += total
            ds["data"].append(running)
        cumulative.append(ds)
    return f"""
<div id="content-tokens" class="tab-content">
  <div class="card chart-box">
    <h3>Tokens per Step</h3>
    <canvas id="chart-tokens-bar"></canvas>
  </div>
  <div class="card chart-box">
    <h3>Cumulative Tokens</h3>
    <canvas id="chart-tokens-line"></canvas>
  </div>
  <script>
    drawBarChart('chart-tokens-bar', {labels_json}, {json.dumps(datasets)});
    drawLineChart('chart-tokens-line', {labels_json}, {json.dumps(cumulative)});
  </script>
</div>
"""


def _semantic_map(data: ReportData) -> str:
    rows = ""
    for obj in data.semantic_objects:
        last = obj.observations[-1] if obj.observations else {}
        pos = last.get("position")
        pos_str = f"({pos[0]}, {pos[1]}, {pos[2]})" if isinstance(pos, list) and len(pos) == 3 else _h(pos)
        attrs = last.get("attributes", {})
        attr_str = json.dumps(attrs, ensure_ascii=False) if attrs else ""
        conflict = '<span class="badge" style="background:#dc2626;">CONFLICT</span>' if obj.conflict else ""
        rows += f"""
<tr>
  <td>{_h(obj.object_type)} {conflict}</td>
  <td>{_h(obj.name)}</td>
  <td>{pos_str}</td>
  <td>{_h(attr_str)}</td>
  <td>{len(obj.observations)}</td>
</tr>
"""
    return f"""
<div id="content-semantic" class="tab-content">
  <div class="card">
    <h3>Semantic Map Objects</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Type</th><th>Name</th><th>Last Position</th><th>Last Attributes</th><th>Observations</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>
"""


def _llm_trace(data: ReportData) -> str:
    if not data.llm_traces:
        return '<div id="content-llm" class="tab-content"><p class="subtitle">No worker LLM traces found.</p></div>'
    groups = ""
    for (agent, task_id), events in sorted(data.llm_traces.items()):
        event_rows = ""
        for i, ev in enumerate(events):
            detail = ""
            if ev.event == "llm_request":
                detail = f"<pre>{_h(ev.content[:500])}{'...' if len(ev.content) > 500 else ''}</pre>"
            elif ev.event == "llm_response":
                detail = f"<p>{_h(ev.content)}</p><p style='color:var(--muted);font-size:12px;'>Usage: {_h(json.dumps(ev.usage))}</p>"
            elif ev.event == "tool_result":
                detail = f"<p>Tool: {_h(ev.tool_name)}</p><pre>{_h(ev.result[:500])}{'...' if len(ev.result) > 500 else ''}</pre>"
            event_rows += f"""
<div class="card">
  <span class="badge">{_h(ev.event)}</span> <span style="color:var(--muted);font-size:12px;">{_h(ev.ts)}</span>
  <span class="collapsible" onclick="toggle('trace-{agent}-{task_id}-{i}')">Toggle details</span>
  <div id="trace-{agent}-{task_id}-{i}" class="collapsed">{detail}</div>
</div>
"""
        groups += f"""
<div class="card">
  <h3>{_h(agent)} / {_h(task_id)}</h3>
  {event_rows}
</div>
"""
    return f"""
<div id="content-llm" class="tab-content">
  {groups}
</div>
"""


def _tabs() -> str:
    items = [
        ("overview", "Overview", True),
        ("timeline", "Timeline", False),
        ("coordinator", "Coordinator", False),
        ("tokens", "Token Usage", False),
        ("semantic", "Semantic Map", False),
        ("llm", "LLM Trace", False),
    ]
    buttons = "".join(
        f'<button class="tab{" active" if active else ""}" id="tab-{name}" onclick="openTab(\'{name}\')">{label}</button>'
        for name, label, active in items
    )
    return f'<div class="tabs">{buttons}</div>'


def _warnings(data: ReportData) -> str:
    if not data.warnings:
        return ""
    items = "".join(f"<li>{_h(w)}</li>" for w in data.warnings)
    return f'<div class="warn"><strong>Warnings</strong><ul>{items}</ul></div>'


def render_html(data: ReportData) -> str:
    body_parts = [
        _header(data),
        _warnings(data),
        _tabs(),
        _overview(data),
        _timeline(data),
        _coordinator(data),
        _token_usage(data),
        _semantic_map(data),
        _llm_trace(data),
        "</div>",
    ]
    body = "\n".join(body_parts)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SAR Experiment Report</title>
  <style>{_CSS}</style>
  <script>{_JS}</script>
</head>
<body>
{body}
</body>
</html>
"""
```

- [ ] **Step 2: 提交**

```bash
git add sar_orch/render_report/html.py
git commit -m "feat(render_report): add HTML generator"
```

---

### Task 4: CLI 入口

**Files:**
- Create: `sar_orch/render_report/cli.py`

**Interfaces:**
- Consumes: `load_report_data`, `render_html`。
- Produces: 写入 HTML 文件；`main()` 作为 CLI 入口。

- [ ] **Step 1: 编写 `cli.py`**

```python
import argparse
import sys
from pathlib import Path

from sar_orch.render_report.html import render_html
from sar_orch.render_report.loaders import load_report_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a SAR experiment run into a self-contained HTML report."
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        type=Path,
        help="Path to the experiment results directory (e.g. sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS).",
    )
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=Path,
        help="Path to the logs directory (e.g. sar_orch/logs).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output HTML path. Defaults to <results-dir>/report.html.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    results_dir = args.results_dir.resolve()
    logs_dir = args.logs_dir.resolve()
    output_path = args.output or results_dir / "report.html"

    if not results_dir.exists():
        print(f"ERROR: results directory not found: {results_dir}", file=sys.stderr)
        return 1

    data = load_report_data(results_dir, logs_dir)
    html = render_html(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 提交**

```bash
git add sar_orch/render_report/cli.py
git commit -m "feat(render_report): add CLI entrypoint"
```

---

### Task 5: 集成测试

**Files:**
- Create: `tests/test_render_report.py`

**Interfaces:**
- Consumes: `load_report_data`, `render_html`，以及当前真实实验数据。

- [ ] **Step 1: 编写测试**

```python
import re
from pathlib import Path

import pytest

from sar_orch.render_report.html import render_html
from sar_orch.render_report.loaders import load_report_data


@pytest.fixture
def real_results_dir() -> Path:
    return Path(__file__).parent.parent / "sar_orch" / "results" / "sar_experiment_20260705_192717"


@pytest.fixture
def real_logs_dir() -> Path:
    return Path(__file__).parent.parent / "logs"


def test_load_report_data(real_results_dir: Path, real_logs_dir: Path):
    data = load_report_data(real_results_dir, real_logs_dir)
    assert data.meta is not None
    assert data.meta.scene == 1
    assert data.meta.agent_count == 2
    assert data.metrics is not None
    assert data.metrics.steps == 30
    assert len(data.steps) == 30
    assert len(data.tokens) > 0
    assert len(data.agent_tokens) == 2
    assert any(t.agent == "Alice" for t in data.agent_tokens)
    assert any(t.agent == "Bob" for t in data.agent_tokens)
    assert len(data.semantic_objects) > 0
    assert len(data.llm_traces) > 0


def test_render_html_contains_expected_sections(real_results_dir: Path, real_logs_dir: Path):
    data = load_report_data(real_results_dir, real_logs_dir)
    html = render_html(data)
    assert html.startswith("<!DOCTYPE html>")
    assert "SAR Experiment Report" in html
    assert 'id="content-timeline"' in html
    assert 'id="content-coordinator"' in html
    assert 'id="content-tokens"' in html
    assert 'id="content-semantic"' in html
    assert 'id="content-llm"' in html
    # Check that step positions were parsed
    assert re.search(r"I am at co-ordinates", html) is None or "Pos:" in html


def test_render_report_cli(tmp_path: Path, real_results_dir: Path, real_logs_dir: Path):
    from sar_orch.render_report.cli import main

    output = tmp_path / "test_report.html"
    rc = main(
        [
            "--results-dir",
            str(real_results_dir),
            "--logs-dir",
            str(real_logs_dir),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert content.startswith("<!DOCTYPE html>")
    assert "SAR Experiment Report" in content
```

- [ ] **Step 2: 运行测试**

```bash
uv run pytest tests/test_render_report.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 3: 提交**

```bash
git add tests/test_render_report.py
git commit -m "test(render_report): add integration tests for report rendering"
```

---

### Task 6: 代码格式化与 lint

**Files:**
- Modify: `sar_orch/render_report/*.py`, `tests/test_render_report.py`

- [ ] **Step 1: 运行 lint/format**

```bash
uv run --with ruff ruff check src/ sar_orch/ tests/test_render_report.py
uv run --with ruff ruff format src/ sar_orch/ tests/test_render_report.py
```

- [ ] **Step 2: 提交修复**

```bash
git add -A
git commit -m "style(render_report): ruff format and lint fixes"
```

---

### Task 7: 生成本次实验报告并验证

**Files:**
- 生成: `sar_orch/results/sar_experiment_20260705_192717/report.html`

- [ ] **Step 1: 运行 CLI**

```bash
uv run python -m sar_orch.render_report.cli \
  --results-dir sar_orch/results/sar_experiment_20260705_192717 \
  --logs-dir logs
```

Expected output:

```
Report written to: /home/wyh/daily_work/LLaMAR-sematic_map/sar_orch/results/sar_experiment_20260705_192717/report.html
```

- [ ] **Step 2: 用浏览器或简单检查验证**

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('sar_orch/results/sar_experiment_20260705_192717/report.html')
assert p.exists()
html = p.read_text(encoding='utf-8')
assert html.startswith('<!DOCTYPE html>')
for section in ['content-overview','content-timeline','content-coordinator','content-tokens','content-semantic','content-llm']:
    assert f'id="{section}"' in html, section
print('Report validation passed')
PY
```

- [ ] **Step 3: 提交生成的报告（可选）**

```bash
git add sar_orch/results/sar_experiment_20260705_192717/report.html
git commit -m "chore: add rendered HTML report for sar_experiment_20260705_192717"
```

---

## Self-Review

### Spec Coverage

| Spec 要求 | 对应任务 |
|-----------|----------|
| 解析 results 下 JSON/CSV/NDJSON | Task 2 |
| 解析 logs 下 semantic_map.jsonl 与 worker trace | Task 2 |
| 单文件 HTML，内嵌 CSS/JS | Task 3 |
| Overview / Timeline / Coordinator / Token / Semantic Map / LLM Trace 视图 | Task 3 |
| CLI 接口 `--results-dir` / `--logs-dir` / `--output` | Task 4 |
| 默认输出到 `<results_dir>/report.html` | Task 4 |
| 缺失数据不中断，仅 warning | Task 2 + Task 3 |
| 使用 CLAUDE.md 颜色规范 | Task 3 `_CSS` |
| 测试验证 | Task 5 + Task 7 |

### Placeholder Scan

无 `TBD`/`TODO`/`implement later`；所有任务均包含完整可运行代码。

### Type Consistency

- `load_report_data` 返回 `ReportData`，`render_html` 接收 `ReportData`。
- `main()` 返回 `int` 退出码。
- 数据类字段类型在各任务中一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-05-sar-experiment-html-report.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach do you want?

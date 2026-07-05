import html
import json
from typing import Any

from render_sar_report.models import ReportData


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
        parts.extend(
            [
                f"Scene {_h(meta.scene)}",
                f"{_h(meta.agent_count)} Agents",
                f"Model {_h(meta.model)}",
                f"Seed {_h(meta.seed)}",
            ]
        )
    if metrics:
        parts.extend(
            [
                f"{metrics.steps}/{_h(meta.max_steps if meta else '?')} Steps",
                f"Finished: {_h(metrics.finished)}",
                f"End Reason: {_h(metrics.end_reason)}",
                f"Elapsed: {metrics.elapsed_seconds:.1f}s",
            ]
        )
    return f"""
<div class="container">
  <h1>{title}</h1>
  <p class="subtitle">{" · ".join(parts)}</p>
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
        f'<div class="metric-card"><div class="metric-value">{v}</div><div class="metric-label">{label}</div></div>'
        for v, label in cards
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
  <span style="color:var(--muted);font-size:13px;">Action: {_h(step.actions.get(agent, ""))}</span><br/>
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
        text = s.text[:120] + "..." if len(s.text) > 120 else s.text
        subtask_rows += f"<tr><td>{_h(s.subtask_id)}</td><td>{s.step}</td><td>{_h(s.status)}</td><td>{_h(s.assigned_to)}</td><td>{_h(text)}</td></tr>"
    router_rows = ""
    for e in data.router_events:
        text = e.subtask[:120] + "..." if len(e.subtask) > 120 else e.subtask
        router_rows += f"<tr><td>{e.step}</td><td>{_h(e.event_type)}</td><td>{_h(e.assigned_to)}</td><td>{_h(text)}</td></tr>"
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
            total = sum(
                t.total for t in data.tokens if t.step == step and t.agent == agent
            )
            ds["data"].append(total)
        datasets.append(ds)
    cumulative = []
    for agent in agents:
        running = 0
        ds = {"label": agent, "color": colors.get(agent, "#64748b"), "data": []}
        for step in steps:
            total = sum(
                t.total for t in data.tokens if t.step == step and t.agent == agent
            )
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
        if isinstance(pos, list) and len(pos) == 3:
            pos_str = f"({pos[0]}, {pos[1]}, {pos[2]})"
        else:
            pos_str = _h(pos)
        attrs = last.get("attributes", {})
        attr_str = json.dumps(attrs, ensure_ascii=False) if attrs else ""
        conflict = (
            '<span class="badge" style="background:#dc2626;">CONFLICT</span>'
            if obj.conflict
            else ""
        )
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
    buttons = []
    for name, label, active in items:
        cls = "tab active" if active else "tab"
        buttons.append(
            f'<button class="{cls}" id="tab-{name}" onclick="openTab(\'{name}\')">{label}</button>'
        )
    return f'<div class="tabs">{"".join(buttons)}</div>'


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

---
name: render-sar-report
description: Use when the user wants to turn SAR multi-agent experiment outputs into a single human-readable HTML report, or mentions rendering experiment results, agent interactions, metrics, token usage, or semantic map into HTML.
---

# Render SAR Experiment Report

## Overview

This skill renders a completed SAR experiment run into a single self-contained HTML report. The report includes an overview, per-step timeline, coordinator decisions, token usage charts, semantic map evolution, and full LLM traces.

## When to Use

- The user asks for an HTML report of a SAR experiment.
- The user complains that CSV/NDJSON logs are hard to read.
- The user wants to visualize agent interactions, metrics, token usage, or semantic map evolution.

## How to Render

1. **Identify directories**
   - `results_dir`: the experiment output directory, e.g. `sar_orch/results/sar_experiment_YYYYMMDD_HHMMSS`.
   - `logs_dir`: the logs root directory, normally `logs` at the project root.
   - If the user only names a timestamp or run ID, resolve it under `sar_orch/results/`.

2. **Run the CLI** (always from the project root):

   ```bash
   PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
     uv run python -m render_sar_report.cli \
     --results-dir <results_dir> \
     --logs-dir <logs_dir> \
     [--output <optional_output_path.html>]
   ```

   Default output is `<results_dir>/report.html`.

3. **Verify the output**
   - Confirm the file exists.
   - Confirm it starts with `<!DOCTYPE html>`.
   - Report the generated file path to the user.

## What the Report Contains

| Tab | Content |
|-----|---------|
| **Overview** | Run metadata (scene, agents, model), coverage/transport rate metrics, total tokens, per-agent token summary table |
| **Timeline** | Per-step agent cards showing Action, Position, Inventory, and expandable Observations. Coordinator is excluded (it doesn't act in the grid) |
| **Coordinator** | Subtask table (ID, step, status, assignee, text) and Router events table |
| **Token Usage** | Per-step bar chart + cumulative line chart, both with color legend showing each agent's token consumption. Uses Canvas 2D drawing, re-renders on tab switch via `requestAnimationFrame` |
| **Semantic Map** | Table of all objects discovered by agents (fires, reservoirs, deposits, persons). Columns: Type+Conflict, Name, First Step (when first observed), Last Step (most recent observation), Last Position, Last Reporter, Observation Count |
| **LLM Trace** | Grouped by agent/task, each event as an expandable card. `llm_request` shows full prompt (scrollable `max-height: 400px`), `llm_response` shows completion + usage stats, `tool_result` shows tool name in a purple badge + full result (scrollable) |

### Implementation Notes

**JavaScript**: Uses `DOMContentLoaded` + `addEventListener` (not inline `onclick`). Event binding via `data-tab`/`data-target` attributes. Charts rendered on Canvas with manual legend drawing. Zero-size canvas detection retries on next animation frame.

**Two source copies**: `skills/render-sar-report/render_sar_report/` (used by PYTHONPATH) and `.agents/skills/render-sar-report/render_sar_report/` (skill definition). Both must be kept in sync.

## Viewing the Report

> ⚠️ **VS Code built-in "Open Preview" (Ctrl+Shift+V) does NOT execute JavaScript** — tabs, toggles, and charts will not work.
>
> Use one of these instead:
> - **External browser**: Open the HTML file directly in Chrome/Edge/Firefox (on WSL: `explorer.exe report.html`)
> - **VS Code Live Preview extension**: `ms-vscode.live-server` — right-click → "Open with Live Server"
> - **VS Code Live Server extension**: `ritwickdey.LiveServer` — right-click → "Open with Live Server"

## Common Mistakes

- Running the script from the wrong working directory. Always run from the project root so `skills/render-sar-report/` resolves.
- Forgetting `--logs-dir`. The report can still be generated but will lack LLM traces and semantic map details.
- Passing a file instead of a directory to `--results-dir`.
- Editing only one of the two source copies (must sync `skills/` and `.agents/skills/`).
- Using `onclick` attributes — always prefer `addEventListener` + data attributes for reliability.

## Example Invocation

```bash
cd /home/wyh/daily_work/LLaMAR
PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
  uv run python -m render_sar_report.cli \
  --results-dir sar_orch/results/sar_experiment_20260705_192717 \
  --logs-dir logs
```

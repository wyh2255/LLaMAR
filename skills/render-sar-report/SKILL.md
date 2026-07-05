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

- **Overview**: run metadata, coverage, transport rate, total tokens, per-agent token summary.
- **Timeline**: per-step actions, positions, inventories, observations for each agent.
- **Coordinator**: subtasks and router events.
- **Token Usage**: per-step and cumulative token charts.
- **Semantic Map**: objects (fires, reservoirs, persons, deposits) and their observed evolution.
- **LLM Trace**: expandable worker LLM requests/responses/tool results.

## Common Mistakes

- Running the script from the wrong working directory. Always run from the project root so `skills/render-sar-report/` resolves.
- Forgetting `--logs-dir`. The report can still be generated but will lack LLM traces and semantic map details.
- Passing a file instead of a directory to `--results-dir`.

## Example Invocation

```bash
cd /home/wyh/daily_work/LLaMAR
PYTHONPATH="skills/render-sar-report:$PYTHONPATH" \
  uv run python -m render_sar_report.cli \
  --results-dir sar_orch/results/sar_experiment_20260705_192717 \
  --logs-dir logs
```

---
name: sar-paper-benchmark
description: Use when running SAR paper-spec experiment batches (scene/agents/seed matrix, max_steps=30) in parallel with isolated ports, evaluating them with the eval agent, and comparing against a pre-change baseline. Covers the pitfall list learned the hard way (pkill self-match, lost background tasks, port/log-dir isolation, baseline re-eval with current code).
---

# SAR Paper-Spec Benchmark + Eval Loop

## Overview

Run N SAR experiment groups in parallel following the original LLaMAR paper §5 grid, evaluate every run with `sar_orch.eval.cli`, and compare against a pre-change baseline to judge whether a framework change helped. This skill encodes the exact commands and the pitfalls hit when running this loop on this repo (2026-07-30).

## Paper Spec (原论文规范)

- Matrix: scenes 1–5 × agents {2,3,4,5} × seeds {0,10,20,30,40} (full sweep = 100 cells; a partial batch picks a subset)
- Step cap: `max_steps=30` (`PAPER_MAX_STEPS`, already the default in both `sar_orch/experiment.py` and `sar_orch/benchmark.py`)
- Mode: `semantic` (default), model from `.env` (lowercase keys: `provider`, `api_key`, `api_base`, `model`)

## Running a Parallel Batch

`sar_orch/benchmark.py` only supports `--scene` filtering and the full 100-cell matrix — for a custom subset (e.g. 10 specific cells), launch `experiment.py` directly:

```bash
cd /home/wyh/daily_work/LLaMAR_evel
i=0
for spec in "1 2 42" "1 5 42" ...; do          # "scene agents seed"
  set -- $spec
  cp=$((18080+20*i)); ap=$((18191+20*i))         # port block stride 20
  dir="sar_orch/results/$(date +%Y%m%d_%H%M%S)_post30_s$1_s$3_a$2"
  env no_proxy="localhost,0.0.0.0,127.0.0.1,${no_proxy:-}" PYTHONPATH="src:${PYTHONPATH:-}" \
    uv run python sar_orch/experiment.py --scene "$1" --agents "$2" --seed "$3" \
    --max-steps 30 --wall-clock-limit 0 \
    --coordinator-port "$cp" --agent-base-port "$ap" --log-dir "$dir" \
    > "${dir}.console.log" 2>&1 &
  i=$((i+1))
done
wait
```

Rules that are NOT optional:

- **Explicit port block per run** (`--coordinator-port` / `--agent-base-port`, stride ≥ 10): each run occupies coordinator HTTP+A2A plus one port per worker. Default 8080/8191 collides immediately.
- **Explicit `--log-dir`**: timestamped default dirs collide when runs start in the same second; it also makes run dirs self-describing (`..._s{scene}_s{seed}_a{agents}`).
- **`--wall-clock-limit 0`** when the user says time is unlimited — default is a 3600s safety net that can kill a legitimate 30-step run.
- **Keep `http_proxy`/`https_proxy`, extend `no_proxy`**: the LLM gateway (`.env` `api_base`, e.g. packyapi) is reached THROUGH the proxy; localhost coordinator/worker traffic must bypass it. Do not strip proxy vars like `benchmark.py` does unless the gateway is direct-reachable.
- Parallelism: 10 concurrent runs froze WSL on this machine; **5 concurrent is the known-good level**.

## Batch Chaining Without Polling

`wait` only works for a shell's own children. To auto-launch batch 2 when batch 1 finishes, use a background watcher that polls PIDs:

```bash
while true; do
  alive=0
  for p in $B1_PIDS; do kill -0 "$p" 2>/dev/null && alive=$((alive+1)); done
  [ "$alive" -eq 0 ] && break
  sleep 60
done
# launch batch 2 here
```

## Pitfalls (踩过的坑)

1. **`pkill -f <pattern>` kills your own tool shell.** The Bash tool's shell command line contains the pattern string, so pkill matches and kills the caller itself (exit code -1, half-finished work). Use a character class so the regex matches targets but not the literal string in your own cmdline: `pkill -f "post30_s2_s4[2]_a2"`.
2. **"Background process lost" ≠ processes dead.** After a WSL freeze the task tracker reports `lost`, but the experiment processes kept running fine. Always `ps aux | grep experiment.py` before deciding to kill or relaunch — don't abandon live runs, and don't double-launch.
3. **Killed runs leave partial dirs.** Delete the truncated run dirs + console logs before relaunching the same cells, or the eval aggregator later counts them as unevaluated/failed episodes.
4. **glob picks up `.console.log` files.** Analysis scripts iterating `sar_orch/results/<batch>_*` must filter `os.path.isdir()`, else `NotADirectoryError` on `<dir>.console.log/eval_report.json`.
5. **`eval_report.json` shapes**: `constraint_violations` is a **list** of violation objects (not a dict with a total); `llm_judge.observation.hallucination_rate` can be `null` (0 sampled claims); dispatch judge can be missing entirely on old runs. Code defensively.
6. **Push-callback `ReadError` tracebacks in console logs are transient** (network read on A2A push notification) and non-fatal — don't kill a run over them; check `trajectory.csv` step progress instead.
7. **Interpreting results**: `agents=2` hitting `max_steps_reached` on scenes 1–2 is a stable capacity boundary (2 agents can't finish in 30 steps), NOT a framework regression. The framework-defect signal is `end_reason=framework_error` and high `constraint_violations`.

## Evaluation + Baseline Comparison

```bash
# Per-run eval (deterministic graders + LLM judge), ~5-7 min each; 5 concurrent is fine:
env no_proxy="localhost,0.0.0.0,127.0.0.1,${no_proxy:-}" PYTHONPATH="src:${PYTHONPATH:-}" \
  uv run python -m sar_orch.eval.cli --results-dir sar_orch/results/<run_dir>

# Baseline runs already have eval_report.json from an OLDER eval version.
# For a fair deterministic comparison, re-grade the baseline with CURRENT code,
# zero LLM cost, writing to a SEPARATE file so the original judge data survives:
uv run python -m sar_orch.eval.cli --results-dir <baseline_dir> --no-llm-judge \
  --output <baseline_dir>/eval_report_current.json
```

Comparison discipline:

- Pair cells exactly: same (scene, agents, seed, max_steps, model, mode). A baseline at seed 42 does not pair with a new run at seed 0.
- Core metrics live in `eval_report.json -> episode` (`finished`, `coverage`, `transport_rate`, `balance`, `steps`, `end_reason`); judge quality in `llm_judge.dispatch.pass_rate` and `llm_judge.observation.hallucination_rate`; safety in `len(constraint_violations)`.
- Judge/subject same model (`deepseek-v4-flash` judging `deepseek-v4-flash`) triggers `same_model_warning` — it applies equally to baseline and new runs, so the comparison stays valid, but absolute judge scores are optimistic.

## Reference Result (2026-07-30, post-fix b8450e3 vs pre-fix ee06568)

10 cells (6 paired scene-1 + 4 scene-2), max_steps=30, deepseek-v4-flash: SR 0/6 → 3/6 on paired cells; `framework_error` endings 3 → 0; dispatch pass_rate mean 0.078 → 0.186; hallucination_rate mean 0.433 → 0.052; violations/run 11.3 → 7.3.

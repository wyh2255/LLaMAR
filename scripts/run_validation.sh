#!/bin/bash
# Framework A/B validation chain: experiment -> per-run eval -> aggregate -> gate.
#
# Replaces .agents/workspace/run_cell_chain.sh, which stopped after per-run eval.
# That left the two steps that actually produce a verdict -- aggregation (CI,
# pass@k) and the regression gate -- to be run by hand, or not at all. Every
# tuning round so far compared raw pass@1 counts with no interval, which is how
# a 40 pp swing (4/5 -> 2/5) got read as a real effect when Fisher gives p=0.52.
#
# LLM config is a CONSTANT here, not a comparison axis: sampling params do not
# bind on this gateway (measured -- same seed and even temperature=0 diverge).
# So this script pins model/provider/temperature from .env for every run in the
# batch and the aggregate layer fails the gate on any within-batch config drift.
# What varies between batches is the FRAMEWORK: prompts, skills, code.
#
# Usage:
#   scripts/run_validation.sh <tag> [--baseline <dir-or-aggregate.json>]
#                                   [--repeats N] [--cells <file>]
#                                   [--prompt-dir D] [--skills-dir D]
#                                   [--max-steps N] [--judge] [--parallel N]
#
# --parallel N runs up to N experiments concurrently (default 1 = serial).
# A 3-run probe (2026-08-02) found no rate-limit damage from concurrency on
# this gateway (timeout_steps 1/2/2, all exit 0) -- but that was n=3, not a
# guarantee. A batch run with --parallel > 1 gets an execution_mode.json
# marker and a console warning: rate-limit damage is NOT necessarily
# identical to a serial batch, so cross-batch deltas should not be
# attributed to the framework alone unless both batches share the setting.
#
# Exit: 0 = gate passed, 1 = gate failed, 2 = usage/setup error.

set -uo pipefail

usage() { sed -n '2,28p' "$0" >&2; exit 2; }

[ $# -ge 1 ] || usage
TAG="$1"; shift

REPEATS=3
BASELINE=""
CELLS_FILE=""
PROMPT_DIR=""
SKILLS_DIR=""
MAX_STEPS=30
PARALLEL=1
# Judge is off by default: it was demoted to a diagnostic (its scoring anchors
# contradicted the system's own design), it does not gate, and it costs dozens
# of extra LLM calls per run.
JUDGE_FLAG="--no-llm-judge"

while [ $# -gt 0 ]; do
  case "$1" in
    --baseline)   BASELINE="$2"; shift 2 ;;
    --repeats)    REPEATS="$2"; shift 2 ;;
    --cells)      CELLS_FILE="$2"; shift 2 ;;
    --prompt-dir) PROMPT_DIR="$2"; shift 2 ;;
    --skills-dir) SKILLS_DIR="$2"; shift 2 ;;
    --max-steps)  MAX_STEPS="$2"; shift 2 ;;
    --judge)      JUDGE_FLAG=""; shift ;;
    --parallel)   PARALLEL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

case "$REPEATS" in ''|*[!0-9]*) echo "--repeats must be a positive integer" >&2; exit 2 ;; esac
[ "$REPEATS" -ge 1 ] || { echo "--repeats must be >= 1" >&2; exit 2; }

case "$PARALLEL" in ''|*[!0-9]*) echo "--parallel must be a positive integer" >&2; exit 2 ;; esac
[ "$PARALLEL" -ge 1 ] || { echo "--parallel must be >= 1" >&2; exit 2; }

# min_runs in the gate config is 2. At repeats=1 every group has n=1, so all
# regression checks silently downgrade to warn and the gate reports PASSED --
# a batch that cannot fail is worse than no gate, because it looks like a check.
if [ "$REPEATS" -lt 2 ]; then
  echo "WARNING: --repeats 1 gives n=1 per group; the gate downgrades every" >&2
  echo "         check to warn and will report PASSED regardless of results." >&2
fi

cd "$(dirname "$0")/.." || exit 2
export PYTHONPATH="src:${PYTHONPATH:-}"
export no_proxy="localhost,0.0.0.0,127.0.0.1,${no_proxy:-}"

TS=$(date +%Y%m%d_%H%M%S)
BATCH="sar_orch/results/${TS}_${TAG}"
mkdir -p "$BATCH" || exit 2

# Default cells: the 5 paired units used by every round so far.
if [ -n "$CELLS_FILE" ]; then
  [ -r "$CELLS_FILE" ] || { echo "cannot read --cells $CELLS_FILE" >&2; exit 2; }
  CELLS=$(grep -vE '^\s*(#|$)' "$CELLS_FILE")
else
  CELLS="1 42 2
1 43 2
1 44 2
2 42 2
2 43 2"
fi

EXTRA_ARGS=()
[ -n "$PROMPT_DIR" ] && EXTRA_ARGS+=(--prompt-dir "$PROMPT_DIR")
[ -n "$SKILLS_DIR" ] && EXTRA_ARGS+=(--skills-dir "$SKILLS_DIR")

echo "=== batch ${BATCH} ==="
echo "    tag=${TAG} repeats=${REPEATS} max_steps=${MAX_STEPS} parallel=${PARALLEL}"
echo "    cells:"; echo "$CELLS" | sed 's/^/      /'
[ -n "$PROMPT_DIR" ] && echo "    prompt-dir=${PROMPT_DIR}"
[ -n "$SKILLS_DIR" ] && echo "    skills-dir=${SKILLS_DIR}"
if [ "$PARALLEL" -gt 1 ]; then
  echo "    WARNING: --parallel ${PARALLEL} -- rate-limit damage is not"
  echo "             guaranteed identical to a serial batch. Do not compare"
  echo "             timeout_steps or attribute deltas to the framework"
  echo "             against a batch with a different --parallel setting."
fi
echo
cat > "${BATCH}/execution_mode.json" <<EOF
{"parallel": ${PARALLEL}}
EOF

# ---------------------------------------------------------------------------
# STEP 1 -- experiments. Serial by default; --parallel N runs up to N at once.
#
# Concurrency risk is a rate limiter, not a correctness issue: each run gets
# its own port pair, so concurrent runs cannot collide on state. A 3-run probe
# found no rate-limit damage (timeout_steps 1/2/2 vs a serial-batch mean of
# 2.9), but that is a small sample, not a guarantee for every gateway load
# condition -- hence the execution_mode.json marker and warning above.
# ---------------------------------------------------------------------------
echo "=== STEP 1: experiments (parallel=${PARALLEL}) ==="
STATUS_FILE=$(mktemp)
i=0
while read -r scene seed agents; do
  [ -n "${scene:-}" ] || continue
  for rep in $(seq 1 "$REPEATS"); do
    CP=$((18080 + 20*i)); AP=$((18191 + 20*i))
    DIR="${BATCH}/s${scene}_s${seed}_a${agents}_r${rep}"
    mkdir -p "$DIR"
    (
      uv run python sar_orch/experiment.py \
        --scene "$scene" --agents "$agents" --seed "$seed" \
        --max-steps "$MAX_STEPS" --wall-clock-limit 0 \
        --coordinator-port "$CP" --agent-base-port "$AP" \
        --prompt-tag "$TAG" \
        "${EXTRA_ARGS[@]}" \
        --log-dir "$DIR" > "${DIR}/console.log" 2>&1
      rc=$?
      if [ $rc -eq 0 ]; then
        echo "  ok   $DIR" | tee -a "$STATUS_FILE"
      else
        echo "  FAIL $DIR (exit $rc, see ${DIR}/console.log)" | tee -a "$STATUS_FILE"
        echo "FAIL" >> "${STATUS_FILE}.rc"
      fi
    ) &
    i=$((i+1))
    # Bound concurrency to PARALLEL: once at the cap, wait for any one job to
    # finish before launching the next.
    while [ "$(jobs -r -p | wc -l)" -ge "$PARALLEL" ]; do
      wait -n
    done
  done
done <<< "$CELLS"
wait
FAILED_RUNS=0
[ -f "${STATUS_FILE}.rc" ] && FAILED_RUNS=$(wc -l < "${STATUS_FILE}.rc")
rm -f "$STATUS_FILE" "${STATUS_FILE}.rc"
echo "  runs failed: ${FAILED_RUNS}"

# ---------------------------------------------------------------------------
# STEP 2 -- per-run eval (deterministic graders).
# ---------------------------------------------------------------------------
echo
echo "=== STEP 2: per-run eval ==="
EVAL_FAILED=0
for DIR in "${BATCH}"/*/; do
  [ -f "${DIR}trajectory.csv" ] || continue
  if uv run python -m sar_orch.eval.cli --results-dir "$DIR" $JUDGE_FLAG \
       > "${DIR}eval.log" 2>&1; then
    echo "  ok   ${DIR}"
  else
    echo "  FAIL ${DIR} (see ${DIR}eval.log)"
    EVAL_FAILED=$((EVAL_FAILED+1))
  fi
done
echo "  evals failed: ${EVAL_FAILED}"

# ---------------------------------------------------------------------------
# STEP 3 -- aggregate: pass@k + Clopper-Pearson CI, grouped by (scene, agents).
#
# This is the step whose absence made every previous round unreadable: without
# an interval, "4/5 vs 2/5" is a number with no error bar.
# ---------------------------------------------------------------------------
echo
echo "=== STEP 3: aggregate ==="
uv run python -m sar_orch.eval.aggregate --results-root "$BATCH" || {
  echo "aggregate failed" >&2; exit 2; }

# ---------------------------------------------------------------------------
# STEP 4 -- gate. Verdict lives here, in code, not in a human reading a table.
# ---------------------------------------------------------------------------
echo
echo "=== STEP 4: gate ==="
GATE_ARGS=(--results-root "$BATCH")
if [ -n "$BASELINE" ]; then
  GATE_ARGS+=(--baseline "$BASELINE")
  echo "    baseline: ${BASELINE}"
else
  echo "    no baseline given -- absolute checks only (no regression detection)"
fi
uv run python -m sar_orch.eval.gate "${GATE_ARGS[@]}"
GATE_RC=$?

echo
echo "=== SUMMARY ==="
echo "  batch:        ${BATCH}"
echo "  runs failed:  ${FAILED_RUNS}"
echo "  evals failed: ${EVAL_FAILED}"
echo "  gate exit:    ${GATE_RC}"
echo "  reports:      ${BATCH}/aggregate_report.{json,md}"
echo "                ${BATCH}/gate_report.{json,md}"

# A green gate over a batch where runs died is not a pass: the missing runs are
# usually the failures, so their absence biases every metric upward.
if [ "$FAILED_RUNS" -gt 0 ] || [ "$EVAL_FAILED" -gt 0 ]; then
  echo
  echo "  NOTE: ${FAILED_RUNS} run(s) and ${EVAL_FAILED} eval(s) failed. Metrics"
  echo "        below are computed over the surviving runs only and are biased"
  echo "        upward if the missing ones failed. Do not compare this batch"
  echo "        against a complete one."
fi

exit $GATE_RC

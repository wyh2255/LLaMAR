---
name: sar-harness-agent-improvement
description: Use when improving the SAR harness agent (coordinator/worker prompts, dispatch strategy, orchestration behavior) to raise success rate — e.g. fixing the drop-off deadlock or not_visible flailing. Enforces anti-cheating rules: no pre-written answers in prompts, no weakening env mechanics (carry needs 2 agents, drop_off needs ALL coupled agents present at the deposit), no metric gaming.
---

# SAR Harness Agent Improvement (with integrity constraints)

## Overview

Improve SAR multi-agent success rate the honest way: better prompts, better task decomposition, better deadlock handling, better fallback behavior. This skill exists because the obvious shortcuts are all cheating. It applies to work under `sar_orch/` (prompts, coordinator/worker tools, watchdog, orchestration) — NOT to the physics of `SAR/core.py`.

## Hard Rule 1 — Environment mechanics are inviolable

The two-agent collaboration semantics are part of the benchmark and MUST NOT be weakened, bypassed, or auto-completed by the harness:

- **Carry**: a Person can only be picked up when **≥2 agents are coupled** to it (`Person.MIN_REQUIRED_AGENTS = 2`, `SAR/core.py:1164`; `load = extraload + 2`). Both agents must navigate to the person and each call `carry_person`.
- **Drop-off**: a rescue completes only when **ALL coupled agents are at the deposit and every one of them has called `drop_off_person`** (`Person._drop`, `SAR/core.py:1278-1299`). Both agents must be physically present at the deposit — one agent alone cannot finish the drop.

Forbidden changes (non-exhaustive):

- Lowering `MIN_REQUIRED_AGENTS` / `extraload`, or editing `Person.pick` / `Person.drop` / `Person._drop` to relax coupling, presence, or all-dropped requirements.
- Making the harness auto-issue `drop_off_person` for an agent, teleporting agents/persons, or faking a successful drop in logs.
- "Fixing" the eval (`sar_orch/eval/`) graders, the rescue_flow check, or the env checker so incomplete rescues count as complete.

If a proposed diff touches `SAR/core.py` physics, the checker (`SAR/Scenes/checker.py`, `base_checker.py`), or grader pass criteria — stop and flag it; that is changing the exam, not the student.

## Hard Rule 2 — No pre-written answers (禁止投机取巧)

Improvements must generalize. Anything that smuggles episode-specific answers into the agent is cheating:

- **Never** hardcode per-scene/per-seed facts into prompts, tools, or code: person names, fire positions/types, deposit coordinates, which supply a fire needs, action sequences ("step 24 carry, step 27 navigate, step 28 drop_off"), or scene-specific scripts.
- **Never** route around `--mode semantic` to give the coordinator oracle/ground-truth it is supposed to discover via worker observations.
- **Never** tune prompts against the eval judge's sampled steps or against a specific seed's trajectory; tune against failure *mechanisms*, not instances.
- **Never** special-case `agents=2` with scripted behavior; the same prompt must run the full paper grid (scenes 1–5 × agents {2,3,4,5} × seeds {0,10,20,30,40}).

What IS allowed in prompts (not cheating): **general, episode-independent rules** that are true for every run — e.g. stating the documented env mechanics ("a person needs 2 carriers; every carrier must call drop_off_person at the deposit; calls need not be simultaneous"), general strategy ("if navigate_to fails with not_visible, explore first"), and output-format instructions. The test: would this sentence still be true and fair on an unseen scene with an unseen seed? If not, it does not belong in the prompt.

## Known failure mechanisms (from 2026-07-30 analysis) and legitimate fixes

1. **Drop-off deadlock** (4/5 failed runs): both agents carry → both navigate to deposit → both `no_op` waiting for each other → step cap, 0 drop_off calls. LLM misreads the semantics as "must drop simultaneously"; coordinator only polls `query_task_events`, never dispatches the final drop instruction.
   - Coordinator prompt: rescue chain template must END with `drop_off_person` for BOTH coupled agents; on observing "2 carriers + both at deposit", dispatch the drop instruction immediately instead of polling.
   - Worker prompt / `drop_off_person` docstring: state the true semantics — all coupled agents each call drop_off once at the deposit; NOT required in the same step; waiting for the partner to "act together" is wrong.
   - TaskWatchdog: act on TASK_STALE when agents are carrying + idle no_op (currently the event is emitted but the coordinator does not react).
2. **not_visible flailing** (s1_s44_a2): repeated `navigate_to` on never-discovered fire regions instead of exploring first.
   - Worker prompt/tooling: on `not_visible` failure, fall back to `explore()`; coordinator should not dispatch navigate targets that have no confirmed position in the semantic map.
3. **2-agent capacity is tight** — with `load=2`, the whole episode rides on one synchronized pair. Improve reliability of the pair protocol; do NOT reduce the required headcount.

## Validation protocol

1. Re-run the paired cells with the paper-spec loop (see skill `sar-paper-benchmark`): same (scene, agents, seed, max_steps=30, model, mode), at least the 6 scene-1 cells, ideally all 10.
2. Compare `eval_report.json` episode metrics + `len(constraint_violations)` + judge rates against the pre-change baseline. Improvement claims require the SAME eval code version on both sides.
3. Anti-cheat audit before claiming success: `git diff` must show no changes to `SAR/core.py`, `SAR/Scenes/`, or `sar_orch/eval/` pass criteria; grep new prompts for person names, coordinates, seed/scene literals, and scripted action sequences.
4. A fix that only helps the exact cells you tuned on is overfitting — spot-check at least one unseen (scene, seed) cell.

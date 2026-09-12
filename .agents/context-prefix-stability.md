# Context Prefix-Stability Contract

Status: active — P1 cache optimization (2026-09-12)
Scope: SAR agent context managers — `src/Agent/worker_agent/context.py`,
`src/Agent/router_agent/context.py`, and their `ContextConfig`.
Switch: `ContextConfig.prune_policy` (`count_window` | `prefix_stable`), wired
`sar_orch/experiment.py --prune-policy` → `SARWorker` / `SARCoordinator` →
`ContextConfig`; the effective value is recorded in `metadata.json` as
`prune_policy`.

## 1. Why this contract exists

DeepSeek-family (auto-prefix) caching matches a request prefix token-by-token
(128-token blocks). Rewriting, removing, or left-shifting **any** history
message that was already sent to the model invalidates the cached prefix from
that point on. The measured root cause of the 46% cache hit rate
(`.hermes/spec/cache-hit-rate/README.md`) was the worker count-based sliding
window: every prune slid the whole history left, pinning the divergence point
at history index 2 (1,111 events / 50.6% of worker requests).

## 2. Zones

| Zone | Contents | Mutability |
|---|---|---|
| **Prefix zone** | `messages[0]` system prompt (incl. Output / Response Contract), `messages[1]` task message, and every history message already written (assistant / tool / user nudge) | **IMMUTABLE** once written: no removal, no rewrite, no left shift, no re-ordering |
| **Tail zone** | the trailing `role=user` state block re-rendered per request by `assemble()` (Environment State, team status, task plan, mailbox, diagnosis, long-term memory) | Re-rendered every request; **never written into the history** |
| **Appended tail** | new assistant/tool messages for the current turn; `_inject_continue_nudge()` | Append-only |

## 3. Rules (normative)

- **R1** — Under `prune_policy="prefix_stable"`, `prune_history()` must not
  remove, rewrite, or shift any message already in the list (worker
  `count_prune` and phase 1 are both disabled).
- **R2** — Phase 1 (`_compress_phase1`) rewrites long tool outputs in the
  middle zone → forbidden under `prefix_stable`; the guard returns before any
  write.
- **R3** — Phase 3 (`compress_with_llm`) replaces the middle zone with an LLM
  summary → refuses to run under `prefix_stable` (`return False`). It is dead
  code today (no call sites); it must be redesigned as a prefix-safe
  (append-only / tail) compression before it may be wired in.
- **R4** — Volatile content enters a request only through the trailing state
  block appended by `assemble()`, never into the history.
- **R5** — Sole exception, extreme-overflow watermark: when the estimated
  request size reaches `prune_overflow_ratio × token_limit` (default `1.0`, the
  configured model context budget itself), long tool outputs **in the newest
  window** (`recent_messages`) are truncated in place — newest first, and only
  until enough tokens are reclaimed (`_prune_prefix_stable`). Normal runs
  (≤35 env steps) never reach the watermark. Trace:
  `context/prune_events.ndjson` with `policy="prefix_stable"`,
  `trigger="overflow_watermark"`.
- **R6** — `count_window` is the legacy control/rollback arm and is *not*
  covered by this contract: it keeps the legacy sliding window plus phase 1
  byte-for-byte (it violates R1 by design). Any comparison run must keep the
  default policy.

## 4. Enforcement map (as of 2026-09-12)

| Path | Worker | Router (coordinator) |
|---|---|---|
| count window (`count_prune`) | `worker_agent/context.py` `prune_history()` — legacy path; not reached under `prefix_stable` | not implemented (never ran a count window) |
| phase 1 guard | `worker_agent/context.py` `_compress_phase1()` — `if self._prefix_stable(): return` | `router_agent/context.py` `_compress_phase1()` — same guard |
| phase 3 guard | `worker_agent/context.py` `compress_with_llm()` — `if self._prefix_stable(): return False` | `router_agent/context.py` `compress_with_llm()` — same guard |
| overflow watermark | `worker_agent/context.py` `_prune_prefix_stable()` | `router_agent/context.py` `_prune_prefix_stable()` |
| tail state block | `worker_agent/context.py` `assemble()` (appends after history) | `router_agent/context.py` `assemble()` (appends after history) |
| policy switch | `ContextConfig.prune_policy` / `prune_overflow_ratio` | same fields |

## 5. Audited message-mutation sites outside the prune path

- `agent._cleanup_incomplete_messages()` — drops the incomplete tail turn
  (protocol closure). Tail-only: keeps the prefix valid.
- `agent._summarize_messages()` — rewrites the whole list; **dead code**
  (no call sites). Must not be wired in under `prefix_stable` without a
  prefix-safe redesign.
- `_sanitize_tool_pairs()` — reached only from phase 4 (dead code, guarded
  through the phase 3 guard).
- pinned / episodic memory (`observe()`, `_prune_episodic()`) — never touches
  the message list.

## 6. How to verify

```bash
cd "$(git rev-parse --show-toplevel)"
uv run pytest tests/test_context_prefix_stability.py tests/test_context_compression.py
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 3 --agents 2 --seed 0 --max-steps 2 \
  --prune-policy prefix_stable --coordinator-port 7080 --agent-base-port 7091 \
  --log-dir <absolute-dir>
# then: metadata.json → "prune_policy": "prefix_stable"
```

## 7. Change rule

Any new code path that deletes, rewrites, or left-shifts an existing message
must either (a) be unreachable under `prune_policy="prefix_stable"`, or (b) be
redesigned append-only / tail-safe, with this contract updated in the same
change. Reviewers should check every `messages` mutation site against R1–R5.

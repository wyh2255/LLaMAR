# Campaign record — sar-smoke (reef × sar_orch evolution, smoke phase)

A4 card (`t_8b66bb3a`). Frozen once at campaign start; every gate step of this
campaign must run the same code baseline.

## Frozen invariants

| Item | Value | Frozen at |
|---|---|---|
| `LLAMAR_REF` | `b243b2f0f563c14fded6cf4b01b5b2a99d236262` (main HEAD) | 2026-09-17 17:54 CST |
| Descriptor env literal | `reef_sar_adapter/descriptor.yaml` line 36 | verified at start (R1 F1 fix present) |
| Service | reef serve, `127.0.0.1:8900`, stack `stack.yaml` | A3, running since 17:34 |
| Scenario | `sar-smoke` (`loaded=true`) | A3 |
| Seed release | `941a5cb729cb99acfbedd700f57318f4e0470bbb` (current=true, pending=false) | A3 |
| Gate binding | `https://cf.api.fan` + `deepseek-flash` + main repo `.env` key | A3/R1 corrected |
| Gate suite | scene3 × agents{2,4} × seed{0,10} × max_steps=35, repeats=1, `min_win_margin=0` | serve.yaml |
| Episode workers | 2 (`execution.evolution.workers`) | serve.yaml |
| State dir | `/home/wyh/.cache/reef-sar-smoke/reef-service/` | stack.yaml |

## Pre-flight checks

- 2026-09-17 17:56 — 1-token ping through reef's own `ModelBinding.chat` at the
  frozen binding: **OK** (POST https://cf.api.fan/v1/chat/completions
  model=deepseek-flash; key sha256[:12]=`0980fb1a8368`; reply `''`).
  Command: `./reef-service.sh ping`.
- 2026-09-17 17:54 — `descriptor.yaml` env carries the frozen `LLAMAR_REF`
  literal (R1 must-read item 1) — present, equals `git rev-parse HEAD` of the
  main repo.
- 2026-09-17 ~18:00 — operator note: the main repo `.env` switched to the
  opencode-zen gateway (new key), but **this campaign's gate binding does not
  follow** (frozen invariant). The running service still holds the old key. So:
  **no service restart** (a restart would export the new key and 401 every
  episode); if the service dies, `kanban_block(needs_input)` instead.
  Verified live instead of via the launcher's ping: the service processes
  (pids 2052317/2052573) carry `REEF_SAR_UPSTREAM_API_KEY` with fingerprint
  `0980fb1a8368` — identical to the 17:56 value — and a 1-token call through
  `cf.api.fan` + `deepseek-flash` with that live key returns 200
  (script `/tmp/reef_a4_verify_live_key.py`). Note the launcher's
  `reef-service.sh ping` now reads the *new* `.env` and would false-401; the
  live-key check is the authoritative pre-train probe.

## Step log

| Step | Instruction source | Proposed mutation | Admission | Gate result (wins/losses/ties) | Verdict | Release |
|---|---|---|---|---|---|---|
| 1 | placeholder (card fallback; orchestrator comment absent by 18:23) | — | — | not run | **skipped: "no proposal"** | current head unchanged |
| 2 | same (re-inject probe, 18:33) | — | — | not run | **skipped: "no proposal"** | current head unchanged |
| 3 | same (post-restart, 18:41) | `worker-system` (skill) update, "Before Every Action" clause | admitted | 0/0/**4 ties** — all 8 episodes failed at launch (`reef-sar` not on the service PATH) | no change (ties) | current head unchanged |
| 4 | same (post env fix, 18:50) | `worker-system` (skill) update, "Before Every Action" clause | admitted | 1/**3**/0 — infra-tainted (defect 3 on pair 1, defect 4 on pairs 2/4) | **reject**: "candidate did not exceed its 3 losses with 1 wins" | no release; head `941a5cb7` unchanged |

### Step 1/2 root cause: reasoning burn, empty content (2026-09-17)

Both steps reached the proposer and the model answered in ~19.5 s with an
**empty content string**, so `parse_mutations` had nothing to parse and the step
settled `skipped: "no proposal"` (no gate episodes, no release).

Evidence chain:

1. Step record `work/step-records/sar-smoke/1/proposer.json`: `"reply": ""`,
   `"seconds": 19.573`, params `{"max_tokens": 4096, "timeout_s": 120.0}`.
2. Raw probe of the frozen binding (`cf.api.fan` + `deepseek-flash`): the
   response carries `message.reasoning_content` and `finish_reason: "length"`
   with `completion_tokens_details.reasoning_tokens == max_tokens` — i.e. the
   whole completion budget went to reasoning, `message.content` stayed empty.
   `ModelBinding.chat` returns `content` (an empty string, no exception).
3. **Replay of the exact recorded prompt** (`/tmp/reef_a4_replay_prompt.py`):
   - A recorded params (4096, no field): `finish=length`, content 0 chars,
     reasoning 4096 → reproduces the skip.
   - B 8192 + `reasoning_effort: "none"`: `finish=stop`, 570 chars, **parses**
     into one mutation (`worker-system`/`skill`).
   - C 8192 plain: reasoning eats all 8192 → empty again (so raising the cap
     alone does not fix it; the reasoning field is the fix).

### Fix applied (uncommitted, adapter package)

`reef_sar_adapter/method.py`:

- `MODEL_MAX_TOKENS` 4096 → **8192** (the largest entry is ~17 KB of text; the
  old cap could not carry a full `system.semantic` rewrite even without
  reasoning).
- new `NO_REASONING_PARAMS = {"reasoning_effort": "none"}` — the proposer asks
  the endpoint not to reason; a reasoning burn is indistinguishable from a
  silent model to the parser.
- `_ask` now tries the no-reasoning request first and falls back to the plain
  request when the endpoint refuses the field (400-class), so a model with no
  reasoning to disable keeps working unchanged.
- `tests/test_method.py`: the endpoint-failure test pins both attempts; two new
  tests cover the refuse-field fallback and the budget floor. **145 passed**
  (`/home/wyh/daily_work/reef/.venv/bin/python -m pytest`).

### Blocker: the running service cannot pick the fix up (needs a restart)

`resolve_proposer` resolves `reef_sar_adapter.method:propose` via
`importlib.import_module` **at recipe load (service boot, 17:34)**; the running
process therefore holds the pre-fix function object. Verified empirically: the
step-2 re-inject (18:33, after the edit) still recorded
`{"max_tokens": 4096}` and another empty reply — the module was not re-read.
Python's `sys.modules` cache means no config/scenario route can reload it.

A restart is required, and the operator note (17:59) forbids stop/restart
because `reef-service.sh start` re-reads the main repo `.env` (now the
opencode-zen key → 401 against the frozen `cf.api.fan` binding). Prepared and
ready to run on authorization: `/tmp/reef_a4_restart_preserving_key.sh` — it
captures the key the service already holds (from `/proc` environ, fingerprint
checked against `0980fb1a8368`), stops the stack, starts it directly with that
exact key exported (bypassing the launcher's `.env` read), then verifies
healthz + live-key fingerprint + a 1-token call at the frozen binding.

### Restart executed (authorized) and step 3 (2026-09-17 18:40)

The operator approved option 1 (18:40); `/tmp/reef_a4_restart_preserving_key.sh`
ran at 18:40:03: key fingerprint unchanged (`0980fb1a8368`), healthz in 3 s,
new pids 2417837/2417867, 1-token call at the frozen binding 200. Scenario and
release state survived (`loaded` flips false in the in-memory table until the
next request resolves it, which the API does lazily — `registry.list()` marks
durable registrations as `loaded: false`).

Step 3 (18:41) reached the fixed proposer: `proposer.json` now records
`{"max_tokens": 8192, "reasoning_effort": "none"}`, reply non-empty, 1.5 s, one
mutation parsed (`worker-system` update) — the reasoning fix works. **All 8
episodes then failed at the launch stage**: `harness binary 'reef-sar' not
found or not executable` (`harness/episodes/executor.py:99-104`), every episode
scored 0.0, the step recorded 4 ties / 8 episode failures and published no
change.

### Environment defect 1: `reef-sar` not on the service PATH (fixed)

`LocalExecutor` inherits only `PATH` (plus SYSTEMROOT/TMPDIR/CUDA_VISIBLE_DEVICES)
from the service process (`executor.py:150-156`), and the service itself was
started by running the venv python directly — which does not put the venv's
`bin/` on `PATH` (the launcher only adds `$HOME/.local/bin`, and `uv run` is not
in the picture). `reef-sar` was installed in
`/home/wyh/daily_work/reef/.venv/bin/reef-sar` but unresolvable.

Fix, no restart required: a symlink `~/.local/bin/reef-sar →
/home/wyh/daily_work/reef/.venv/bin/reef-sar` (that directory *is* on the
service's PATH). Proof: `shutil.which("reef-sar", path=<service PATH from
/proc>)` resolves
(`/tmp/reef_a4_shim.py`). The descriptor's `install` section is deliberately
absent, so reef's own wrapper route (`GET /reef/harness/install?adapter=sar` →
"adapter 'sar' declares no install section") does not apply.

### Environment defect 2: the binding base URL lacks `/v1` (fixed in the runner)

A pre-flight episode (3-step real run, `/tmp/reef_a4_preflight.py`) proved the
chain end-to-end before the gate spent 8 full episodes — and caught the second
defect: the experiment's LLM call failed 4 retries with `not found`.

Root cause: reef's binding contract is "`base_url` carries no `/v1` suffix; the
request paths add it" (`harness/episodes/model_binding.py:61`) — reef appends
`/v1/chat/completions` itself. The SAR experiment hands `--api-base` straight to
`AsyncOpenAI(base_url=...)` (`src/Agent/worker_agent/llm/openai_client.py:71`),
and the SDK appends only `/chat/completions`; a gateway that serves the OpenAI
paths under `/v1` (cf.api.fan, a vLLM) answers `404 not found`. Probe: POST
`https://cf.api.fan/chat/completions` → 404; `/v1/chat/completions` → 200.
reef's own bundled adapters whose harness is SDK-style render `{base_url}/v1`
in their descriptors (codex, pi, opencode, terminus, hermes, dsh); `native`
uses the bare value because its client appends `/v1` itself.

Fix in `reef_sar_adapter/runner.py`: `harness_base_url()` restores the prefix
exactly once (no-op when the base already ends in `/v1`, `""` stays `""`), and
`experiment_command()` uses it for `--api-base`; `plan()` reports the translated
value under `config.binding.experiment_api_base`. Runner-side (not descriptor
side) deliberately: the runner process is fresh per episode, so the fix needs no
service restart, and the runner is already the reef-dialect → experiment-CLI
translation layer. `tests/test_runner.py` pins the translation (6 new tests;
**151 passed**).

Pre-flight re-run after the fix: `--api-base https://cf.api.fan/v1`,
`end_reason: max_steps_reached`, 3 steps, exit 0, 33 835.8 billed tokens;
`run_meta.json` `llamar_ref = b243b2f0…` with `llamar_ref_source: LLAMAR_REF`;
config snapshot carries `"api_key": "<redacted>"`; truth only under
`workspace/truth/` and never in `sar/out/`.

### Environment defect 3: the port probe reserves nothing (fixed in the runner)

Step 4's first pair launched together and both probed the same block:
`allocate_ports` scanned from `PORT_SCAN_START = 50000` for the first free
block, and the probe releases the port the instant it returns — but the launch
after it (`uv run` starting the experiment's venv) takes seconds, not
milliseconds. Both episodes recorded `--coordinator-port 50000`; the pair's
`current-0` bound it and ran 157 s, while `candidate-0`'s experiment died on the
bind after **2.0 s with 0 steps and 0 tokens** (`framework_error`;
`memory_terminal.materialized: false`; `episode.json` exit 0, everything
collected) — an infra loss, not a mutation effect. Signature worth remembering:
a 2 s / 0-token framework error right after a sibling started means the port
block, not the model.

Fix: `_scan_slots(spread_seed)` rotates the scan start by
`sha256(seed)[:8] % slots`; both call sites pass the episode root
(`REEF_SAR_TREE`, a `mkdtemp` unique per episode) as the seed, so two
concurrent episodes land in different corners of 50000–59999. An empty seed
keeps the plain scan (manual runs, tests). Two more runner changes ride along:
`plan()` reports no new fields beyond the earlier `experiment_api_base`, and
`run_episode` now echoes the experiment's log tails (12 lines, key redacted —
the experiment exits 0 on a framework error, so `run_logged`'s failure echo
never fired and the step record held no hint of the cause).

Tests: `tests/test_runner.py` pins the rotation (different seeds → different
first slot, same slot multiset; seeded allocation still returns a live block)
and the redacted diagnostics. **153 passed.** The remaining step-4 episodes pick
the fix up per episode (the runner is a fresh process per episode, no restart).

## Step 4 result (verdict `reject`, head unchanged)

All 8 episodes produced records (`proposer.json` 1 call / 1.463 s;
`mutations.json` admitted an `update` of `worker-system`, kind `skill`). Audit
(`/tmp/reef_a4_card_audit.py 4`): every episode's `run_meta.json` carries
`llamar_ref = b243b2f0…` with `llamar_ref_source: LLAMAR_REF`; `files.collected`
holds only `run_metrics.json` / `eval_metrics.json` / `summary.csv`; truth lives
in `workspace/truth/` (never in the collected set). PASS.

| pair (agents/seed) | candidate | current | note |
|---|---|---|---|
| 2 / 0 | **0.0** | 0.6796 | candidate died on the port race (defect 3, 2 s / 0 tokens); current died at step 21 (`framework_error`) |
| 2 / 10 | 0.6188 | **-0.0561** | current's workers never came online (defect 4) — infra win |
| 4 / 0 | 0.8389 | **1.0368** | clean pair, current finished the mission (`success`, 29 steps) |
| 4 / 10 | **-0.0575** | 0.8139 | candidate's workers never came online (defect 4) — infra loss |

Reef's own record (`/reef/status` → `last_committed_step`): `wins: 1,
losses: 3, ties: 0, selected: false, published: false`, reason *"candidate did
not exceed its 3 losses with 1 wins"*; `artifact_head_sync` stays
`synchronized` on the seed release `941a5cb7`. No release, nothing to promote.

Billed tokens (sum of `run_metrics.tokens`): 6 107 148 total — episodes
0 / 977 579 / 1 616 556 / 287 614 (candidate) and 379 870 / 280 461 / 1 331 765
/ 1 233 303 (current); the two `0`-step failures burned ~568 k of that with no
environment step. Wall clock 18:50 → 19:16 for the gate.

Verdict caveat: 3 of the 4 pairs are infra-tainted (defect 3 on pair 1, defect 4
on pairs 2 and 4), so the score comparison is *not* usable as a promotion
decision. The loop itself (instruction → propose → admission → 8 scored
episodes → verdict → no change) is proven.

## Environment defect 4: the worker start races the coordinator's startup (open, LLaMAR side)

**Reproduced deliberately** (`/tmp/reef_a4_race_repro.py`, two runner processes
launched in the same instant, `max_steps=2`). Episode *a* reproduced the failure
exactly (0 steps, `framework_error`, 387 s); episode *b* ran clean (2 steps,
16 s) — and the port fix held: *a* took block 59440, *b* took 58960.

Timeline from *a*'s logs (`workspace/logs/experiment.err.log`), all within
19:17:39–19:17:44:

1. `SARCoordinator starting on port 59440` (19:17:39), `Map Agent MCP server
   mounted` (19:17:42), then `Worker Alice started on port 59442` /
   `Worker Bob started on port 59443` (19:17:42).
2. Both workers' MCP clients (`StreamableHTTP session manager` →
   `streamable_http_client`) hit `ConnectError: All connection attempts failed`
   in that same second — *before* the coordinator's ASGI startup completed
   (`Application startup complete.` / `Uvicorn running on
   http://0.0.0.0:59440` land just after, 19:17:42).
3. The worker's lifespan raises (`BaseExceptionGroup`), the worker never
   registers → the coordinator polls `query_workers` → `无可用 Agent` for 200
   agent steps ≈ 370 s → `A2A orchestration failed` → `framework_error`,
   ~280 k tokens burned, 0 environment steps.

Mechanism: `sar_orch/experiment.py` starts the coordinator as a background task
(`882`) and the workers immediately after (`955-980`), then sleeps 5 s for
registration (`985`); a worker whose MCP connect lands in that window before the
coordinator's listener is up dies without a retry (`worker.py` lifecycle). Under
one episode at a time the window is usually won; with two episodes plus the
machine's swap pressure it is lost often enough to matter (3 of 8 gate episodes
across the two failure classes, plus a failure in the repro).

Not fixed here: this is main-repo LLaMAR code, and the campaign's episodes
materialize `LLAMAR_REF` with `git archive` — a fix only exists after a commit,
which would move the campaign's frozen ref (spec §9-8) and make step 4 vs later
steps non-comparable. Operator decision requested (see the kanban block).

**The fix already exists, uncommitted**: the side card `t_7c303cb1` (workflow §C
编外卡) holds exactly this fix in `.worktrees/t-7c303cb1` (branch
`wt/t_7c303cb1`, HEAD still `b243b2f` — work is in the working tree):
`src/Agent/worker_agent/tools/mcp_loader.py` bounded handshake retry
(`connect_attempts=3`, `retry_backoff=1.0` doubling; its own comment names the
race: *"a single refused attempt used to abort the worker for the whole run"*),
`sar_orch/worker.py` liveness, `sar_orch/experiment.py` workers-dead fail-fast,
plus new tests (`test_mcp_connect_resilience.py`,
`test_worker_mcp_startup_failfast.py`, `test_experiment_worker_liveness.py`).
Merging it (a new frozen ref) is the precondition for a clean re-gate.

Smoke-level mitigations that are in scope and in place: the port rotation keeps
concurrent episodes off each other's blocks, and `run_episode` now echoes the
experiment's failing log tails into the episode record so the next occurrence is
diagnosable from the step record alone.

## Retry-run verification (2026-09-17 19:28–19:40, run 952)

The first run of this card hit its iteration budget seconds after posting the
report (19:27); the retry re-ran every claim above against the live service and
the step records. All reproduced:

- `/reef/status` + `/reef/scenarios/sar-smoke/releases`: step 4 committed,
  `wins:1 losses:3 ties:0 selected:false published:false`, scheme reason
  "candidate did not exceed its 3 losses with 1 wins"; head release
  `941a5cb7` `current:true`, no pending release.
- Card audit re-run (`/tmp/reef_a4_card_audit.py 4`): 8/8
  `run_meta.llamar_ref == b243b2f0…` with `llamar_ref_source: LLAMAR_REF`;
  collected set per episode is exactly `run_metrics.json` / `eval_metrics.json`
  / `summary.csv` — no truth entry, `truth_dir` under `workspace/truth`. PASS.
- Token tally recomputed from the step-4 records: 6 107 148 billed total
  (0 / 977 579 / 1 616 556 / 287 614 candidate, 379 870 / 280 461 / 1 331 765 /
  1 233 303 current) — matches the report.
- Adapter suite re-run: **153 passed** (17.4 s).
- Running service untouched all through (pids 2417837/2417867); live
  `REEF_SAR_UPSTREAM_API_KEY` fingerprint `0980fb1a8368` (unchanged) and a
  1-token call at the frozen binding returns 200.
- `pull_release` dry run re-verified: head release `941a5cb7`, both prompt
  entries SAME, rules SKIP, config REPORT, **0 files to write**.

## Open decision (recorded 19:40; card closed as smoke closure)

This campaign recorded **option A**: the smoke's contract (spec §9-2 — the
smoke proves the loop, it does not prove uplift) is satisfied; the verdict is
`reject` with no release, so there is nothing to promote and the promote/pull
legs stay unexercised by design. Option B — a clean, promotion-grade verdict —
requires merging the defect-4 fix (it exists uncommitted in the side card
`t_7c303cb1`), which moves `LLAMAR_REF` and therefore is **a new campaign**
(new frozen ref, budget approval ~6M tokens/step), not a re-run of this one.

# reef_sar_adapter

The [reef](https://github.com/Human-Agent-Society/reef) harness adapter for
LLaMAR's `sar_orch` search-and-rescue simulator: one SAR simulation is one reef
gate episode. The adapter manages the **text layer** of the harness (coordinator
and worker prompts, rules text, harness config); it never mutates code.

Design and decisions: `.hermes/spec/20260917-reef-sar-evolution.md` (spec) and
`.hermes/spec/20260917-reef-sar-implementation-workflow.md` (card-level, whose
§A.2 correction list is the hard contract for this package).

## What is what

| File | Role |
|---|---|
| `descriptor.yaml` | Declares the adapter to reef's render and episode engines: binary/argv, the tree layout under `overlay/`, env relocation, trajectory reader, model binding. |
| `trajectory.py` | `read_sar_run(path)`: one SAR run directory → the `metrics` + `run_meta` events the gate grades. |
| `method.py` | `evaluate(task, result)` (never raises, always a finite float) and `propose(nodes, samples, models, *, requests, rejected)` (evidence package → anchored tree mutations). |
| `runner.py` | The `reef-sar` console script: the full episode chain — materialize the frozen ref, apply the overlay, translate the harness config, pick free ports, run `sar_orch/experiment.py`, collect the metrics, print one metrics line. `--dry-run` rehearses materialize + render and prints the plan. |
| `serve.yaml` | The recipe preset: adapter wiring, the smoke task suite, and the seeded tree (4 entries, §B7-1). |
| `stack.yaml` | The `reef serve -c` deployment stack: port, token, the frozen upstream binding, and the state directory (A3). |
| `reef-service.sh` | `start` / `stop` / `status` / `ping` for the local service; reads the upstream key out of the main repo `.env`, never echoing it. |
| `ping_upstream.py` | One 1-token call through reef's own `ModelBinding.chat` at the frozen binding - the freeze check before a campaign's first step. |
| `pull_release.py` | `GET /reef/harness` (head, or `--release-id`) → the mapped checkout files; reports by default, writes with `--apply`. |
| `gen_serve_yaml.py` | Regenerates `serve.yaml`, embedding the current `sar_orch/prompts/...` bodies as the seed. |

## The episode chain, and what it keeps

`reef-sar <task JSON>` runs one gate episode. The contracts its docstring owns:

- **The task JSON is authoritative**: `scene`/`agents`/`seed` come from argv, and so
  does `max_steps` when the task carries one (every task in `serve.yaml` does).
  `sar_config.json` contributes only the behavior keys the runner whitelists and
  translates (`runner.CONFIG_RUN_KEYS`); anything else is warned about on stderr
  and ignored. A config mutation can never change which task a candidate is graded
  on, nor buy it more steps than its paired baseline episode gets.
- **Truth stays in the episode root**: `--truth-output-dir` is
  `{tree}/workspace/truth`, and `sar/out` (the trajectory directory reef copies
  into the step record) never holds a truth file — the collect step refuses to
  finish if one appears.
- **Everything lands under `workspace/` or the whitelisted `sar/`**, HOME-scoped
  caches included, so the residue audit stays clean.
- **The binding's `api_key` reaches the run through its environment only**: the
  config snapshot kept as an artifact carries it redacted.

The experiment runs as `uv run --extra sar ...` from the materialized checkout: the
repo's base dependency set does not carry matplotlib, which `SAR/utils.py` imports,
so a bare `uv run` cannot start it. The per-episode venv is built from the
descriptor-pinned `UV_CACHE_DIR` (a warm cache costs seconds; a cold one would
re-download every dependency 8-40 times per gate step).

## Install (reef service environment)

```bash
cd /home/wyh/daily_work/reef          # the reef checkout
uv venv                               # once; .venv is gitignored there
uv pip install -e .
uv pip install -e /home/wyh/daily_work/LLaMAR/.worktrees/reef-a/reef_sar_adapter
```

`reef-infra` is deliberately not a dependency of this package: it installs
*into* the reef environment and uses the checkout the deployment runs.

## Verify

```bash
cd /home/wyh/daily_work/reef
.venv/bin/python -c "from reef.harness.adapters.descriptor import external_descriptors; print(external_descriptors())"   # must contain 'sar'

cd /home/wyh/daily_work/LLaMAR/.worktrees/reef-a/reef_sar_adapter
/home/wyh/daily_work/reef/.venv/bin/python -m pytest
/home/wyh/daily_work/reef/.venv/bin/python -m reef_sar_adapter.gen_serve_yaml --check
```

## Re-seeding after a prompt change

`serve.yaml`'s seed text must be the prompt bodies the harness loads today:

```bash
python -m reef_sar_adapter.gen_serve_yaml           # rewrite serve.yaml
python -m reef_sar_adapter.gen_serve_yaml --check   # exit 1 when stale (the tests run this)
```

The generator parses its own output back and refuses to write a file that does
not reproduce the config it was built from.

The `base-rules` seed is the one exception to "seed text = repo file": reef's
tree refuses a rules node with an empty `text` (`harness/tree/nodes.py`
`rules_node` → `_require_text`), so a seed with `text: ""` cannot even be
turned into a scenario. The seed carries `runner.RULES_PLACEHOLDER` instead,
and the overlay treats that one body as "no rules": the baseline stays
byte-identical to the checkout, and the first proposal that writes real rules
replaces it.

## Run the loop locally (A3 hand book)

One reef process, state under `/home/wyh/.cache/reef-sar-smoke/reef-service`
(releases, step records, logs — restarting the service keeps them):

```bash
cd "$(git rev-parse --show-toplevel)/reef_sar_adapter"
./reef-service.sh ping                     # 1-token freeze check of the binding (no key printed)
./reef-service.sh start                    # serve on 127.0.0.1:8900, waits for /healthz
./reef-service.sh status
```

The service API (token = `reef-sar-local`, locals only):

```bash
H='Authorization: Bearer reef-sar-local'
curl -sS -H "$H" http://127.0.0.1:8900/healthz
curl -sS -H "$H" http://127.0.0.1:8900/reef/harness/adapters            # 'sar' must be listed
curl -sS -X POST -H "$H" -H 'Content-Type: application/json' \
     -d '{"name":"sar-smoke"}' http://127.0.0.1:8900/reef/scenarios     # 201 + release_id
S='x-reef-scenario: sar-smoke'
curl -sS -H "$H" -H "$S" http://127.0.0.1:8900/reef/harness             # head tree files + gate
curl -sS -H "$H" -H "$S" http://127.0.0.1:8900/reef/harness/releases    # catalog with gate metrics
curl -sS -H "$H" -H "$S" http://127.0.0.1:8900/reef/scenarios/sar-smoke/releases
python -m reef_sar_adapter.pull_release                                  # dry run: mapping + diff
python -m reef_sar_adapter.pull_release --apply                          # write + show git diff
./reef-service.sh stop                     # SIGTERM, waits for the port to free
```

A step (an operator instruction, the gate, the release) is driven by
`POST /reef/train`; that is the A4 card's loop. `pull_release.py` re-reads
`stack.yaml` for the URL and token, and defaults to the `sar-smoke` scenario
and `/home/wyh/daily_work/LLaMAR` as the checkout to write into.

# Tool Gap Analysis — Coordinator ↔ Worker System Prompts & Tool Registration

## 1. What tools and capabilities exist?

### Worker tools (17 registered in `SAR_WORKER_TOOLS`)

| Tool (snake_case) | Parameters | Prompts Worker About It? |
|---|---|---|
| `navigate_to` | `target_id: string` | Yes — `navigate_to(target)` |
| `move` | `direction: enum(Up,Down,Left,Right,UpLeft,UpRight,DownLeft,DownRight,Center)` | No |
| `explore` | _(none)_ | No |
| `carry_person` | `person_id: string` | Yes |
| `drop_off_person` | `person_id, deposit_id` | Yes |
| `get_supply` | `source_id, supply_type: Water\|Sand` | Yes |
| `store_supply` | `deposit_id: string` | No |
| `use_supply` | `fire_id, supply_type: Water\|Sand` | Yes |
| `clear_inventory` | _(none)_ | No |
| `get_agent_state` | _(none)_ | No |
| `report_observation` | `object_type, name, position, attributes, confidence, note` | Yes |
| `query_shared_memory` | _(none)_ | No |
| `no_op` | _(none)_ | Yes |
| `finish_task` | `success, summary, task_description` | Yes |
| `ask_coordinator` | _(from a2a package)_ | No |
| `read_mailbox` | _(only if peer mail enabled)_ | No |
| `send_peer_mail` | _(only if peer mail enabled)_ | No |

### Coordinator tools (registered in `agent_executor.py`)

1. `send_message` — unified gateway: `assign_task`, `reply_to_help`, `cancel_task`
2. `query_task_events` — check dispatched task status
3. `update_plan` — declare/update DAG plan
4. `query_workers` — fetch online workers + capabilities (builtin)
5. `verify_result` — verify worker outputs
6. `query_task_results` — retrieve completed task results
7. `finish_task` — signal mission complete
8. `query_sar_state` (oracle mode only)
9. `query_semantic_map` — via Environment State (no tool registration needed)
10. `query_team_status` — via Environment State

### Worker AgentCard capabilities

Created in `worker/a2a_server.py` (line 65-76):
```python
capabilities = ["sar", "navigation", "rescue", "firefighting"]
skills = [
  AgentSkill(id="sar", name="sar", description="Capability: sar", tags=["sar"]),
  AgentSkill(id="navigation", name="navigation", ...),
  ...
]
```
These are **string tags only** — no tool names, no parameter schemas.

---

## 2. Tool Gaps — Prompt says vs Reality

### Gap A: Coordinator prompt references action conventions, not tool names

The coordinator prompt (`system.semantic.md`) shows examples using CamelCase:

```
"NavigateTo(Reservoir) → GetSupply(Reservoir) → NavigateTo(Fire_Region) → UseSupply(Fire_Region) → ..."
```

**Reality:** These are **not tool names in any registration** — they're narrative descriptions the coordinator writes into the `content` field of `send_message`. The worker LLM then interprets them and calls its own tools (`navigate_to`, `get_supply`, `use_supply`). The CamelCase names match nothing in the actual tool registry; they're a communication convention.

**Mismatch severity: LOW** — the convention is human-readable and workers have successfully interpreted it. But there's zero schema enforcement.

### Gap B: Worker prompt uses actual tool names correctly

Worker prompt (`worker/system.md`) correctly references:
- `navigate_to(target)` — matches actual `navigate_to(target_id)`
- `get_supply()` — matches actual `get_supply(source_id, supply_type)`
- `use_supply()` — matches actual `use_supply(fire_id, supply_type)`
- `no_op()` — matches actual `no_op()`
- `finish_task(...)` — matches actual `finish_task(success, summary, task_description)`
- `report_observation` — matches actual `report_observation(object_type, ...)`

**Severity: NONE** — worker prompt is factually correct.

### Gap C: Tools in worker prompt not fully covered

Worker prompt mentions only **7 of 17 tools**:
- Mentioned: `navigate_to`, `get_supply`, `use_supply`, `no_op`, `finish_task`, `report_observation`, `carry_person`, `drop_off_person`
- NOT mentioned: `move`, `explore` (mentioned in strategy but not as tool), `store_supply`, `clear_inventory`, `get_agent_state`, `query_shared_memory`, `ask_coordinator`

**Severity: LOW** — workers can still use unmentioned tools via their tool definitions, but the prompt doesn't guide when to use them.

### Gap D: Object ID mismatch

The coordinator prompt says `NavigateTo(Reservoir)` / `NavigateTo(Fire_Region)`. The actual environment object IDs are things like `Reservoir_0`, `GreatFire_Region_1`. The coordinator infers object names from the Environment State block, but the environment uses numbered suffixes (`Reservoir_0`, `Deposit_0`) while the prompt examples use generic names (`Reservoir`, `Deposit`). In the experiment, the coordinator invented `NavigateTo(ReservoirOxbow)` — an ID that doesn't exist.

**Severity: HIGH** — causes hard failures when workers try to navigate to nonexistent objects.

---

## 3. Is there a worker_agent_card?

**Yes, but it carries no tool information.**

The worker creates an `AgentCard` in `a2a/worker/a2a_server.py` (line 86-98):
- `name`: `"Mini-Agent Worker {worker_id}"`
- `description`: `"Mini-Agent worker node {worker_id}"`
- `capabilities`: `AgentCapabilities(streaming=True, push_notifications=True)` — A2A protocol level only
- `skills`: List of `AgentSkill` — just the tags `["sar", "navigation", "rescue", "firefighting", "backend", "model"]`

The coordinator fetches this card via `_fetch_agent_card()` in `server.py` (line 1073) during WebSocket registration. It parses skills into capabilities in `agent_registry.py` (line 108-153):
```python
for skill in skills:
    if skill_id not in ("backend", "model"):
        capabilities.append(skill_id)  # -> ["sar", "navigation", "rescue", "firefighting"]
```

**What's missing from the agent card:**
- ❌ No tool names
- ❌ No tool parameter schemas
- ❌ No structured description of what workers can do
- ❌ No object IDs the worker knows about

**What the coordinator sees:**
The `query_workers` tool returns:
```
- Alice: Mini-Agent worker alice (能力: sar, navigation, rescue, firefighting)
```
That's the **only** capability signal the coordinator receives — and it's purely advisory, not injected into system prompt or context memory.

---

## 4. How does the coordinator actually assign tasks?

The flow is:
1. Coordinator calls `send_message(message_type="assign_task", who="Alice", content="NavigateTo(Reservoir_0) → GetSupply(Reservoir_0, Water) → NavigateTo(GreatFire_Region_1) → UseSupply(GreatFire_Region_1, Water)", related_task_id="alice-task")`
2. `SendMessageTool.execute()` → `DispatchTaskTool.execute()` → `store._router.send_task_async(agent_id="Alice", prompt=content, ...)`
3. The prompt (the `content` string) is sent via A2A protocol to the worker's A2A endpoint
4. The worker's LLM receives this as a user message / task instruction
5. The worker interprets the natural language and calls its own tools

**The coordinator does NOT know tool signatures** — it describes the job in natural language. There's no structured task format, no typed parameters, no schema validation. The `content` field is an opaque string.

---

## 5. Does Environment State include worker capabilities?

**No.** The coordinator's Environment State block (rendered by `CoordinatorContextManager._render_environment_view()` + `_render_current_state()` + `_render_task_plan()`) contains:
- Environment: known fires, persons, reservoirs, deposits (counts only)
- Current State: step budget, mission finished flag, dispatched tasks, task status, recent changes, supervision alerts
- Task Plan & Progress: structured task views

**What's missing from Environment State:**
- ❌ Worker capabilities list
- ❌ Worker tool descriptions
- ❌ Worker inventory/position per agent (only in oracle mode via `global_snapshot`)
- ❌ Object IDs known to the environment

In `semantic` mode, the team_status_summary only has:
```python
{
  "workers": snap.get("agents", []),  # just agent names
  "recent_observations": [...],
  ...
}
```
The `agents` list from `SemanticMapStore` contains only `{"agent_id": name}` — no tool info.

---

## 6. Does `send_message` properly convey task structure?

**Partially — it's opaque text.**

The `send_message` tool's `content` parameter is just a string. There's no:
- ✅ Structured action sequence format
- ✅ Parameter type enforcement
- ❌ Validation that action names match known worker tools
- ❌ Validation that object IDs exist
- ❌ Feedback loop to correct malformed task descriptions

The coordinator prompt tries to enforce a convention (long action chains, CamelCase notation) but this is purely prompt-based, not system-enforced.

---

## 7. Does the coordinator prompt reference tool names that don't match reality?

The coordinator prompt uses these action patterns in examples:
- `NavigateTo(X)` — worker tool is `navigate_to(target_id)`, so `X` maps to `target_id`
- `GetSupply(X)` — worker tool is `get_supply(source_id, supply_type)`, so `X` is `source_id` but `supply_type` is implicit
- `UseSupply(X, Y)` — worker tool is `use_supply(fire_id, supply_type)`, so `X, Y` maps correctly
- `NoOp()` — matches exactly
- `DropOff(X, Y)` — worker tool is `drop_off_person(person_id, deposit_id)`, argument order reversed from example

**Argument order inconsistency in drop_off_person:**
- Coordinator prompt example: `DropOff(deposit_id, person_id)` 
- Worker tool signature: `drop_off_person(person_id, deposit_id)`
- Worker action builder: `DropOff({deposit_id}, {person_id})` — uses the prompt convention

This reversal is handled by the worker prompt's explicit instruction: "use `no_op()` while checking..." — the worker figures it out from context.

---

## Summary of Critical Gaps

| # | Gap | Severity | Impact |
|---|---|---|---|
| 1 | **No worker tool/capability injection in coordinator's Environment State** | HIGH | Coordinator plans tasks without knowing what workers can actually do |
| 2 | **Object IDs are stale or invented** | HIGH | Worker fails with "I don't see that object" |
| 3 | **send_message content is unstructured text** | MEDIUM | No validation, no schema, no error recovery on malformed task descriptions |
| 4 | **AgentCard carries only capability tags, no tool schemas** | MEDIUM | Coordinator can't discover tool signatures dynamically |
| 5 | **Coordinator prompt teaches action conventions, not tool APIs** | LOW | Convention works but is brittle — worker changes break silently |
| 6 | **query_task_events loop (from experiment)** | MEDIUM | Prompt encourages periodic querying but doesn't enforce efficient patterns |
| 7 | **Some worker tools undocumented in prompt** | LOW | Workers may not use `move`, `store_supply`, `clear_inventory`, `query_shared_memory`, `get_agent_state` optimally |

## Improvement Suggestions

1. **Inject worker tools into Environment State**: Include the worker's registered tool list (names + parameters) in the coordinator's Environment State block, derived from the AgentCard or tool registry.

2. **Structured task format**: Replace the free-text `content` with a structured JSON schema (e.g., `{"actions": [{"type": "navigate_to", "target_id": "..."}, ...]}`) with validation before dispatch.

3. **Object ID awareness**: Ensure the Environment State block includes the exact object IDs from the environment, not just counts. Add a known-object-IDs field.

4. **AgentCard enrichment**: Include tool names and parameter schemas in the worker's AgentCard skills, so the coordinator can discover what workers can do.

5. **Worker capability block in system prompt**: Auto-inject a dynamically generated "Worker Capabilities" section into the coordinator system prompt that lists what each worker can do, derived from AgentCard + tool registry.

6. **Task validation callback**: Before dispatching, validate that referenced object IDs exist in the known environment state, and warn if an action name doesn't match any known pattern.

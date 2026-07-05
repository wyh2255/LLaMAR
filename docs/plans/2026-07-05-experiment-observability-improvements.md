---
日期: 2026-07-05
文档类型: 修改计划
文档概述: 针对 LLaMAR 实验评测方案提出的项目改进实施计划，覆盖实验元数据、轨迹增强、错误类型、延迟追踪、关联 ID、subtask 时间线、聚合诊断和文档同步。
---

# Experiment Observability Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the SAR experiment pipeline so experiment outputs can support reproducibility, simulator replacement, and failure attribution across framework, Prompt, model, environment, and budget issues.

**Architecture:** Keep the existing SAR orchestration flow intact and add observability at the lowest shared logging boundary first. `ExperimentLogger` becomes the stable writer for metadata, enriched CSV rows, unified events, and subtask timelines; `experiment.py`, `barrier.py`, `worker.py`, and `coordinator.py` pass richer context into it without changing task semantics.

**Tech Stack:** Python 3, pytest, CSV/JSON/NDJSON files, existing `sar_orch` modules, existing `SAR` simulator, existing A2A Coordinator/Worker stack.

## Global Constraints

- Do not change SAR task semantics, action names, checker rules, or A2A protocol behavior while adding observability.
- Preserve backward compatibility for existing CSV readers by keeping current columns and appending new columns rather than renaming or removing columns.
- Do not require network LLM calls in unit tests; test logger, metadata, and aggregation behavior with deterministic fakes or direct method calls.
- Keep SAR-specific mappings isolated so later simulator environments can reuse the logging and aggregation pattern.
- Default run command must continue to work: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42`.
- Lint command remains: `uv run --with ruff ruff check src/ sar_orch/`.
- Reference design document: `docs/system_docs/experiment_design.md`.

---

## File Structure

Create or modify these files only unless a task explicitly expands scope after review.

- Create `tests/test_experiment_logger_observability.py`: unit tests for metadata, enriched CSV fields, event logging, subtask logging, and summary fields.
- Create `tests/test_sar_barrier_observability.py`: deterministic tests for `SARBarrier.get_last_step_log()` error type and duration fields using monkeypatched environment behavior.
- Create `tests/test_aggregate_observability.py`: unit tests for enhanced aggregation with new metadata and end reason fields.
- Modify `sar_orch/logger.py`: add metadata writer, event writer, subtask writer, extended CSV headers, optional fields, and summary extensions.
- Modify `sar_orch/experiment.py`: generate `run_id`, collect run metadata, compute `end_reason`, pass max step and timing fields into `log_step()`, and write final metadata/result fields.
- Modify `sar_orch/barrier.py`: capture per-step duration, timeout agents, and best-effort `error_type_by_agent` from SAR environment state without changing action execution.
- Modify `sar_orch/worker.py`: add lightweight correlation IDs and latency timing around LLM/tool callback logging when callback data supports it.
- Modify `sar_orch/coordinator.py`: log coordinator task events with correlation fields and richer router interaction fields.
- Modify `sar_orch/aggregate.py`: include optional observability fields in aggregated output while tolerating old result directories.
- Modify `docs/system_docs/logging_map.md`: update the logging map after implementation.
- Modify `docs/system_docs/experiment_design.md`: add a short implementation status note after the code changes land.

## Task 1: Logger Metadata And Extended Schemas

**Files:**
- Create: `tests/test_experiment_logger_observability.py`
- Modify: `sar_orch/logger.py`

**Interfaces:**
- Consumes: Existing `ExperimentLogger(experiment_name: str = "sar_experiment", log_dir: str | None = None)`.
- Produces: `ExperimentLogger.write_metadata(metadata: dict[str, Any]) -> None`.
- Produces: `ExperimentLogger.log_event(event_type: str, payload: dict[str, Any] | None = None, **fields: Any) -> None`.
- Produces: `ExperimentLogger.log_subtask(subtask_id: str, status: str, **fields: Any) -> None`.
- Produces: Extended optional parameters on `log_step()`, `log_agent_interaction()`, `log_router_interaction()`, and `log_token_usage()`.

- [ ] **Step 1: Write metadata and schema tests**

Add this test file:

```python
from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.logger import ExperimentLogger


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_logger_writes_metadata_and_extended_step_fields(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.write_metadata(
        {
            "run_id": "run-1",
            "env_name": "SAR",
            "scenario_id": "scene_1",
            "seed": 42,
            "agent_count": 2,
            "model": "fake-model",
            "prompt_version": "baseline",
        }
    )
    logger.log_step(
        step_num=1,
        actions=["NoOp()", "NoOp()"],
        successes=[True, True],
        observations=["obs-a", "obs-b"],
        coverage=0.25,
        transport_rate=0.5,
        finished=False,
        timeout_agents=[],
        run_id="run-1",
        max_steps=10,
        remaining_steps=9,
        wall_time_since_start=1.5,
        step_duration_ms=25.0,
        error_types=["", ""],
        completed_subtasks_delta=["NavigateTo(ReservoirUtah)"],
        end_reason="",
    )
    logger.close()

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == "run-1"
    assert metadata["scenario_id"] == "scene_1"

    rows = read_csv(tmp_path / "trajectory.csv")
    assert rows[0]["RunID"] == "run-1"
    assert rows[0]["MaxSteps"] == "10"
    assert rows[0]["RemainingSteps"] == "9"
    assert rows[0]["StepDurationMs"] == "25.0"
    assert "NavigateTo(ReservoirUtah)" in rows[0]["CompletedSubtasksDelta"]


def test_logger_writes_events_and_subtasks(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_event(
        "dispatch",
        run_id="run-1",
        step=2,
        agent="Coordinator",
        correlation_id="corr-1",
        payload={"worker": "Alice"},
    )
    logger.log_subtask(
        subtask_id="dispatch-1",
        status="assigned",
        run_id="run-1",
        step=2,
        assigned_to="Alice",
        subtask="Explore the west side",
    )
    logger.close()

    event_line = (tmp_path / "events.ndjson").read_text(encoding="utf-8").strip()
    event = json.loads(event_line)
    assert event["event_type"] == "dispatch"
    assert event["correlation_id"] == "corr-1"
    assert event["payload"] == {"worker": "Alice"}

    rows = read_csv(tmp_path / "subtasks.csv")
    assert rows[0]["SubtaskID"] == "dispatch-1"
    assert rows[0]["Status"] == "assigned"
    assert rows[0]["AssignedTo"] == "Alice"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
```

Expected: failures because `write_metadata()`, `log_event()`, `log_subtask()`, and extended `log_step()` parameters do not exist yet.

- [ ] **Step 3: Extend `ExperimentLogger` minimally**

In `sar_orch/logger.py`, add `json` and `time` imports, add `events` and `subtasks` to lazy file handling, and extend current methods with optional fields. The implementation shape should be:

```python
import json
import time


def write_metadata(self, metadata: dict[str, Any]) -> None:
    path = self._log_dir / "metadata.json"
    with self._lock:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2, sort_keys=True)


def log_event(
    self,
    event_type: str,
    payload: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    row = {
        "timestamp": time.time(),
        "event_type": event_type,
        **fields,
        "payload": payload or {},
    }
    with self._lock:
        path = self._log_dir / "events.ndjson"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def log_subtask(self, subtask_id: str, status: str, **fields: Any) -> None:
    with self._lock:
        self._ensure_file("subtasks")
        row = {
            "RunID": fields.get("run_id", ""),
            "Step": fields.get("step", ""),
            "SubtaskID": subtask_id,
            "Status": status,
            "AssignedTo": fields.get("assigned_to", ""),
            "Subtask": fields.get("subtask", ""),
            "CreatedAt": fields.get("created_at", ""),
            "UpdatedAt": fields.get("updated_at", time.time()),
            "FailureClass": fields.get("failure_class", ""),
            "Details": fields.get("details", ""),
        }
        self._writers["subtasks"].writerow(row)
        self._files["subtasks"].flush()
```

Extend `headers_map` by appending new fields, not replacing old fields:

```python
"trajectory": [
    "Step", "Actions", "Successes", "Observations", "Coverage",
    "TransportRate", "Finished", "TimeoutAgents", "RunID", "MaxSteps",
    "RemainingSteps", "WallTimeSinceStart", "StepDurationMs",
    "ErrorTypes", "CompletedSubtasksDelta", "EndReason",
]
```

Use `extrasaction="ignore"` only if a writer can receive legacy rows with fewer fields. Do not remove existing columns.

- [ ] **Step 4: Run logger tests and verify they pass**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
```

Expected: all tests in this file pass.

- [ ] **Step 5: Run lint for changed module**

Run:

```bash
uv run --with ruff ruff check sar_orch/logger.py tests/test_experiment_logger_observability.py
```

Expected: no ruff errors.

## Task 2: Run Metadata And End Reason In Experiment Runner

**Files:**
- Modify: `sar_orch/experiment.py`
- Modify: `tests/test_experiment_logger_observability.py`

**Interfaces:**
- Consumes: `ExperimentLogger.write_metadata()` and extended `log_step()` from Task 1.
- Produces: `build_run_metadata(...) -> dict[str, Any]` helper in `sar_orch/experiment.py`.
- Produces: `classify_end_reason(...) -> str` helper in `sar_orch/experiment.py`.

- [ ] **Step 1: Add unit tests for metadata helper and end reason helper**

Append to `tests/test_experiment_logger_observability.py`:

```python
from sar_orch.experiment import build_run_metadata, classify_end_reason


def test_build_run_metadata_contains_reproducibility_fields():
    metadata = build_run_metadata(
        run_id="run-1",
        scene=1,
        num_agents=2,
        seed=42,
        model="fake-model",
        provider="openai",
        api_base="https://example.invalid",
        max_steps=120,
        wall_clock_limit=600.0,
        sandbox_profile="workspace",
        coordinator_prompts="/repo/sar_orch/prompts/coordinator",
        worker_prompts="/repo/sar_orch/prompts/worker",
    )
    assert metadata["run_id"] == "run-1"
    assert metadata["env_name"] == "SAR"
    assert metadata["scenario_id"] == "scene_1"
    assert metadata["agent_count"] == 2
    assert metadata["success_criteria"] == "SAR checker subtasks complete"


def test_classify_end_reason_orders_specific_causes():
    assert classify_end_reason(
        finished=True,
        steps=5,
        max_steps=10,
        elapsed_seconds=20.0,
        wall_clock_limit=600.0,
        a2a_done=False,
        a2a_error=False,
        coordinator_error=False,
    ) == "success"
    assert classify_end_reason(
        finished=False,
        steps=10,
        max_steps=10,
        elapsed_seconds=20.0,
        wall_clock_limit=600.0,
        a2a_done=False,
        a2a_error=False,
        coordinator_error=False,
    ) == "max_steps_reached"
    assert classify_end_reason(
        finished=False,
        steps=3,
        max_steps=10,
        elapsed_seconds=601.0,
        wall_clock_limit=600.0,
        a2a_done=False,
        a2a_error=False,
        coordinator_error=False,
    ) == "wall_clock_timeout"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
```

Expected: import failures because helpers do not exist yet.

- [ ] **Step 3: Implement helpers in `sar_orch/experiment.py`**

Add near the constants:

```python
def build_run_metadata(
    *,
    run_id: str,
    scene: int,
    num_agents: int,
    seed: int,
    model: str,
    provider: str,
    api_base: str,
    max_steps: int,
    wall_clock_limit: float,
    sandbox_profile: str,
    coordinator_prompts: str,
    worker_prompts: str,
) -> dict:
    return {
        "run_id": run_id,
        "env_name": "SAR",
        "scenario_id": f"scene_{scene}",
        "scene": scene,
        "seed": seed,
        "agent_count": num_agents,
        "model": model,
        "provider": provider,
        "api_base": api_base,
        "max_steps": max_steps,
        "wall_clock_timeout": wall_clock_limit,
        "sandbox_profile": sandbox_profile,
        "task_objective": "Extinguish all fires and rescue all persons",
        "success_criteria": "SAR checker subtasks complete",
        "coordinator_prompts": coordinator_prompts,
        "worker_prompts": worker_prompts,
        "prompt_version": "baseline",
    }


def classify_end_reason(
    *,
    finished: bool,
    steps: int,
    max_steps: int,
    elapsed_seconds: float,
    wall_clock_limit: float,
    a2a_done: bool,
    a2a_error: bool,
    coordinator_error: bool,
) -> str:
    if finished:
        return "success"
    if coordinator_error or a2a_error:
        return "framework_error"
    if elapsed_seconds >= wall_clock_limit:
        return "wall_clock_timeout"
    if steps >= max_steps:
        return "max_steps_reached"
    if a2a_done:
        return "coordinator_finished_early"
    return "stopped_before_success"
```

- [ ] **Step 4: Wire metadata and end reason into `run_experiment()`**

In `run_experiment()`, generate a `run_id` after logger creation:

```python
import uuid

run_id = f"sar-scene{scene}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"
wall_clock_limit = 600.0
exp_logger.write_metadata(
    build_run_metadata(
        run_id=run_id,
        scene=scene,
        num_agents=num_agents,
        seed=seed,
        model=model,
        provider=provider,
        api_base=api_base,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit,
        sandbox_profile=sandbox_profile,
        coordinator_prompts=_COORDINATOR_PROMPTS,
        worker_prompts=_WORKER_PROMPTS,
    )
)
```

Move the existing `wall_clock_limit = 600.0` assignment so the same variable is used for metadata and the poll loop. Track booleans `a2a_done`, `a2a_error`, and `coordinator_error` in the poll loop, then add final fields:

```python
end_reason = classify_end_reason(
    finished=final_metrics["finished"],
    steps=final_metrics["steps"],
    max_steps=max_steps,
    elapsed_seconds=elapsed_total,
    wall_clock_limit=wall_clock_limit,
    a2a_done=a2a_done,
    a2a_error=a2a_error,
    coordinator_error=coordinator_error,
)
final_metrics["end_reason"] = end_reason
final_metrics["run_id"] = run_id
final_metrics["max_steps"] = max_steps
```

Pass new fields to `log_step()`:

```python
exp_logger.log_step(
    step_num=metrics["steps"],
    actions=step_log.get("actions", []),
    successes=step_log.get("successes", []),
    observations=step_log.get("observations", []),
    coverage=metrics["coverage"],
    transport_rate=metrics["transport_rate"],
    finished=metrics["finished"],
    timeout_agents=step_log.get("timeout_agents", []),
    run_id=run_id,
    max_steps=max_steps,
    remaining_steps=max(0, max_steps - metrics["steps"]),
    wall_time_since_start=elapsed,
    step_duration_ms=step_log.get("step_duration_ms", ""),
    error_types=step_log.get("error_types", []),
    completed_subtasks_delta=step_log.get("completed_subtasks_delta", []),
    end_reason="",
)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
```

Expected: all tests in this file pass.

## Task 3: Barrier Step Diagnostics

**Files:**
- Create: `tests/test_sar_barrier_observability.py`
- Modify: `sar_orch/barrier.py`

**Interfaces:**
- Consumes: `SARBarrier.get_last_step_log() -> dict`.
- Produces: `get_last_step_log()` returns existing keys plus `step_duration_ms`, `error_types`, and `completed_subtasks_delta`.

- [ ] **Step 1: Write barrier observability test**

Add this test file:

```python
from __future__ import annotations

from sar_orch.barrier import SARBarrier


def test_last_step_log_exposes_error_types_and_duration(monkeypatch):
    barrier = SARBarrier(num_agents=1, scene=1, seed=42)

    def fake_step(actions):
        barrier.env.event = {"error_type": "not_visible"}
        return ["act text"], [False]

    monkeypatch.setattr(barrier.env, "step", fake_step)
    monkeypatch.setattr(barrier.env, "generate_obs_text", lambda idx: ("obs", {}))
    monkeypatch.setattr(barrier.env, "get_agent_state", lambda idx: "state")
    monkeypatch.setattr(barrier.env.checker, "check_success", lambda: False)

    barrier._action_queue[0] = "NavigateTo(MissingTarget)"
    barrier._execute_step(expected_step=0)

    log = barrier.get_last_step_log()
    assert log["actions"] == ["NavigateTo(MissingTarget)"]
    assert log["successes"] == [False]
    assert log["error_types"] == ["not_visible"]
    assert isinstance(log["step_duration_ms"], float)
    assert log["step_duration_ms"] >= 0.0
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_sar_barrier_observability.py -v
```

Expected: failure because `error_types` and `step_duration_ms` are not present.

- [ ] **Step 3: Capture timing and error types in `SARBarrier`**

In `__init__()`, add:

```python
self._last_error_types: list[str] = []
self._last_step_duration_ms: float = 0.0
self._last_completed_subtasks_delta: list[str] = []
self._previous_completed_subtasks: set[str] = set()
```

In `_execute_step()`, wrap `self.env.step(actions)`:

```python
started = time.monotonic()
obs_text, act_successes = self.env.step(actions)
self._last_step_duration_ms = (time.monotonic() - started) * 1000.0
```

After step execution, infer error types conservatively:

```python
error_type = ""
event = getattr(self.env, "event", None)
if isinstance(event, dict):
    error_type = str(event.get("error_type", "") or "")
error_types = []
for success in act_successes or []:
    error_types.append("" if success else error_type)
self._last_error_types = error_types
```

For completed subtasks, use checker state only when available:

```python
completed = set(getattr(self.env.checker, "subtasks_completed", []) or [])
self._last_completed_subtasks_delta = sorted(completed - self._previous_completed_subtasks)
self._previous_completed_subtasks = completed
```

Extend `get_last_step_log()`:

```python
return {
    "actions": list(self._last_actions),
    "successes": list(self._last_successes),
    "observations": list(self._last_observations),
    "timeout_agents": list(self._last_timeout_agents),
    "error_types": list(self._last_error_types),
    "step_duration_ms": self._last_step_duration_ms,
    "completed_subtasks_delta": list(self._last_completed_subtasks_delta),
}
```

- [ ] **Step 4: Run barrier tests**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_sar_barrier_observability.py integration/test_sar_barrier.py -v
```

Expected: new observability test passes and existing barrier integration tests still pass.

## Task 4: Coordinator And Worker Correlation Logging

**Files:**
- Modify: `sar_orch/worker.py`
- Modify: `sar_orch/coordinator.py`
- Modify: `tests/test_experiment_logger_observability.py`

**Interfaces:**
- Consumes: Extended `ExperimentLogger.log_agent_interaction()`, `log_router_interaction()`, `log_token_usage()`, `log_event()`, and `log_subtask()`.
- Produces: `RunID`, `CorrelationID`, `EventType`, and latency fields in interaction CSVs when available.

- [ ] **Step 1: Add logger-level interaction tests**

Append to `tests/test_experiment_logger_observability.py`:

```python
def test_logger_accepts_correlation_fields_for_interactions(tmp_path: Path):
    logger = ExperimentLogger(log_dir=str(tmp_path))
    logger.log_agent_interaction(
        step=3,
        agent="Alice",
        tool_name="navigate_to",
        tool_args='{"target":"ReservoirUtah"}',
        action="NavigateTo(ReservoirUtah)",
        observation="Arrived",
        run_id="run-1",
        correlation_id="Alice-tool-1",
        event_type="tool_result",
        tool_latency_ms=12.5,
        error_type="",
    )
    logger.log_router_interaction(
        step=3,
        subtask="Go to reservoir",
        assigned_to="Alice",
        run_id="run-1",
        correlation_id="dispatch-1",
        worker_task_id="dispatch-1",
        event_type="dispatch_task",
    )
    logger.log_token_usage(
        step=3,
        agent="Alice",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        run_id="run-1",
        llm_latency_ms=20.0,
        model="fake-model",
        prompt_version="baseline",
    )
    logger.close()

    assert read_csv(tmp_path / "agent_interactions.csv")[0]["CorrelationID"] == "Alice-tool-1"
    assert read_csv(tmp_path / "router_interactions.csv")[0]["WorkerTaskID"] == "dispatch-1"
    assert read_csv(tmp_path / "token_usage.csv")[0]["LLMLatencyMs"] == "20.0"
```

- [ ] **Step 2: Run tests and verify they fail if Task 1 did not add all fields**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
```

Expected: pass if Task 1 already added these extended fields; otherwise fail on missing keyword arguments or CSV headers.

- [ ] **Step 3: Add correlation state to `SARWorker` callbacks**

In `sar_orch/worker.py`, import `time` and use existing `_call_seq`:

```python
import time
```

In `tool_start`, store timestamp and correlation ID:

```python
self._call_seq += 1
self._pending_tool = {
    "tool_name": data.get("tool_name", ""),
    "arguments": data.get("arguments", {}),
    "started_at": time.monotonic(),
    "correlation_id": f"{self.agent_name}-tool-{self._call_seq}",
}
```

In `tool_result`, pass latency and correlation fields:

```python
tool_latency_ms = (time.monotonic() - self._pending_tool["started_at"]) * 1000.0
exp.log_agent_interaction(
    step=getattr(self._barrier, "_step_counter", 0),
    agent=self.agent_name,
    tool_name=tool_name,
    tool_args=json.dumps(args, ensure_ascii=False),
    action=_build_action(tool_name, args),
    observation=data.get("content", ""),
    llm_input=self._last_llm_input,
    llm_output=self._last_llm_output,
    correlation_id=self._pending_tool["correlation_id"],
    event_type="tool_result",
    tool_latency_ms=tool_latency_ms,
)
```

- [ ] **Step 4: Add coordinator correlation and subtask timeline logging**

In `sar_orch/coordinator.py`, add a sequence counter in `__init__()`:

```python
self._dispatch_seq = 0
```

In `_router_cb`, when `tool_name == "dispatch_task"`:

```python
self._dispatch_seq += 1
correlation_id = f"coordinator-dispatch-{self._dispatch_seq}"
worker_task_id = f"dispatch-{self._dispatch_seq}"
self._exp_logger.log_router_interaction(
    step=step,
    subtask=args.get("prompt", ""),
    assigned_to=args.get("agent_id", ""),
    correlation_id=correlation_id,
    worker_task_id=worker_task_id,
    event_type="dispatch_task",
)
self._exp_logger.log_subtask(
    subtask_id=worker_task_id,
    status="assigned",
    step=step,
    assigned_to=args.get("agent_id", ""),
    subtask=args.get("prompt", ""),
)
self._exp_logger.log_event(
    "dispatch_task",
    step=step,
    agent="Coordinator",
    correlation_id=correlation_id,
    payload=args,
)
```

Keep existing behavior for other coordinator tools and add `event_type` fields where useful.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_experiment_logger_observability.py -v
uv run --with ruff ruff check sar_orch/worker.py sar_orch/coordinator.py sar_orch/logger.py
```

Expected: tests pass and ruff reports no errors.

## Task 5: Enhanced Aggregation And Failure Taxonomy

**Files:**
- Create: `tests/test_aggregate_observability.py`
- Modify: `sar_orch/aggregate.py`

**Interfaces:**
- Consumes: Existing `result.json` plus optional `metadata.json` in each run directory.
- Produces: Aggregated TSV with existing columns plus optional observability columns: `end_reason`, `max_steps`, `elapsed_seconds`, `run_id`, `model`, `prompt_version`, `failure_class`.

- [ ] **Step 1: Write aggregation tests**

Add this test file:

```python
from __future__ import annotations

import csv
import json
from pathlib import Path

from sar_orch.aggregate import aggregate


def test_aggregate_includes_observability_columns(tmp_path: Path):
    run_dir = tmp_path / "benchmark" / "scene_1" / "agents_2" / "seed_42"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "finished": False,
                "steps": 10,
                "max_steps": 10,
                "coverage": 0.5,
                "transport_rate": 0.25,
                "elapsed_seconds": 123.0,
                "end_reason": "max_steps_reached",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metadata.json").write_text(
        json.dumps({"model": "fake-model", "prompt_version": "baseline"}),
        encoding="utf-8",
    )

    output = tmp_path / "out.tsv"
    aggregate(str(tmp_path / "benchmark"), str(output))

    with output.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    assert rows[0]["end_reason"] == "max_steps_reached"
    assert rows[0]["failure_class"] == "budget"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["model"] == "fake-model"
    assert rows[0]["prompt_version"] == "baseline"
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_aggregate_observability.py -v
```

Expected: failure because aggregate output does not include the new columns.

- [ ] **Step 3: Implement failure classification in `sar_orch/aggregate.py`**

Add helper:

```python
def classify_failure(end_reason: str, finished: bool) -> str:
    if finished:
        return "success"
    if end_reason in {"max_steps_reached", "wall_clock_timeout"}:
        return "budget"
    if end_reason in {"framework_error", "worker_timeout", "coordinator_finished_early"}:
        return "framework"
    if end_reason == "environment_error":
        return "environment"
    return "unknown"
```

When reading each run directory, load metadata if present:

```python
metadata_file = seed_dir / "metadata.json"
metadata = {}
if metadata_file.exists():
    try:
        with open(str(metadata_file), encoding="utf-8") as f:
            metadata = json.load(f)
    except (json.JSONDecodeError, OSError):
        metadata = {}
```

Append new fields while preserving old fields:

```python
end_reason = metrics.get("end_reason", "")
rows.append(
    {
        "scene": scene,
        "agents": agents,
        "seed": seed,
        "steps": steps,
        "balance": 1.0 if finished else transport_rate,
        "coverage": coverage,
        "success_rate": 1.0 if finished else 0.0,
        "transport_rate": transport_rate,
        "end_reason": end_reason,
        "failure_class": classify_failure(end_reason, finished),
        "max_steps": metrics.get("max_steps", ""),
        "elapsed_seconds": metrics.get("elapsed_seconds", ""),
        "run_id": metrics.get("run_id", metadata.get("run_id", "")),
        "model": metadata.get("model", ""),
        "prompt_version": metadata.get("prompt_version", ""),
    }
)
```

Extend `fieldnames` with the new columns after existing columns.

- [ ] **Step 4: Run aggregation tests**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_aggregate_observability.py -v
```

Expected: test passes.

## Task 6: Documentation Sync And End-To-End Verification

**Files:**
- Modify: `docs/system_docs/logging_map.md`
- Modify: `docs/system_docs/experiment_design.md`

**Interfaces:**
- Consumes: Implemented CSV, JSON, and NDJSON fields from Tasks 1-5.
- Produces: Documentation that matches actual output files and commands.

- [ ] **Step 1: Update `logging_map.md` output file list**

Add entries for:

```markdown
| `metadata.json` | Run metadata: run_id, env, scene, seed, agent_count, model, prompt_version, max_steps, timeout, prompt paths | Reproducibility and experiment grouping |
| `events.ndjson` | Unified event stream for coordinator, worker, A2A-adjacent callbacks, barrier, and environment events | Cross-file debugging and correlation |
| `subtasks.csv` | Subtask assignment and status timeline | Coordinator planning and task completion analysis |
```

- [ ] **Step 2: Update field tables in `logging_map.md`**

Add the newly implemented fields to the existing sections:

```markdown
`trajectory.csv` new fields: `RunID`, `MaxSteps`, `RemainingSteps`, `WallTimeSinceStart`, `StepDurationMs`, `ErrorTypes`, `CompletedSubtasksDelta`, `EndReason`.

`agent_interactions.csv` new fields: `RunID`, `CorrelationID`, `EventType`, `ToolLatencyMs`, `ErrorType`.

`router_interactions.csv` new fields: `RunID`, `CorrelationID`, `WorkerTaskID`, `EventType`.

`token_usage.csv` new fields: `RunID`, `LLMLatencyMs`, `Model`, `PromptVersion`.
```

- [ ] **Step 3: Add implementation status note to `experiment_design.md`**

Append a short section after “最小补充实现清单”:

```markdown
## 15. 实施状态

实验可观测性改进已按 `docs/plans/2026-07-05-experiment-observability-improvements.md` 实施。新增输出包括 `metadata.json`、`events.ndjson`、`subtasks.csv`，并扩展 `trajectory.csv`、`agent_interactions.csv`、`router_interactions.csv`、`token_usage.csv` 和聚合 TSV 字段。
```

Renumber the previous summary section if needed so headings remain coherent.

- [ ] **Step 4: Run full focused verification**

Run:

```bash
env PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_experiment_logger_observability.py \
  tests/test_sar_barrier_observability.py \
  tests/test_aggregate_observability.py \
  integration/test_sar_barrier.py -v
uv run --with ruff ruff check sar_orch/ tests/ integration/test_sar_barrier.py
```

Expected: tests pass and ruff reports no errors.

- [ ] **Step 5: Run a smoke experiment only if API credentials are available**

Run:

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 1 --seed 42 --max-steps 2
```

Expected when credentials and model access are configured: a result directory is created under `sar_orch/results/`, and it contains `metadata.json`, `trajectory.csv`, `summary.csv`, and any interaction logs produced before the short run exits. If credentials are unavailable, skip this smoke run and record that only unit/integration tests were executed.

## Self-Review Checklist

- Spec coverage: Tasks 1-5 cover metadata, end reason, trajectory extension, error type, latency, correlation ID, subtask timeline, events, and aggregation. Task 6 covers documentation sync.
- Completeness scan: This plan contains only concrete tasks, commands, and implementation steps.
- Type consistency: New logger methods use `dict[str, Any]`, optional keyword fields, and existing CSV writer patterns. New experiment helpers return plain `dict` and `str` for easy testing.
- Compatibility: Existing CSV columns remain in place; new columns are appended. Existing single experiment and benchmark commands remain valid.

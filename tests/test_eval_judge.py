"""Tests for ``sar_orch.eval.judge`` (sar-metrics v1 V1-Judge metrics).

Cover the pinned contracts of the D card with a mock LLM client:

- prompt assembly carries the four Judge-Task principles for both metrics
  (single responsibility / reason-first / negative examples / strict JSON);
- tolerant JSON extraction (fences, prose, nested braces) and fail-closed
  handling of truncated or invalid output;
- ``judge_error`` counts in the denominator but not the numerator; retry
  budget is exactly 1 initial + <=2 retries with usage from every attempt;
- observation_ignore sampling is uniform across steps and deterministic per
  run; numerator/denominator accounting follows the card convention;
- planning_path normalization (score 0-5, deduction categories, tolerant
  coercion) and missing-input / unconfigured degradation;
- CLI exit codes and artifact placement (default + ``--output``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sar_orch.eval.judge import (
    DEFAULT_OUTPUT,
    DEFAULT_SAMPLE_SIZE,
    EVALUATOR_VERSION,
    SCHEMA_VERSION,
    evaluate_observation_ignore,
    evaluate_planning_path,
    evaluate_run,
    write_artifact,
)
from sar_orch.eval.judge.__main__ import (
    EXIT_INVALID_INPUT,
    EXIT_JUDGE_UNCONFIGURED,
    EXIT_METRICS_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    main,
)
from sar_orch.eval.judge.artifacts import (
    ACTION_TOOLS,
    NON_ACTION_TOOLS,
    build_observation_digest,
    build_scene_digest,
    extract_env_sections,
    extract_task_instruction,
    is_action_round,
    iter_worker_rounds,
)
from sar_orch.eval.judge.client import (
    RETRY_SUFFIX,
    JudgeCallError,
    JudgeCompletion,
    JudgeParseError,
    extract_json_object,
    run_judge_json,
)
from sar_orch.eval.judge.observation_ignore import (
    _sample_seed,
    collect_candidates,
    sample_candidates,
)
from sar_orch.eval.judge.planning_path import DEDUCTION_CATEGORIES
from sar_orch.eval.judge.prompts import (
    OBSERVATION_IGNORE_SYSTEM_PROMPT,
    PLANNING_PATH_SYSTEM_PROMPT,
    build_observation_ignore_user_prompt,
    build_planning_path_user_prompt,
)

TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

ENV_STATE_BLOCK = """\
---
## Environment State
---
### Spatial State
- AgniFire (fire)
  - position: [16, 17, 0]
  - status: extinguished
### Embodied State
- position: [5, 5, 0]
- inventory: {'Water': 0, 'Sand': 3}
### Relevant Recent Events
- seq=1 callback.status_update actor=Alice dispatch=dsp_unit
### Task Execution State
- dsp_unit -> Alice [RUNNING] worker_task=aaaaaaaa
### Freshness / Conflicts / Evidence
- freshness: FRESH
"""


def _task_envelope(agent: str = "Alice") -> str:
    return json.dumps(
        {
            "protocol": "a2a-peer",
            "kind": "task",
            "sender_id": "Coordinator",
            "recipient_id": agent,
            "body": {
                "content": "Extinguish the fire at AgniFire.",
                "task_id": "dsp_unit",
            },
        }
    )


def _request_event(
    step: int,
    *,
    tool_result: str | None = None,
    agent: str = "Alice",
    ts: str = "2026-09-14T00:00:00Z",
) -> dict:
    messages: list[dict] = [
        {"role": "system", "content": "worker system prompt"},
        {"role": "user", "content": _task_envelope(agent)},
    ]
    if tool_result is not None:
        messages.append(
            {
                "role": "assistant",
                "content": "prior move",
                "tool_calls": [
                    {
                        "id": "call_prev",
                        "type": "function",
                        "function": {"name": "explore", "arguments": {}},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "content": tool_result, "name": "explore"})
    messages.append({"role": "user", "content": ENV_STATE_BLOCK})
    return {
        "ts": ts,
        "task_id": TASK_ID,
        "event": "llm_request",
        "step_index": step,
        "messages": messages,
        "tools": [],
    }


def _response_event(
    step: int,
    tool_names: list[str],
    *,
    status: str | None = None,
    content: str = "reasoning text",
    ts: str = "2026-09-14T00:00:05Z",
) -> dict:
    event = {
        "ts": ts,
        "task_id": TASK_ID,
        "event": "llm_response",
        "step_index": step,
        "content": content,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": {}},
            }
            for name in tool_names
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    if status is not None:
        event["status"] = status
    return event


class FakeJudgeClient:
    """Scripted judge client: rule-matched or sequential responses."""

    def __init__(self, responses=None, *, rules=None, default_response=None):
        self.responses = list(responses or [])
        self.rules = list(rules or [])
        self.default_response = default_response
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _resolve(item):
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, JudgeCompletion):
            return item
        return JudgeCompletion(
            content=str(item),
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cache_hit_tokens": 10,
            },
        )

    def complete(self, *, system_prompt: str, user_prompt: str) -> JudgeCompletion:
        self.calls.append((system_prompt, user_prompt))
        for needle, response in self.rules:
            if needle in user_prompt:
                return self._resolve(response)
        if self.responses:
            return self._resolve(self.responses.pop(0))
        if self.default_response is not None:
            return self._resolve(self.default_response)
        raise AssertionError("FakeJudgeClient ran out of scripted responses")


def _write_worker_ndjson(
    run_dir: Path, agent: str, rounds: list[tuple[int, list[str]]], *, aborted_steps=()
) -> None:
    events: list[dict] = []
    for index, (step, tools) in enumerate(rounds):
        events.append(
            _request_event(
                step,
                tool_result="Directly around me, I can see: nothing new.",
                agent=agent,
                ts=f"2026-09-14T00:00:{index:02d}Z",
            )
        )
        events.append(_response_event(step, tools))
    for step in aborted_steps:
        events.append(_request_event(step, agent=agent))
        events.append(
            _response_event(step, [], status="aborted", content="interrupted")
        )
    path = run_dir / "workers" / agent / agent / f"{TASK_ID}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _build_run(
    tmp_path: Path,
    *,
    rounds: list[tuple[int, list[str]]] | None = None,
    workers: dict[str, list[tuple[int, list[str]]]] | None = None,
    aborted_steps=(),
    with_planning_inputs: bool = True,
) -> Path:
    """Synthetic mini run dir for judge tests."""
    run_dir = tmp_path / "run-judge-unit"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": "run-judge-unit"}), encoding="utf-8"
    )
    if workers is None:
        workers = {"Alice": rounds or [(0, ["explore"]), (1, ["navigate_to"])]}
    for agent, agent_rounds in workers.items():
        _write_worker_ndjson(run_dir, agent, agent_rounds, aborted_steps=aborted_steps)
    if with_planning_inputs:
        (run_dir / "scene_config.json").write_text(
            json.dumps(
                {
                    "grid": {"width": 30, "height": 30, "altitude": 1},
                    "num_agents": 2,
                    "scene": 3,
                    "seed": 42,
                    "objects": {
                        "agents": [
                            {"name": "Alice", "position": {"x": 9, "y": 7, "z": 0}},
                            {"name": "Bob", "position": {"x": 3, "y": 24, "z": 0}},
                        ],
                        "fires": [
                            {
                                "name": "AgniFire",
                                "fire_type": "Non-chemical",
                                "position": {"x": 16, "y": 17, "z": 0},
                                "average_intensity": "Low",
                                "flammable_ids": ["Flammable|1", "Flammable|2"],
                            }
                        ],
                        "flammables": [
                            {
                                "name": "AgniFire_Region_1",
                                "parent_fire": "AgniFire",
                                "position": {"x": 16, "y": 17, "z": 0},
                            }
                        ],
                        "reservoirs": [
                            {
                                "name": "ReservoirLibre",
                                "resource_type": "Water",
                                "position": {"x": 10, "y": 17, "z": 0},
                            }
                        ],
                        "deposits": [
                            {"name": "DepositFacility", "position": {"x": 13, "y": 25, "z": 0}}
                        ],
                        "persons": [
                            {
                                "name": "LostPersonThomas",
                                "position": {"x": 25, "y": 6, "z": 0},
                                "load": 2,
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "router_interactions.csv").write_text(
            "Step,Subtask,AssignedTo,RunID,CorrelationID,WorkerTaskID,EventType,"
            "Success,ErrorType\n"
            '0,Explore,Alice,r1,c1,w1,assign_task,,\n'
            '1,Extinguish,Alice,r1,c2,w2,assign_task,,\n'
            '2,cancel_task(task_id=dsp_x),Worker,r1,c3,w2,cancel_task,,\n',
            encoding="utf-8",
        )
        (run_dir / "subtasks.csv").write_text(
            "RunID,Step,SubtaskID,Status,AssignedTo,Subtask,CreatedAt,UpdatedAt,"
            "FailureClass,Details\n"
            "r1,0,dispatch-1,assigned,Alice,Explore,,,\n"
            "r1,1,dispatch-2,canceled,Worker,cancel_task,,\n",
            encoding="utf-8",
        )
    return run_dir


def _planning_response(score: int = 3, category: str = "incomplete_coverage") -> str:
    return json.dumps(
        {
            "reasoning": "The plan left one agent idle for long stretches.",
            "score": score,
            "deductions": [{"category": category, "detail": "steps 3-6"}],
        }
    )


def _observation_response(contradiction: bool, reasoning: str = "checked") -> str:
    return json.dumps({"reasoning": reasoning, "contradiction": contradiction})


# --------------------------------------------------------------------------
# prompt assembly — the four Judge-Task principles
# --------------------------------------------------------------------------


def test_planning_system_prompt_carries_four_principles():
    prompt = PLANNING_PATH_SYSTEM_PROMPT
    assert "single job" in prompt  # single responsibility
    assert "Write the reasoning FIRST" in prompt  # reason before conclusion
    assert "GOOD PATH" in prompt and "BAD PATH" in prompt  # contrast examples
    last_line = [line for line in prompt.strip().splitlines() if line.strip()][-1]
    assert last_line.startswith('{"reasoning"')  # strict JSON template last
    assert last_line.index('"reasoning"') < last_line.index('"score"')
    assert '"score": <integer 0-5>' in prompt


def test_observation_system_prompt_carries_four_principles():
    prompt = OBSERVATION_IGNORE_SYSTEM_PROMPT
    assert "single job" in prompt
    assert "Write the reasoning FIRST" in prompt
    assert "CONTRADICTION TRUE" in prompt and "CONTRADICTION FALSE" in prompt
    last_line = [line for line in prompt.strip().splitlines() if line.strip()][-1]
    assert last_line.startswith('{"reasoning"')
    assert last_line.index('"reasoning"') < last_line.index('"contradiction"')
    assert '"contradiction"' in last_line


def test_planning_user_prompt_contains_artifacts_and_truncation_notes():
    user = build_planning_path_user_prompt(
        scene_digest="Grid: 30 x 30",
        dispatch_timeline="step=0 | assign_task | to=Alice",
        subtask_log="step=0 | dispatch-1 | assigned",
        timeline_truncated=True,
        subtask_log_truncated=False,
    )
    assert "## SCENE" in user
    assert "Grid: 30 x 30" in user
    assert "## DISPATCH TIMELINE" in user
    assert "assign_task" in user
    assert "## SUBTASK LOG" in user
    assert "timeline compacted" in user
    assert "subtask log compacted" not in user


def test_observation_user_prompt_contains_sample_sections():
    user = build_observation_ignore_user_prompt(
        agent="Alice",
        step=4,
        task_instruction="Extinguish AgniFire.",
        observation_digest="### Spatial State\n- AgniFire",
        decision_text="Tool call(s):\n- navigate_to({})",
    )
    assert "## TASK (agent Alice, step 4)" in user
    assert "Extinguish AgniFire." in user
    assert "## LATEST OBSERVATION" in user
    assert "## DECISION" in user
    assert "navigate_to" in user


# --------------------------------------------------------------------------
# tolerant JSON extraction / fail-closed parsing
# --------------------------------------------------------------------------


def test_extract_json_object_tolerates_fences_prose_and_nesting():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object(
        'Here is my verdict:\n{"a": {"b": "x}y{"}, "c": 2}\nHope it helps.'
    ) == {"a": {"b": "x}y{"}, "c": 2}
    assert extract_json_object('text {"ok": true} trailing {not json') == {"ok": True}


def test_extract_json_object_rejects_truncated_and_non_objects():
    with pytest.raises(JudgeParseError):
        extract_json_object('{"reasoning": "cut off')
    with pytest.raises(JudgeParseError):
        extract_json_object("[1, 2, 3]")
    with pytest.raises(JudgeParseError):
        extract_json_object("")
    with pytest.raises(JudgeParseError):
        extract_json_object("no json at all")


def test_run_judge_json_retries_with_corrective_suffix_and_accumulates_usage():
    client = FakeJudgeClient(["not json", _observation_response(True)])
    outcome = run_judge_json(
        client,
        system_prompt="sys",
        user_prompt="user",
        parse=lambda payload: payload,
    )
    assert outcome.ok
    assert outcome.attempts == 2
    assert outcome.usage["calls"] == 2
    assert outcome.usage["prompt_tokens"] == 200  # both attempts billed
    assert client.calls[0][1] == "user"
    assert client.calls[1][1] == "user" + RETRY_SUFFIX


def test_run_judge_json_fails_closed_after_max_attempts():
    client = FakeJudgeClient([JudgeCallError("gateway 502")], default_response="oops")
    outcome = run_judge_json(
        client,
        system_prompt="sys",
        user_prompt="user",
        parse=lambda payload: payload,
        max_attempts=3,
    )
    assert not outcome.ok
    assert outcome.value is None
    assert outcome.attempts == 3
    assert outcome.usage["calls"] == 3
    assert outcome.error is not None


def test_metric_parses_fenced_dirty_output(tmp_path):
    run_dir = _build_run(tmp_path, rounds=[(0, ["explore"])])
    client = FakeJudgeClient(
        ['```json\n' + _observation_response(False) + "\n```"]
    )
    payload = evaluate_observation_ignore(run_dir, client, sample_size=1)
    assert payload["status"] == "ok"
    assert payload["denominator"] == 1
    assert payload["samples"][0]["contradiction"] is False


# --------------------------------------------------------------------------
# observation_ignore accounting / sampling
# --------------------------------------------------------------------------


def test_observation_ignore_numerator_denominator_convention(tmp_path):
    run_dir = _build_run(
        tmp_path,
        rounds=[(0, ["explore"]), (0, ["navigate_to"]), (1, ["use_supply"])],
    )
    client = FakeJudgeClient(
        [
            _observation_response(True, "used water with none in stock"),
            _observation_response(False, "consistent"),
            JudgeCallError("gateway down"),
        ],
        default_response=JudgeCallError("gateway down"),
    )
    payload = evaluate_observation_ignore(run_dir, client, sample_size=3)
    assert payload["status"] == "ok"
    assert payload["denominator"] == 3
    assert payload["numerator"] == 1
    assert payload["judged_count"] == 2
    assert payload["judge_error_count"] == 1
    assert payload["ignore_rate"] == pytest.approx(1 / 3)  # error in denominator
    samples = {sample["status"]: sample for sample in payload["samples"]}
    assert samples["judge_error"]["contradiction"] is None
    assert samples["judge_error"]["judge_attempts"] == 3
    assert payload["usage"]["calls"] == 2 + 3  # 2 ok calls + 3 failed attempts


def test_judge_error_never_counts_as_contradiction(tmp_path):
    run_dir = _build_run(
        tmp_path, rounds=[(0, ["explore"]), (1, ["explore"])], aborted_steps=(2,)
    )
    client = FakeJudgeClient(default_response=JudgeCallError("always down"))
    payload = evaluate_observation_ignore(run_dir, client, sample_size=5)
    assert payload["denominator"] == 2  # aborted round is not a candidate
    assert payload["numerator"] == 0
    assert payload["judge_error_count"] == 2
    assert payload["ignore_rate"] == 0.0


def test_non_action_rounds_are_not_candidates(tmp_path):
    run_dir = _build_run(
        tmp_path,
        rounds=[
            (0, ["report_observation"]),
            (0, ["get_agent_state"]),
            (1, ["map_agent__get_fire_info"]),
            (1, ["get_skill"]),
            (2, ["report_observation", "explore"]),
        ],
    )
    candidates, total_rounds = collect_candidates(run_dir)
    assert total_rounds == 5
    assert len(candidates) == 1
    assert candidates[0].tool_names() == ["report_observation", "explore"]


def test_sampling_is_uniform_across_steps_and_deterministic(tmp_path):
    # 3 candidates per step across 10 steps (Alice x2 shapes + Bob x1).
    workers = {
        "Alice": [(step, ["explore"]) for step in range(10)]
        + [(step, ["navigate_to"]) for step in range(10)],
        "Bob": [(step, ["use_supply"]) for step in range(10)],
    }
    run_dir = _build_run(tmp_path, workers=workers)
    candidates, _ = collect_candidates(run_dir)
    assert len(candidates) == 30

    seed = _sample_seed(run_dir)
    picked_first = sample_candidates(candidates, sample_size=12, seed=seed)
    picked_second = sample_candidates(candidates, sample_size=12, seed=seed)
    assert [r.request_ts for r in picked_first] == [r.request_ts for r in picked_second]

    counts: dict[int, int] = {}
    for round_ in picked_first:
        counts[round_.step] = counts.get(round_.step, 0) + 1
    assert sorted(counts) == list(range(10))
    assert max(counts.values()) - min(counts.values()) <= 1

    other_seed = sample_candidates(list(candidates), sample_size=6, seed=seed + 1)
    assert len(other_seed) == 6


def test_sampling_returns_all_candidates_when_short(tmp_path):
    run_dir = _build_run(tmp_path, rounds=[(0, ["explore"]), (1, ["explore"])])
    candidates, _ = collect_candidates(run_dir)
    picked = sample_candidates(candidates, sample_size=50, seed=0)
    assert len(picked) == 2


def test_no_candidates_status(tmp_path):
    run_dir = _build_run(tmp_path, rounds=[(0, ["report_observation"])])
    payload = evaluate_observation_ignore(
        run_dir, FakeJudgeClient(default_response=_observation_response(False))
    )
    assert payload["status"] == "no_candidates"
    assert payload["ignore_rate"] is None
    assert payload["denominator"] == 0
    assert payload["sample_size_requested"] == DEFAULT_SAMPLE_SIZE == 20


def test_missing_workers_dir_status(tmp_path):
    run_dir = _build_run(tmp_path, workers={}, with_planning_inputs=False)
    payload = evaluate_observation_ignore(run_dir, FakeJudgeClient())
    assert payload["status"] == "missing_input"
    assert payload["missing"] == ["workers/"]


def test_observation_ignore_unconfigured_without_client(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = evaluate_observation_ignore(run_dir, None)
    assert payload["status"] == "judge_unconfigured"


# --------------------------------------------------------------------------
# planning_path normalization
# --------------------------------------------------------------------------


def test_planning_path_ok_payload_and_usage(tmp_path):
    run_dir = _build_run(tmp_path)
    client = FakeJudgeClient([_planning_response(4, "redundant_cancel")])
    payload = evaluate_planning_path(run_dir, client)
    assert payload["status"] == "ok"
    assert payload["score"] == 4
    assert payload["max_score"] == 5
    assert payload["deductions"] == [
        {"category": "redundant_cancel", "detail": "steps 3-6"}
    ]
    assert payload["judge_error_count"] == 0
    assert payload["attempts"] == 1
    assert payload["input"]["router_rows"] == 3
    assert payload["input"]["subtask_rows"] == 2
    assert payload["usage"]["total_tokens"] == 120
    assert "## SCENE" in client.calls[0][1]


def test_planning_path_normalizes_categories_and_coerces_score(tmp_path):
    run_dir = _build_run(tmp_path)
    raw = json.dumps(
        {
            "reasoning": "mixed",
            "score": "3",
            "deductions": [
                {"category": "漏派", "detail": "rescue never dispatched"},
                {"category": "some novel category", "detail": "odd"},
                "plain string deduction",
            ],
        }
    )
    payload = evaluate_planning_path(run_dir, FakeJudgeClient([raw]))
    assert payload["status"] == "ok"
    assert payload["score"] == 3
    categories = [item["category"] for item in payload["deductions"]]
    assert categories == ["missing_dispatch", "other", "other"]
    assert "some novel category" in payload["deductions"][1]["detail"]
    assert set(categories) <= set(DEDUCTION_CATEGORIES) | {"other"}


def test_planning_path_invalid_score_retries_then_errors(tmp_path):
    run_dir = _build_run(tmp_path)
    raw = json.dumps({"reasoning": "x", "score": 9, "deductions": []})
    client = FakeJudgeClient(default_response=raw)
    payload = evaluate_planning_path(run_dir, client)
    assert payload["status"] == "judge_error"
    assert payload["score"] is None
    assert payload["judge_error_count"] == 1
    assert payload["attempts"] == 3
    assert len(client.calls) == 3


def test_planning_path_missing_inputs_degrade(tmp_path):
    run_dir = _build_run(tmp_path, with_planning_inputs=False)
    payload = evaluate_planning_path(run_dir, FakeJudgeClient())
    assert payload["status"] == "missing_input"
    assert payload["score"] is None
    assert payload["input"]["missing"] == [
        "scene_config.json",
        "router_interactions.csv",
        "subtasks.csv",
    ]


def test_planning_path_unconfigured_without_client(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = evaluate_planning_path(run_dir, None)
    assert payload["status"] == "judge_unconfigured"


# --------------------------------------------------------------------------
# run-level orchestration + artifact
# --------------------------------------------------------------------------


def test_evaluate_run_aggregates_totals(tmp_path):
    run_dir = _build_run(
        tmp_path, rounds=[(0, ["explore"]), (1, ["navigate_to"])]
    )
    client = FakeJudgeClient(
        rules=[
            ("## SCENE", _planning_response(5)),
            ("## LATEST OBSERVATION", _observation_response(True)),
        ]
    )
    payload = evaluate_run(run_dir, sample_size=2, client=client, judge_info={"model": "m"})
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["evaluator_version"] == EVALUATOR_VERSION
    assert payload["run_id"] == "run-judge-unit"
    assert payload["judge"] == {"model": "m"}
    metrics = payload["metrics"]
    assert set(metrics) == {"planning_path", "observation_ignore"}
    assert metrics["planning_path"]["score"] == 5
    assert metrics["observation_ignore"]["numerator"] == 2
    assert payload["totals"]["judge_calls"] == 3
    assert payload["totals"]["judge_error_count"] == 0


def test_evaluate_run_rejects_unknown_metric(tmp_path):
    run_dir = _build_run(tmp_path)
    with pytest.raises(ValueError):
        evaluate_run(run_dir, metrics=["not_a_metric"])


def test_write_artifact_defaults_and_custom_path(tmp_path):
    run_dir = _build_run(tmp_path)
    payload = evaluate_run(
        run_dir, metrics=["planning_path"], client=FakeJudgeClient(
            default_response=_planning_response(2)
        )
    )
    target = write_artifact(run_dir, payload)
    assert target == run_dir / DEFAULT_OUTPUT
    assert json.loads(target.read_text(encoding="utf-8")) == payload

    custom = tmp_path / "nested" / "custom.json"
    written = write_artifact(run_dir, payload, custom)
    assert written == custom and custom.is_file()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_default_artifact_and_exit_codes(tmp_path, capsys):
    run_dir = _build_run(tmp_path, rounds=[(0, ["explore"])])
    client = FakeJudgeClient(
        rules=[
            ("## SCENE", _planning_response(3)),
            ("## LATEST OBSERVATION", _observation_response(False)),
        ]
    )
    assert (
        main(
            ["--results-dir", str(run_dir), "--sample-size", "1"],
            client=client,
            judge_info={"model": "fake"},
        )
        == EXIT_OK
    )
    target = run_dir / DEFAULT_OUTPUT
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["judge"] == {"model": "fake"}
    assert set(payload["metrics"]) == {"planning_path", "observation_ignore"}
    out = capsys.readouterr().out
    assert "wrote" in out and "planning_path: ok" in out

    subset = main(
        ["--results-dir", str(run_dir), "--metrics", "planning_path"],
        client=FakeJudgeClient(default_response=_planning_response(4)),
    )
    assert subset == EXIT_OK
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert set(payload["metrics"]) == {"planning_path"}

    custom = tmp_path / "judge_out.json"
    assert (
        main(
            ["--results-dir", str(run_dir), "--output", str(custom)],
            client=FakeJudgeClient(default_response=_planning_response(4)),
            judge_info={"model": "fake"},
        )
        == EXIT_OK
    )
    assert custom.is_file()


def test_cli_rejects_bad_inputs(tmp_path, capsys):
    assert main(["--results-dir", str(tmp_path / "nope")]) == EXIT_INVALID_INPUT
    run_dir = _build_run(tmp_path)
    assert (
        main(
            ["--results-dir", str(run_dir), "--metrics", "bogus"],
            client=FakeJudgeClient(),
        )
        == EXIT_USAGE
    )
    assert (
        main(
            ["--results-dir", str(run_dir), "--sample-size", "0"],
            client=FakeJudgeClient(),
        )
        == EXIT_USAGE
    )


def test_cli_unconfigured_env_writes_statuses(tmp_path, monkeypatch):
    for key in (
        "provider",
        "model",
        "api_key",
        "api_base",
        "reflection_provider",
        "reflection_model",
        "reflection_api_key",
        "reflection_api_base",
    ):
        monkeypatch.delenv(key, raising=False)
    run_dir = _build_run(tmp_path)
    code = main(
        [
            "--results-dir",
            str(run_dir),
            "--env-file",
            str(tmp_path / "does-not-exist.env"),
        ]
    )
    assert code == EXIT_JUDGE_UNCONFIGURED
    payload = json.loads((run_dir / DEFAULT_OUTPUT).read_text(encoding="utf-8"))
    assert payload["judge"] is None
    assert payload["metrics"]["planning_path"]["status"] == "judge_unconfigured"
    assert payload["metrics"]["observation_ignore"]["status"] == "judge_unconfigured"


def test_cli_all_metrics_failed_exit_code(tmp_path):
    run_dir = _build_run(tmp_path, with_planning_inputs=False)
    for agent_dir in (run_dir / "workers").iterdir():
        for ndjson in agent_dir.rglob("*.ndjson"):
            ndjson.unlink()
    client = FakeJudgeClient(default_response=JudgeCallError("down"))
    code = main(["--results-dir", str(run_dir)], client=client)
    assert code == EXIT_METRICS_FAILED


# --------------------------------------------------------------------------
# readers / digests
# --------------------------------------------------------------------------


def test_env_digest_keeps_facts_and_drops_event_feed(tmp_path):
    run_dir = _build_run(tmp_path, rounds=[(0, ["explore"])])
    rounds = list(iter_worker_rounds(run_dir / "workers"))
    assert len(rounds) == 1
    digest = build_observation_digest(rounds[0].messages)
    assert "### Spatial State" in digest
    assert "status: extinguished" in digest
    assert "### Embodied State" in digest
    assert "### Task Execution State" in digest
    assert "callback.status_update" not in digest  # event feed dropped
    assert "FRESH" not in digest


def test_env_digest_falls_back_when_sections_missing():
    digest = extract_env_sections("---\n## Environment State\n---\nplain text only")
    assert "plain text only" in digest


def test_task_instruction_extraction():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": _task_envelope()},
        {"role": "user", "content": ENV_STATE_BLOCK},
    ]
    assert extract_task_instruction(messages) == "Extinguish the fire at AgniFire."
    assert extract_task_instruction([{"role": "user", "content": ENV_STATE_BLOCK}]) is None


def test_scene_digest_renders_layout():
    digest = build_scene_digest(
        {
            "grid": {"width": 30, "height": 30, "altitude": 1},
            "num_agents": 2,
            "seed": 42,
            "objects": {
                "agents": [{"name": "Alice", "position": {"x": 9, "y": 7}}],
                "fires": [
                    {
                        "name": "AgniFire",
                        "fire_type": "Non-chemical",
                        "position": {"x": 16, "y": 17},
                        "average_intensity": "Low",
                        "flammable_ids": ["a", "b"],
                    }
                ],
                "flammables": [
                    {
                        "name": "AgniFire_Region_1",
                        "parent_fire": "AgniFire",
                        "position": {"x": 16, "y": 17},
                    }
                ],
                "reservoirs": [
                    {
                        "name": "ReservoirLibre",
                        "resource_type": "Water",
                        "position": {"x": 10, "y": 17},
                    }
                ],
                "deposits": [{"name": "DepositFacility", "position": {"x": 13, "y": 25}}],
                "persons": [
                    {
                        "name": "LostPersonThomas",
                        "position": {"x": 25, "y": 6},
                        "load": 2,
                    }
                ],
            },
        }
    )
    assert "Grid: 30 x 30" in digest
    assert "AgniFire: Non-chemical fire" in digest
    assert "AgniFire_Region_1 @ (16,17)" in digest
    assert "ReservoirLibre: Water @ (10,17)" in digest
    assert "DepositFacility @ (13,25)" in digest
    assert "LostPersonThomas @ (25,6)" in digest


def test_action_round_filter_ignores_meta_and_mcp_tools(tmp_path):
    run_dir = _build_run(
        tmp_path,
        rounds=[
            (0, ["report_observation"]),
            (1, ["map_agent__query_natural"]),
            (2, ["no_op"]),
        ],
    )
    rounds = list(iter_worker_rounds(run_dir / "workers"))
    assert [is_action_round(r) for r in rounds] == [False, False, True]


def test_worker_tool_registry_is_classified():
    """Every live worker tool must be classified (keeps the filter honest)."""
    tools_dir = Path(__file__).resolve().parent.parent / "sar_orch" / "tools" / "worker"
    names = set()
    for path in tools_dir.glob("*.py"):
        for match in re.finditer(
            r'^\s*name = "([a-z_]+)"', path.read_text(encoding="utf-8"), re.MULTILINE
        ):
            names.add(match.group(1))
    assert names  # registry found
    unclassified = names - ACTION_TOOLS - NON_ACTION_TOOLS
    assert not unclassified, f"unclassified worker tools: {sorted(unclassified)}"

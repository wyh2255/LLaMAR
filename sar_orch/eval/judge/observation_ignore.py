"""observation_ignore_rate (L3) — LLM-judged contradiction sampling.

Sample unit: one worker decision round — the uuid-named AgentLogger
``llm_request.messages`` (full input; the latest observation sits in the
message tail) paired with the following ``llm_response`` (the action
decision).  The judge answers a binary question: does the decision clearly
contradict the observation the agent had just received?

Sampling (card decision #4): candidates are action-decision rounds stratified
by ``step_index``; a run-name-derived deterministic seed shuffles within each
step and rounds are drawn round-robin across steps, so the sample spreads
uniformly over time and the same run always draws the same pairs.

Account: denominator = sampled pairs, numerator = judged contradictions,
``judge_error`` samples count in the denominator but not the numerator
(``judge_error_count`` is reported separately, plus ``judged_count``).
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any

from .artifacts import (
    build_observation_digest,
    extract_task_instruction,
    is_action_round,
    iter_worker_rounds,
    render_decision,
    render_tool_calls,
)
from .client import MAX_JUDGE_ATTEMPTS, JudgeClient, JudgeParseError, run_judge_json
from .prompts import (
    OBSERVATION_IGNORE_SYSTEM_PROMPT,
    build_observation_ignore_user_prompt,
)

METRIC_NAME = "observation_ignore"
DEFAULT_SAMPLE_SIZE = 20
#: Response statuses that mean "no decision happened" (in-flight request was
#: cancelled/aborted) — never sampled.
NON_DECISION_STATUSES = frozenset({"aborted", "cancelled", "canceled", "error"})


def _normalize_observation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalise the judge's binary verdict (raises JudgeParseError)."""
    contradiction = payload.get("contradiction")
    if isinstance(contradiction, str):
        text = contradiction.strip().lower()
        if text in ("true", "yes"):
            contradiction = True
        elif text in ("false", "no"):
            contradiction = False
    if not isinstance(contradiction, bool):
        raise JudgeParseError(
            f"contradiction must be a boolean, got {contradiction!r}"
        )
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise JudgeParseError("reasoning must be a non-empty string")
    return {"contradiction": contradiction, "reasoning": reasoning.strip()}


def _sample_seed(run_dir: Path) -> int:
    """Deterministic per-run sampling seed (stable across machines)."""
    digest = hashlib.sha256(run_dir.resolve().name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def collect_candidates(run_dir: Path) -> tuple[list[Any], int]:
    """All worker rounds + the action-decision candidate subset."""
    rounds = list(iter_worker_rounds(run_dir / "workers"))
    candidates = [
        round_
        for round_ in rounds
        if round_.messages
        and round_.tool_calls
        and round_.response_status not in NON_DECISION_STATUSES
        and is_action_round(round_)
    ]
    return candidates, len(rounds)


def sample_candidates(
    candidates: list[Any],
    *,
    sample_size: int,
    seed: int,
) -> list[Any]:
    """Stratified draw: shuffle per step, then round-robin across steps.

    Yields a sample spread as uniformly as possible over ``step_index``
    (at most one extra pair per step), deterministic for a given seed.
    """
    if sample_size <= 0:
        return []
    rng = random.Random(seed)
    by_step: dict[int, list[Any]] = {}
    for round_ in sorted(
        candidates,
        key=lambda r: (r.step, r.agent, r.file, r.request_index),
    ):
        by_step.setdefault(round_.step, []).append(round_)
    for queue in by_step.values():
        rng.shuffle(queue)
    step_order = sorted(by_step)
    picked: list[Any] = []
    while len(picked) < sample_size:
        progressed = False
        for step in step_order:
            queue = by_step[step]
            if not queue:
                continue
            picked.append(queue.pop())
            progressed = True
            if len(picked) >= sample_size:
                break
        if not progressed:
            break
    return picked


def _base_payload() -> dict[str, Any]:
    return {
        "status": "missing_input",
        "ignore_rate": None,
        "numerator": 0,
        "denominator": 0,
        "judged_count": 0,
        "judge_error_count": 0,
        "sample_size_requested": DEFAULT_SAMPLE_SIZE,
        "sample_size_effective": 0,
        "candidate_count": 0,
        "total_rounds": 0,
        "steps_covered": 0,
        "samples": [],
        "usage": None,
        "missing": [],
    }


def evaluate_observation_ignore(
    run_dir: Path,
    client: JudgeClient | None,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    sample_seed: int | None = None,
    max_attempts: int = MAX_JUDGE_ATTEMPTS,
) -> dict[str, Any]:
    """Judge ``sample_size`` sampled worker decision rounds; never raises."""
    payload = _base_payload()
    payload["sample_size_requested"] = sample_size
    workers_dir = run_dir / "workers"
    if not workers_dir.is_dir():
        payload["missing"] = ["workers/"]
        return payload
    if client is None:
        payload["status"] = "judge_unconfigured"
        return payload

    candidates, total_rounds = collect_candidates(run_dir)
    payload["candidate_count"] = len(candidates)
    payload["total_rounds"] = total_rounds
    if not candidates:
        payload["status"] = "no_candidates"
        return payload

    seed = sample_seed if sample_seed is not None else _sample_seed(run_dir)
    picked = sample_candidates(candidates, sample_size=sample_size, seed=seed)
    payload["sample_size_effective"] = len(picked)
    payload["steps_covered"] = len({round_.step for round_ in picked})

    usage_totals = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
    }
    samples: list[dict[str, Any]] = []
    for round_ in picked:
        user_prompt = build_observation_ignore_user_prompt(
            agent=round_.agent,
            step=round_.step,
            task_instruction=extract_task_instruction(round_.messages),
            observation_digest=build_observation_digest(round_.messages),
            decision_text=render_decision(round_),
        )
        outcome = run_judge_json(
            client,
            system_prompt=OBSERVATION_IGNORE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            parse=_normalize_observation_payload,
            max_attempts=max_attempts,
        )
        for key in usage_totals:
            usage_totals[key] += int(outcome.usage.get(key, 0) or 0)
        value = outcome.value
        sample: dict[str, Any] = {
            "agent": round_.agent,
            "step": round_.step,
            "task_id": round_.task_id,
            "file": round_.file,
            "request_ts": round_.request_ts,
            "decision": render_tool_calls(round_.tool_calls),
            "status": "ok" if value is not None else "judge_error",
            "contradiction": value["contradiction"] if value is not None else None,
            "reasoning": value["reasoning"] if value is not None else None,
            "judge_attempts": outcome.attempts,
            "error": outcome.error,
            "usage": dict(outcome.usage),
        }
        samples.append(sample)

    denominator = len(samples)
    numerator = sum(
        1 for sample in samples if sample["status"] == "ok" and sample["contradiction"]
    )
    judged = sum(1 for sample in samples if sample["status"] == "ok")
    payload.update(
        {
            "status": "ok",
            "ignore_rate": numerator / denominator if denominator else None,
            "numerator": numerator,
            "denominator": denominator,
            "judged_count": judged,
            "judge_error_count": denominator - judged,
            "samples": samples,
            "usage": usage_totals,
        }
    )
    return payload

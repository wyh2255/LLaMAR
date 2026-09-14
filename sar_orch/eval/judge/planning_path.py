"""planning_path (L2) — LLM-judged dispatch path flaws, deterministic 0-100 score.

Input contract (card decision #3): ``scene_config.json`` (initial layout) +
``router_interactions.csv`` (dispatch time order) + ``subtasks.csv``
(dispatch lifecycle).  The judge scores PATH quality, never mission outcome.

Scoring (card decision #7, sar-metrics v1 — planner board "方案 A"): the LLM
only *identifies* evidenced flaws (deductions); the score is computed here,
deterministically, from the pinned weight/cap table::

    score = max(0, MAX_SCORE - Σ_c min(count_c, cap_c) * weight_c)

The judge is never asked for — and any ``score`` key it still returns is
ignored by the parser — so the same deductions always map to the same score.

Output payload shape (stable, see the package docstring):

    {"status": "ok" | "judge_error" | "missing_input" | "judge_unconfigured",
     "score": int | None, "max_score": 100, "reasoning": str | None,
     "deductions": [{"category": ..., "detail": ...}],
     "score_breakdown": {"categories": {<id>: {count, capped_count, weight,
                                               points}},
                          "weighted_total": int, "floor_applied": bool},
     "judge_error_count": int, "attempts": int, "usage": {...},
     "input": {...}}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import (
    build_scene_digest,
    load_json,
    read_csv_rows,
    render_router_timeline,
    render_subtask_log,
)
from .client import (
    MAX_JUDGE_ATTEMPTS,
    JudgeClient,
    JudgeParseError,
    run_judge_json,
)
from .prompts import PLANNING_PATH_SYSTEM_PROMPT, build_planning_path_user_prompt

METRIC_NAME = "planning_path"
MAX_SCORE = 100

#: Canonical deduction ids (card decision #3); everything else is recorded
#: as "other" with the raw category preserved inside the detail text.
DEDUCTION_CATEGORIES = (
    "missing_dispatch",
    "wrong_order",
    "redundant_cancel",
    "incomplete_coverage",
    "ignored_help",
)

#: Pinned per-occurrence deductions and per-category caps (card decision #7 —
#: planner board "方案 A"; locked, do not tune per-run).
DEDUCTION_WEIGHTS: dict[str, int] = {
    "missing_dispatch": 25,
    "wrong_order": 20,
    "ignored_help": 20,
    "redundant_cancel": 15,
    "incomplete_coverage": 15,
    "other": 10,
}
DEDUCTION_CAPS: dict[str, int] = {
    "missing_dispatch": 2,
    "wrong_order": 2,
    "ignored_help": 2,
    "redundant_cancel": 2,
    "incomplete_coverage": 2,
    "other": 1,
}


_CATEGORY_ALIASES = {
    "missing_dispatch": ("missing dispatch", "missing_dispatch", "漏派", "漏派发", "遗漏派发"),
    "wrong_order": ("wrong order", "wrong_order", "out of order", "错序", "顺序错误", "顺序不当"),
    "redundant_cancel": (
        "redundant cancel",
        "redundant_cancel",
        "cancel churn",
        "冗余取消",
        "取消冗余",
        "反复取消",
    ),
    "incomplete_coverage": (
        "incomplete coverage",
        "incomplete_coverage",
        "coverage",
        "覆盖不全",
        "覆盖不足",
        "覆盖不完整",
    ),
    "ignored_help": ("ignored help", "ignored_help", "ignored_help_request", "无视求助", "忽视求助"),
}


def _normalize_category(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "other"
    text = raw.strip().lower()
    for canonical, aliases in _CATEGORY_ALIASES.items():
        for alias in aliases:
            if alias in text:
                return canonical
    return "other"


def _normalize_planning_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalise the judge's planning payload (raises JudgeParseError).

    The judge no longer returns a score; a ``score`` key (legacy habit or
    prompt leakage) is tolerated and *ignored* — the score is computed
    deterministically from the deductions by :func:`compute_score`.
    """
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise JudgeParseError("reasoning must be a non-empty string")

    raw_deductions = payload.get("deductions")
    if raw_deductions is None:
        raw_deductions = []
    if not isinstance(raw_deductions, list):
        raise JudgeParseError("deductions must be a list")
    deductions: list[dict[str, str]] = []
    for item in raw_deductions:
        if isinstance(item, str):
            detail = item.strip()
            if detail:
                deductions.append({"category": "other", "detail": detail})
            continue
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") or item.get("description") or item.get("text") or ""
        detail = str(detail).strip()
        category = _normalize_category(item.get("category"))
        if category == "other":
            raw_category = item.get("category")
            if isinstance(raw_category, str) and raw_category.strip():
                detail = f"[category={raw_category.strip()}] {detail}".strip()
        deductions.append({"category": category, "detail": detail})
    return {
        "reasoning": reasoning.strip(),
        "deductions": deductions,
    }


def compute_score(
    deductions: list[dict[str, str]],
) -> tuple[int, dict[str, Any]]:
    """Deterministically score normalised deductions (card decision #7).

    Returns ``(score, breakdown)`` where
    ``score = max(0, MAX_SCORE - Σ_c min(count_c, cap_c) * weight_c)`` and the
    breakdown records, per category, ``count`` / ``capped_count`` / ``weight``
    / ``points`` plus the ``weighted_total`` and the ``floor_applied`` flag.
    """
    counts = {category: 0 for category in DEDUCTION_WEIGHTS}
    for item in deductions:
        category = item.get("category") if isinstance(item, dict) else None
        if category not in counts:
            category = "other"
        counts[category] += 1

    categories: dict[str, dict[str, int]] = {}
    weighted_total = 0
    for category, weight in DEDUCTION_WEIGHTS.items():
        count = counts[category]
        capped_count = min(count, DEDUCTION_CAPS[category])
        points = capped_count * weight
        weighted_total += points
        categories[category] = {
            "count": count,
            "capped_count": capped_count,
            "weight": weight,
            "points": points,
        }

    raw_score = MAX_SCORE - weighted_total
    floor_applied = raw_score < 0
    breakdown = {
        "categories": categories,
        "weighted_total": weighted_total,
        "floor_applied": floor_applied,
    }
    return max(0, raw_score), breakdown


def _base_payload() -> dict[str, Any]:
    return {
        "status": "missing_input",
        "score": None,
        "max_score": MAX_SCORE,
        "reasoning": None,
        "deductions": [],
        "score_breakdown": None,
        "judge_error_count": 0,
        "attempts": 0,
        "usage": None,
        "input": {
            "scene_config": False,
            "router_rows": 0,
            "subtask_rows": 0,
            "timeline_truncated": False,
            "subtask_log_truncated": False,
            "missing": [],
        },
    }


def evaluate_planning_path(
    run_dir: Path,
    client: JudgeClient | None,
    *,
    max_attempts: int = MAX_JUDGE_ATTEMPTS,
) -> dict[str, Any]:
    """Judge the run's planning path; never raises for missing inputs."""
    payload = _base_payload()
    scene_config = load_json(run_dir / "scene_config.json")
    router_rows = read_csv_rows(run_dir / "router_interactions.csv")
    subtask_rows = read_csv_rows(run_dir / "subtasks.csv")

    missing: list[str] = []
    if scene_config is None:
        missing.append("scene_config.json")
    if router_rows is None:
        missing.append("router_interactions.csv")
    if subtask_rows is None:
        missing.append("subtasks.csv")
    if missing:
        payload["input"]["missing"] = missing
        return payload
    if client is None:
        payload["status"] = "judge_unconfigured"
        return payload

    timeline, timeline_truncated = render_router_timeline(router_rows)
    subtask_log, subtask_log_truncated = render_subtask_log(subtask_rows)
    user_prompt = build_planning_path_user_prompt(
        scene_digest=build_scene_digest(scene_config),
        dispatch_timeline=timeline,
        subtask_log=subtask_log,
        timeline_truncated=timeline_truncated,
        subtask_log_truncated=subtask_log_truncated,
    )
    payload["input"].update(
        {
            "scene_config": True,
            "router_rows": len(router_rows),
            "subtask_rows": len(subtask_rows),
            "timeline_truncated": timeline_truncated,
            "subtask_log_truncated": subtask_log_truncated,
        }
    )

    outcome = run_judge_json(
        client,
        system_prompt=PLANNING_PATH_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        parse=_normalize_planning_payload,
        max_attempts=max_attempts,
    )
    payload["attempts"] = outcome.attempts
    payload["usage"] = dict(outcome.usage)
    if not outcome.ok:
        payload["status"] = "judge_error"
        payload["judge_error_count"] = 1
        payload["reasoning"] = None
        payload["error"] = outcome.error
        return payload
    payload["status"] = "ok"
    payload["reasoning"] = outcome.value["reasoning"]
    payload["deductions"] = outcome.value["deductions"]
    score, breakdown = compute_score(payload["deductions"])
    payload["score"] = score
    payload["score_breakdown"] = breakdown
    return payload

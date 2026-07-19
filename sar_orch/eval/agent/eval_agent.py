from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from deepagents import create_deep_agent

from sar_orch.eval.dataset import EpisodeDataset
from sar_orch.eval.agent import tools as agent_tools
from sar_orch.eval.agent.subagents import (
    make_dispatch_judge,
    make_observation_judge,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def _build_model(
    model_name: str | None,
    api_base: str | None,
    api_key: str | None,
) -> BaseChatModel:
    return ChatOpenAI(
        model=model_name or "gpt-5.4",
        base_url=api_base or "https://www.packyapi.com/v1",
        api_key=api_key or "",
        temperature=0,
    )


def create_eval_agent(
    episode: EpisodeDataset,
    workspace_dir: Path,
    agent_model: str | None = None,
    judge_model: str | None = None,
    agent_api_base: str | None = None,
    agent_api_key: str | None = None,
    judge_api_base: str | None = None,
    judge_api_key: str | None = None,
    judge_sample_steps: int = 20,
) -> Any:
    agent_tools.init_agent_env(episode, workspace_dir)

    main_model = _build_model(agent_model, agent_api_base, agent_api_key)

    judge_model_instance: BaseChatModel | None = None
    if judge_model and judge_model != agent_model:
        judge_model_instance = _build_model(
            judge_model,
            judge_api_base or agent_api_base,
            judge_api_key or agent_api_key,
        )

    tools = [
        agent_tools.run_grader,
        agent_tools.run_all_graders,
        agent_tools.get_step_evidence,
        agent_tools.get_agent_trace,
        agent_tools.get_dispatch_context,
        agent_tools.get_observation_claims,
        agent_tools.save_judge_verdict,
        agent_tools.save_conclusion,
    ]

    subagents = [
        make_dispatch_judge(model=judge_model_instance),
        make_observation_judge(model=judge_model_instance),
    ]

    system_prompt = _read_prompt("system.md")
    system_prompt += f"\n\n## Configuration\njudge_sample_steps: {judge_sample_steps}"

    agent = create_deep_agent(
        model=main_model,
        tools=tools,
        subagents=subagents,
        system_prompt=system_prompt,
        debug=False,
    )

    return agent


def _detect_score_scale(data: list) -> tuple[float, str]:
    """Scan dispatch entries to detect score scale. Returns (threshold, scale_name)."""
    all_vals = []
    dimensions = [
        "full_coverage",
        "role_match",
        "map_awareness",
        "step_budget_awareness",
    ]
    for entry in data:
        for dim in dimensions:
            val = entry.get(dim)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                all_vals.append(val)
    if not all_vals:
        return (0.5, "unknown")
    mx = max(all_vals)
    if mx > 1.0:
        return (3.0, "1-5")
    return (0.5, "0-1")


def _flatten_dispatch_verdicts_list(data: list) -> list[dict]:
    """Flatten a list-format dispatch verdict like [{"step": 1, "full_coverage": "pass", ...}, ...]."""
    threshold, _ = _detect_score_scale(data)
    verdicts = []
    dimensions = [
        "full_coverage",
        "role_match",
        "map_awareness",
        "step_budget_awareness",
    ]
    for entry in data:
        step_num = entry.get("step", 0)
        if isinstance(step_num, str) and step_num.startswith("step_"):
            step_num = int(step_num.replace("step_", ""))
        flat = {
            "step": int(step_num) if step_num else 0,
            "verdicts": {},
            "reasoning": entry.get("overall_rationale", ""),
            "evidence": [],
        }
        for dim in dimensions:
            val = entry.get(dim)
            if isinstance(val, str):
                flat["verdicts"][dim] = val
            elif isinstance(val, (int, float)):
                flat["verdicts"][dim] = "pass" if val >= threshold else "fail"
        verdicts.append(flat)
    return verdicts


def _flatten_dispatch_verdicts(data: dict) -> list[dict]:
    """Flatten judge subagent dispatch output into list of per-step verdict dicts."""
    # Format F: {"step": N, "verdicts": {"dim": {"pass": bool, "reason": "..."}, ...}}
    if "step" in data and "verdicts" in data and isinstance(data["verdicts"], dict):
        vd = data["verdicts"]
        first_val = next(iter(vd.values()), None)
        if isinstance(first_val, dict) and "pass" in first_val:
            flat = {
                "step": data["step"],
                "verdicts": {},
                "reasoning": data.get("justification", "") or data.get("reasoning", ""),
                "evidence": [],
            }
            for dim_key, dim_val in vd.items():
                flat["verdicts"][dim_key] = "pass" if dim_val.get("pass") else "fail"
            return [flat]

    # Format A: {"per_step": {"1": {"full_coverage": ..., ...}, ...}}
    if "per_step" in data:
        verdicts = []
        for step_str, dims in data["per_step"].items():
            flat = {
                "step": int(step_str),
                "verdicts": {},
                "reasoning": "",
                "evidence": [],
            }
            for dim_key, dim_val in dims.items():
                if isinstance(dim_val, dict) and "score" in dim_val:
                    flat["verdicts"][dim_key] = dim_val["score"]
                elif isinstance(dim_val, (int, float)):
                    flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                elif isinstance(dim_val, str):
                    flat["verdicts"][dim_key] = dim_val
            verdicts.append(flat)
        return verdicts
    # Format B: {"verdicts": {"step_4": {"full_coverage": 1.0, ...}, ...}}
    if "verdicts" in data and isinstance(data["verdicts"], dict):
        vd = data["verdicts"]
        first_val = next(iter(vd.values()), None)
        if isinstance(first_val, dict) and any(k.startswith("step_") for k in vd):
            verdicts = []
            for step_key, dims in vd.items():
                step_num = int(step_key.replace("step_", ""))
                flat = {
                    "step": step_num,
                    "verdicts": {},
                    "reasoning": dims.get("justification", "")
                    or dims.get("rationale", ""),
                    "evidence": [],
                }
                for dim_key, dim_val in dims.items():
                    if dim_key in ("justification", "rationale"):
                        continue
                    if isinstance(dim_val, dict) and "pass" in dim_val:
                        flat["verdicts"][dim_key] = (
                            "pass" if dim_val["pass"] else "fail"
                        )
                    elif isinstance(dim_val, (int, float)):
                        flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                    elif isinstance(dim_val, str):
                        flat["verdicts"][dim_key] = dim_val
                verdicts.append(flat)
            return verdicts
    # Format C: {"verdicts": [{"step": N, "full_coverage": 1.0, ...}, ...]}
    if "verdicts" in data and isinstance(data["verdicts"], list):
        verdicts = []
        for entry in data["verdicts"]:
            step_num = entry.get("step", 0)
            if isinstance(step_num, str) and step_num.startswith("step_"):
                step_num = int(step_num.replace("step_", ""))
            flat = {
                "step": int(step_num) if step_num else 0,
                "verdicts": {},
                "reasoning": entry.get("rationale", "")
                or entry.get("justification", ""),
                "evidence": [],
            }
            for dim_key, dim_val in entry.items():
                if dim_key in ("step", "rationale", "justification", "id"):
                    continue
                if isinstance(dim_val, dict) and "pass" in dim_val:
                    flat["verdicts"][dim_key] = "pass" if dim_val["pass"] else "fail"
                elif isinstance(dim_val, (int, float)):
                    flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                elif isinstance(dim_val, str):
                    flat["verdicts"][dim_key] = dim_val
            verdicts.append(flat)
        return verdicts
    # Format D: top-level step_X keys: {"step_1": {...}, "step_5": {...}, ...}
    step_keys = [k for k in data if k.startswith("step_")]
    if len(step_keys) > 1:
        verdicts = []
        for step_key in step_keys:
            step_num = int(step_key.replace("step_", ""))
            dims = data[step_key]
            flat = {
                "step": step_num,
                "verdicts": {},
                "reasoning": dims.get("rationale", "") or dims.get("justification", ""),
                "evidence": [],
            }
            for dim_key, dim_val in dims.items():
                if dim_key in (
                    "rationale",
                    "justification",
                    "overall",
                    "issues",
                    "summary",
                ):
                    continue
                if isinstance(dim_val, dict) and "pass" in dim_val:
                    flat["verdicts"][dim_key] = "pass" if dim_val["pass"] else "fail"
                elif isinstance(dim_val, dict) and "score" in dim_val:
                    score = dim_val["score"]
                    flat["verdicts"][dim_key] = (
                        "pass"
                        if isinstance(score, (int, float)) and score >= 0.5
                        else "fail"
                    )
                elif isinstance(dim_val, (int, float)):
                    flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                elif isinstance(dim_val, str):
                    flat["verdicts"][dim_key] = dim_val
            verdicts.append(flat)
        return verdicts
    # Format D2: top-level numeric keys: {"1": {...}, "5": {...}, ...}
    num_keys = [k for k in data if isinstance(k, str) and k.isdigit()]
    if len(num_keys) > 1:
        verdicts = []
        for step_key in num_keys:
            step_num = int(step_key)
            dims = data[step_key]
            flat = {
                "step": step_num,
                "verdicts": {},
                "reasoning": dims.get("justification", "") or dims.get("rationale", ""),
                "evidence": [],
            }
            for dim_key, dim_val in dims.items():
                if dim_key in (
                    "justification",
                    "rationale",
                    "overall",
                    "issues",
                    "summary",
                ):
                    continue
                if isinstance(dim_val, dict) and "pass" in dim_val:
                    flat["verdicts"][dim_key] = "pass" if dim_val["pass"] else "fail"
                elif isinstance(dim_val, dict) and "score" in dim_val:
                    score = dim_val["score"]
                    flat["verdicts"][dim_key] = (
                        "pass"
                        if isinstance(score, (int, float)) and score >= 0.5
                        else "fail"
                    )
                elif isinstance(dim_val, (int, float)):
                    flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                elif isinstance(dim_val, str):
                    flat["verdicts"][dim_key] = dim_val
            verdicts.append(flat)
        return verdicts
    # Format G: {"dispatch_evaluations": {"step_1": {"full_coverage": true, ...}, ...}}
    for wrapper_key in (
        "dispatch_evaluations",
        "dispatch_verdicts",
        "step_evaluations",
    ):
        if wrapper_key in data and isinstance(data[wrapper_key], dict):
            source = data[wrapper_key]
            verdicts = []
            for step_key, dims in source.items():
                if not step_key.startswith("step_"):
                    continue
                step_num = int(step_key.replace("step_", ""))
                flat = {
                    "step": step_num,
                    "verdicts": {},
                    "reasoning": dims.get("reasoning", "")
                    or dims.get("justification", "")
                    or dims.get("rationale", ""),
                    "evidence": [],
                }
                for dim_key, dim_val in dims.items():
                    if dim_key in (
                        "reasoning",
                        "justification",
                        "rationale",
                        "overall",
                        "issues",
                        "summary",
                    ):
                        continue
                    if isinstance(dim_val, bool):
                        flat["verdicts"][dim_key] = "pass" if dim_val else "fail"
                    elif isinstance(dim_val, dict) and "pass" in dim_val:
                        flat["verdicts"][dim_key] = (
                            "pass" if dim_val["pass"] else "fail"
                        )
                    elif isinstance(dim_val, dict) and "score" in dim_val:
                        score = dim_val["score"]
                        flat["verdicts"][dim_key] = (
                            "pass"
                            if isinstance(score, (int, float)) and score >= 0.5
                            else "fail"
                        )
                    elif isinstance(dim_val, (int, float)):
                        flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                    elif isinstance(dim_val, str):
                        flat["verdicts"][dim_key] = dim_val
                verdicts.append(flat)
            if verdicts:
                return verdicts

    # Format E: {"verdicts": {"full_coverage": "pass", ...}, "step": 1}
    if "verdicts" in data and "step" in data:
        return [data]
    if "steps" in data and isinstance(data["steps"], dict):
        steps_dict = data["steps"]
        verdicts = []
        for step_key, dims in steps_dict.items():
            step_num = int(step_key) if step_key.isdigit() else 0
            flat = {
                "step": step_num,
                "verdicts": {},
                "reasoning": dims.get("justification", "") or dims.get("rationale", ""),
                "evidence": [],
            }
            for dim_key, dim_val in dims.items():
                if dim_key in (
                    "justification",
                    "rationale",
                    "overall",
                    "issues",
                    "summary",
                ):
                    continue
                if isinstance(dim_val, dict) and "pass" in dim_val:
                    flat["verdicts"][dim_key] = "pass" if dim_val["pass"] else "fail"
                elif isinstance(dim_val, dict) and "score" in dim_val:
                    score = dim_val["score"]
                    flat["verdicts"][dim_key] = (
                        "pass"
                        if isinstance(score, (int, float)) and score >= 0.5
                        else "fail"
                    )
                elif isinstance(dim_val, (int, float)):
                    flat["verdicts"][dim_key] = "pass" if dim_val >= 0.5 else "fail"
                elif isinstance(dim_val, str):
                    flat["verdicts"][dim_key] = dim_val
            verdicts.append(flat)
        return verdicts
    return [data]


def _flatten_observation_verdicts(data: dict) -> list[dict]:
    """Flatten judge subagent observation output into list of per-claim-set dicts."""
    # Format F (primary): {"claims": [{"agent": "...", "step": N, "claim": "...", "supported": bool, "evidence": "..."}], "summary": "..."}
    if (
        "claims" in data
        and isinstance(data["claims"], list)
        and len(data["claims"]) > 0
    ):
        claims = data["claims"]
        total = len(claims)
        hallucinated = sum(1 for c in claims if not c.get("supported", True))
        detail = []
        for c in claims:
            detail.append(
                {
                    "agent": c.get("agent", "?"),
                    "step": c.get("step", 0),
                    "claim": c.get("claim", ""),
                    "supported": c.get("supported", True),
                    "evidence": c.get("evidence", ""),
                }
            )
        return [
            {
                "total_claims": total,
                "hallucinated_claims": hallucinated,
                "claims_detail": detail,
                "verdict": "fail" if hallucinated > 0 else "pass",
            }
        ]

    # Format A: {"per_claim_set": {"Alice-1": {"verdict": "pass", ...}, ...}}
    if "per_claim_set" in data:
        verdicts = []
        for key, val in data["per_claim_set"].items():
            agent_step = val.get("identifier", key)
            verdicts.append(
                {
                    "agent": agent_step.split("-")[0]
                    if "-" in str(agent_step)
                    else str(agent_step),
                    "step": int(agent_step.split("-")[1])
                    if "-" in str(agent_step)
                    else 0,
                    "total_claims": val.get("supported_count", 0)
                    + val.get("hallucination_count", 0),
                    "hallucinated_claims": val.get("hallucination_count", 0),
                    "verdict": val.get("verdict", "Unknown"),
                    "claims_detail": val.get("unsupported_claims", []),
                }
            )
        return verdicts
    # Format B: {"verdicts": {"claim_1_Alice_step1": {"is_hallucination": true, ...}, ...}}
    if "verdicts" in data and isinstance(data["verdicts"], dict):
        vd = data["verdicts"]
        first_val = next(iter(vd.values()), None)
        if isinstance(first_val, dict) and any(k.startswith("claim_") for k in vd):
            hallucinated = 0
            total = 0
            detail = []
            for claim_key, cv in vd.items():
                total += 1
                if cv.get("is_hallucination"):
                    hallucinated += 1
                detail.append(
                    {
                        "claim": claim_key,
                        "supported": not cv.get("is_hallucination", False),
                        "evidence": cv.get("explanation", ""),
                    }
                )
            return [
                {
                    "total_claims": total,
                    "hallucinated_claims": hallucinated,
                    "claims_detail": detail,
                    "verdict": "fail" if hallucinated > 0 else "pass",
                }
            ]
    # Format C: {"verdicts": [{"agent": "Alice", "step": 1, "claims": [...], ...}, ...], ...}
    # Format C2: {"verdicts": [{"id": "...", "agent": "Alice", "step": 1, "claim": "...", "verdict": "MISATTRIBUTED"/"CORRECT"}, ...]}
    if "verdicts" in data and isinstance(data["verdicts"], list):
        total_claims = 0
        hallucinated = 0
        detail = []
        for entry in data["verdicts"]:
            if "claims" in entry:
                for c in entry["claims"]:
                    total_claims += 1
                    is_hall = (
                        c.get("is_hallucination", False) or c.get("supported") is False
                    )
                    if is_hall:
                        hallucinated += 1
                    detail.append(
                        {
                            "agent": entry.get("agent", "?"),
                            "step": entry.get("step", 0),
                            "claim": c.get("claim", ""),
                            "supported": not is_hall,
                            "evidence": c.get("explanation", c.get("evidence", "")),
                        }
                    )
            elif "claim" in entry:
                total_claims += 1
                is_hall = entry.get("verdict", "CORRECT") in (
                    "MISATTRIBUTED",
                    "HALLUCINATION",
                    "fail",
                    "False",
                )
                if is_hall:
                    hallucinated += 1
                detail.append(
                    {
                        "agent": entry.get("agent", "?"),
                        "step": entry.get("step", 0),
                        "claim": entry.get("claim", ""),
                        "supported": not is_hall,
                        "evidence": entry.get(
                            "ground_truth", entry.get("explanation", "")
                        ),
                    }
                )
        if total_claims > 0:
            return [
                {
                    "total_claims": total_claims,
                    "hallucinated_claims": hallucinated,
                    "claims_detail": detail,
                    "verdict": "fail" if hallucinated > 0 else "pass",
                }
            ]
        return [data]
    # Format D: top-level claim_ keys: {"claim_1_alice_deposit_charlie": {"hallucination": true, "severity": "...", "explanation": "..."}, ...}
    claim_keys = [k for k in data if k.startswith("claim_")]
    if len(claim_keys) > 1:
        total = 0
        hallucinated = 0
        detail = []
        for ck in claim_keys:
            cv = data[ck]
            total += 1
            is_hall = cv.get("hallucination", False) or cv.get(
                "is_hallucination", False
            )
            if is_hall:
                hallucinated += 1
            detail.append(
                {
                    "claim": ck,
                    "supported": not is_hall,
                    "evidence": cv.get("explanation", ""),
                }
            )
        return [
            {
                "total_claims": total,
                "hallucinated_claims": hallucinated,
                "claims_detail": detail,
                "verdict": "fail" if hallucinated > 0 else "pass",
            }
        ]
    # Format E: {"hallucinations_found": [...], "total_hallucinations": N, ...}
    if "hallucinations_found" in data or "total_hallucinations" in data:
        total = data.get(
            "total_hallucinations", len(data.get("hallucinations_found", []))
        )
        total += (
            sum(1 for _ in data.get("hallucinations_found", []))
            if "total_hallucinations" not in data
            else 0
        )
        hallucinated = data.get(
            "total_hallucinations", len(data.get("hallucinations_found", []))
        )
        detail = []
        for h in data.get("hallucinations_found", []):
            detail.append(
                {
                    "claim": h.get("claim", h.get("description", "")),
                    "supported": False,
                    "evidence": h.get("explanation", ""),
                }
            )
        return [
            {
                "total_claims": total,
                "hallucinated_claims": hallucinated,
                "claims_detail": detail,
                "verdict": "fail" if hallucinated > 0 else "pass",
            }
        ]
    if "agent" in data or "step" in data:
        return [data]
    if "checks" in data:
        return data["checks"]
    return [data]


def collect_judge_results(
    workspace_dir: Path,
    judge_model_name: str | None = None,
    subject_model_name: str | None = None,
) -> dict[str, Any]:
    judge_dir = workspace_dir / "judge_results"
    if not judge_dir.exists():
        return {}

    dispatch_verdicts = []
    for f in sorted(judge_dir.glob("*dispatch*")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            dispatch_verdicts.extend(_flatten_dispatch_verdicts_list(data))
        elif isinstance(data, dict):
            dispatch_verdicts.extend(_flatten_dispatch_verdicts(data))

    observation_verdicts = []
    for f in sorted(judge_dir.glob("*observ*")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            observation_verdicts.extend(data)
        elif isinstance(data, dict):
            observation_verdicts.extend(_flatten_observation_verdicts(data))

    same_model_warning = (
        judge_model_name is not None
        and subject_model_name is not None
        and judge_model_name == subject_model_name
    )

    dispatch_pass_rate = None
    if dispatch_verdicts:
        dimensions = [
            "full_coverage",
            "role_match",
            "map_awareness",
            "step_budget_awareness",
        ]
        total = len(dispatch_verdicts) * len(dimensions)
        passes = sum(
            1
            for v in dispatch_verdicts
            for dim in dimensions
            if v.get("verdicts", {}).get(dim) == "pass"
        )
        dispatch_pass_rate = passes / total if total > 0 else 0.0

    hallucination_rate = None
    if observation_verdicts:
        total_claims = sum(v.get("total_claims", 0) for v in observation_verdicts)
        hallucinated = sum(
            v.get("hallucinated_claims", 0) for v in observation_verdicts
        )
        hallucination_rate = hallucinated / total_claims if total_claims > 0 else 0.0

    sampled_claims = sum(v.get("total_claims", 0) or 0 for v in observation_verdicts)

    all_claims = []
    for v in observation_verdicts:
        for c in v.get("claims_detail", []):
            all_claims.append(c)

    return {
        "dispatch": {
            "pass_rate": dispatch_pass_rate,
            "sampled_steps": len(dispatch_verdicts),
            "verdicts": dispatch_verdicts,
        },
        "observation": {
            "hallucination_rate": hallucination_rate,
            "sampled_claims": sampled_claims,
            "claims": all_claims,
            "verdicts": observation_verdicts,
        },
        "judge_model": judge_model_name,
        "same_model_warning": same_model_warning,
    }


def read_conclusion(workspace_dir: Path) -> str:
    path = workspace_dir / "conclusion.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def get_agent_messages(result: dict[str, Any]) -> list[dict[str, Any]]:
    messages = result.get("messages", [])
    summary = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        if role == "ai" and content:
            summary.append({"role": "assistant", "content": content[:200]})
        elif role == "tool":
            summary.append(
                {
                    "role": "tool",
                    "name": getattr(msg, "name", ""),
                    "content_preview": str(content)[:200],
                }
            )
    return summary

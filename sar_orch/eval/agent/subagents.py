from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from sar_orch.eval.agent.tools import get_dispatch_context, get_observation_claims

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def _judge_spec(
    name: str,
    description: str,
    prompt_name: str,
    tools_list: list,
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": name,
        "description": description,
        "system_prompt": _read_prompt(prompt_name),
        "tools": tools_list,
    }
    if model is not None:
        spec["model"] = model
    return spec


def make_dispatch_judge(
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    return _judge_spec(
        name="dispatch_judge",
        description=(
            "Evaluate coordinator dispatch quality for specific steps. "
            "Pass the step numbers you want evaluated. Each step is judged "
            "on 4 dimensions: full_coverage, role_match, map_awareness, step_budget_awareness."
        ),
        prompt_name="dispatch_judge.md",
        tools_list=[get_dispatch_context],
        model=model,
    )


def make_observation_judge(
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    return _judge_spec(
        name="observation_judge",
        description=(
            "Detect hallucinated observation claims in worker agent report_observation outputs. "
            "Pass agent name and step number for each claim to check."
        ),
        prompt_name="observation_judge.md",
        tools_list=[get_observation_claims],
        model=model,
    )

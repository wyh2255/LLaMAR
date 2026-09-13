"""Tests for GetSkillTool — unknown-skill failures must use the static
allowlisted ``skill_not_found`` framework code (never a raw free-text error)."""

from __future__ import annotations

import pytest

from Agent.error_taxonomy import (
    FRAMEWORK_ERROR_CODES,
    UNCLASSIFIED_TOOL_ERROR,
    classify_error,
    error_code_for_result,
)

SKILL_NOT_FOUND = "skill_not_found"


class _StubSkill:
    """Minimal Skill-like object for the success path."""

    def __init__(self, name: str, description: str = "d", content: str = "c"):
        self.name = name
        self.description = description
        self.content = content
        self.skill_path = None

    def to_prompt(self) -> str:
        return f"# Skill: {self.name}\n\n{self.content}"


class _StubLoader:
    """SkillLoader subset used by GetSkillTool."""

    def __init__(self, known: dict[str, _StubSkill] | None = None):
        self._known = dict(known or {})

    def get_skill(self, name: str):
        return self._known.get(name)

    def list_skills(self) -> list[str]:
        return list(self._known)


def _router_tool(loader=None):
    from Agent.router_agent.tools.skill_tool import GetSkillTool

    return GetSkillTool(loader or _StubLoader())


def _worker_tool(loader=None):
    from Agent.worker_agent.tools.skill_tool import GetSkillTool

    return GetSkillTool(loader or _StubLoader())


@pytest.mark.parametrize(
    "factory",
    [_router_tool, _worker_tool],
    ids=["router", "worker"],
)
def test_skill_not_found_is_allowlisted(factory):
    assert SKILL_NOT_FOUND in FRAMEWORK_ERROR_CODES
    assert classify_error(SKILL_NOT_FOUND) == SKILL_NOT_FOUND
    assert classify_error(f"{SKILL_NOT_FOUND}: detail") == SKILL_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [_router_tool, _worker_tool],
    ids=["router", "worker"],
)
async def test_get_skill_unknown_returns_allowlisted_code(factory):
    """get_skill for an unknown skill must surface ``skill_not_found`` (static
    allowlisted code) with the descriptive message in content, never a raw
    free-text error string that collapses to unclassified_tool_error."""
    loader = _StubLoader(known={"navigation": _StubSkill("navigation")})
    result = await factory(loader).execute(skill_name="exploration")

    assert result.success is False
    assert result.error == SKILL_NOT_FOUND
    assert result.error in FRAMEWORK_ERROR_CODES
    assert error_code_for_result(result) == SKILL_NOT_FOUND
    # Detail lives in content, listing the available skills for self-correction.
    assert "exploration" in result.content
    assert "navigation" in result.content
    assert classify_error(result.error) != UNCLASSIFIED_TOOL_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [_router_tool, _worker_tool],
    ids=["router", "worker"],
)
async def test_get_skill_known_returns_content(factory):
    loader = _StubLoader(known={"navigation": _StubSkill("navigation")})
    result = await factory(loader).execute(skill_name="navigation")
    assert result.success is True
    assert result.error is None
    assert "# Skill: navigation" in result.content


@pytest.mark.asyncio
async def test_router_and_worker_share_same_protocol():
    """Both agent copies return identical structured outcomes for the same call."""
    loader = _StubLoader()
    router_result = await _router_tool(loader).execute(skill_name="ghost")
    worker_result = await _worker_tool(loader).execute(skill_name="ghost")
    assert router_result.success == worker_result.success is False
    assert router_result.error == worker_result.error == SKILL_NOT_FOUND
    assert router_result.content == worker_result.content

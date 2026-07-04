"""LangRouterAgent — RouterAgent 子类，仅覆盖 _build_agent() 以返回 LangGraph ReActAgent。

复用父类的 route_dag/plan_next/push_task/_parse_dag_result 与全部 A2A 传输逻辑；
仅把 LLMClient→build_llm、Agent→ReActAgent。DAG/agentic 两种编排模式自动生效
（_execute_agentic 经 self._router._build_agent 取 agent）。
"""

from __future__ import annotations

import os

from a2a.coordinator.router import RouterAgent
from a2a.builtin_tools.query_workers import QueryWorkersTool
from Agent.router_agent.tools.skill_loader import SkillLoader
from Agent.router_agent.tools.skill_tool import GetSkillTool

from ..core_agent import ReActAgent
from ..llm import build_llm


class LangRouterAgent(RouterAgent):
    """RouterAgent with LangGraph ReActAgent internals (drop-in override)."""

    def _build_agent(
        self,
        extra_tools: list | None = None,
        system_prompt_override: str | None = None,
    ) -> ReActAgent:
        """构建 LangGraph ReActAgent 实例（替代 RouterAgent._build_agent）。"""
        api_key = os.environ.get(self._api_key_env, "")
        llm = build_llm(
            provider=self._provider,
            api_base=self._api_base,
            model=self._model,
            api_key=api_key,
            temperature=self._temperature,
        )

        # 内置 query_workers + 自定义 + 注入 extra_tools（与父类一致）
        tools: list = [QueryWorkersTool(self._registry)]
        tools.extend(self._custom_tools)
        tools.extend(self._extra_tools)
        if extra_tools:
            tools.extend(extra_tools)

        # system prompt + skill metadata（复用父类加载逻辑）
        system_prompt = system_prompt_override or self._system_prompt
        skills_dir = self._skills_dir or self._discover_skills_dir()
        if skills_dir:
            try:
                skill_loader = SkillLoader(skills_dir=str(skills_dir))
                skill_loader.discover_skills()
                metadata_prompt = skill_loader.get_skills_metadata_prompt()
                if metadata_prompt:
                    system_prompt = system_prompt + "\n\n" + metadata_prompt
                tools.append(GetSkillTool(skill_loader))
            except Exception:
                pass  # 父类用 logger.warning；此处静默以保持适配器纯净

        return ReActAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            log_dir=self._log_dir,
        )

"""Verifier Agent — 审查 Worker 输出，形成闭环反馈。

Verifier 是一个 Mini-Agent，审查 Worker 输出是否符合要求。
它可以自己决定是否调用工具（如查询 Worker 信息），
只有需要额外信息时才调 tool。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from Agent.router_agent.agent import Agent
from Agent.router_agent.llm import LLMClient
from Agent.router_agent.schema import LLMProvider, SamplingParams
from Agent.sandbox import wrap_tools_with_sandbox

from a2a.coordinator.agent_registry import AgentRegistry
from a2a.builtin_tools.query_workers import QueryWorkersTool

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================


@dataclass
class VerificationReport:
    """结构化验证报告。"""

    task_id: str
    passed: bool = False  # True = 输出满足要求
    confidence: float = 0.0  # 0.0 ~ 1.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    summary: str = ""


# ============================================================
# Verifier System Prompt
# ============================================================

_VERIFIER_PROMPT = """You are a verification agent. Your job is to review a worker's output against its assigned task and the original user request.

You MAY use tools if needed to inspect the output more carefully, but only call tools when you cannot make a judgment from the provided text alone.

Available tools:
- query_workers: Check worker capabilities (useful if you suspect a task was assigned to the wrong worker)

Workflow:
1. Read the original user request, the subtask prompt, and the worker's output
2. Determine if the output satisfies the subtask requirements
3. Check if the output aligns with the original user's broader intent
4. Identify any specific issues or gaps
5. Suggest whether the task needs to be re-done, re-routed, or is acceptable

Output ONLY a JSON object (no markdown, no other text):
{
  "passed": true/false,
  "confidence": 0.0-1.0,
  "issues": ["list of specific, actionable problems"],
  "suggestions": ["list of suggestions for improvement or re-routing"],
  "summary": "One-line verdict summarizing the assessment"
}

Rules:
- Be specific about issues — avoid vague feedback like "could be better"
- confidence reflects how certain you are about the verdict (0.0 = guessing, 1.0 = absolutely sure)
- If the output clearly satisfies the prompt, pass it even if it's not perfect
- If you cannot determine without additional information, use a tool
- Only output JSON, no other text
"""


# ============================================================
# VerifierAgent
# ============================================================


class VerifierAgent:
    """
    审查 Worker 输出的 Mini-Agent。

    每次 verify() 调用创建独立 Agent 实例，确保并发安全。
    """

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        model: str = "claude-sonnet-4-6",
        max_steps: int = 10,
        temperature: float = 0.3,  # 验证任务用较低温度，更 deterministic
        seed: int | None = None,
        provider: str = "anthropic",
        api_base: str = "https://api.anthropic.com",
        api_key_env: str = "ANTHROPIC_API_KEY",
        workspace_dir: str = "./workspace/verifier",
        log_dir: Path | None = None,
        sandbox_policy=None,
    ) -> None:
        self._registry = registry or AgentRegistry()
        self._model = model
        self._max_steps = max_steps
        self._temperature = temperature
        self._seed = seed
        self._provider = provider
        self._api_base = api_base
        self._api_key_env = api_key_env
        self._workspace_dir = Path(workspace_dir)
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = log_dir
        self._sandbox_policy = sandbox_policy

    async def verify(
        self,
        original_request: str,
        subtask_prompt: str,
        worker_output: str,
        task_id: str,
    ) -> VerificationReport:
        """
        验证 Worker 的一次输出。

        Args:
            original_request: 原始用户请求
            subtask_prompt: 分配给 Worker 的子任务 prompt
            worker_output: Worker 返回的文本输出
            task_id: 子任务 ID

        Returns:
            VerificationReport 包含通过/失败判定和建议
        """
        # 截断过长输出
        truncated = worker_output
        if len(worker_output) > 8000:
            truncated = worker_output[:8000] + "\n...[output truncated]"

        verification_request = f"""## Original User Request
{original_request}

## Subtask Assigned to Worker ({task_id})
{subtask_prompt}

## Worker Output
{truncated}

Please verify this output and produce a structured report."""

        agent = self._build_agent()
        agent.add_user_message(verification_request)
        result = await agent.run(
            task_id=f"{task_id or 'subtask'}-verify",
            context_id="",
        )
        return self._parse_report(
            result.content if hasattr(result, "content") else str(result), task_id
        )

    def _build_agent(self) -> Agent:
        """构建新的 Agent 实例。"""
        provider_map = {
            "anthropic": LLMProvider.ANTHROPIC,
            "openai": LLMProvider.OPENAI,
        }
        provider = provider_map.get(self._provider, LLMProvider.ANTHROPIC)

        api_key = os.environ.get(self._api_key_env, "")
        llm_client = LLMClient(
            api_key=api_key,
            provider=provider,
            api_base=self._api_base,
            model=self._model,
            # First read of self._temperature -- see the note in router.py.
            sampling=SamplingParams(
                temperature=self._temperature, seed=self._seed
            ),
        )

        tools: list = [
            QueryWorkersTool(self._registry),
        ]
        tools = wrap_tools_with_sandbox(tools, self._sandbox_policy)

        return Agent(
            llm_client=llm_client,
            system_prompt=_VERIFIER_PROMPT,
            tools=tools,
            max_steps=self._max_steps,
            workspace_dir=str(self._workspace_dir),
            log_dir=self._log_dir,
        )

    @staticmethod
    def _parse_report(response: str, task_id: str) -> VerificationReport:
        """解析 Verifier Agent 的 JSON 输出。"""
        cleaned = response.strip()
        # 去除可能的 markdown 围栏
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            cleaned = "\n".join(lines)

        try:
            data = json.loads(cleaned)
            return VerificationReport(
                task_id=task_id,
                passed=data.get("passed", False),
                confidence=float(data.get("confidence", 0.5)),
                issues=data.get("issues", []),
                suggestions=data.get("suggestions", []),
                summary=data.get("summary", "No summary provided"),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(
                "Failed to parse verifier response as JSON: %s... (%s)",
                response[:200],
                e,
            )
            # 解析失败时默认 fail-safe: 标记为通过但有警告
            return VerificationReport(
                task_id=task_id,
                passed=True,
                confidence=0.3,
                issues=[f"Verifier parse error: {str(e)}"],
                suggestions=["Manual review recommended due to verifier error"],
                summary=f"Parse error — auto-passed with low confidence: {response[:100]}...",
            )

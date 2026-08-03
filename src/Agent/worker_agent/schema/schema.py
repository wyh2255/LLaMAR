from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel


class LLMProvider(str, Enum):
    """LLM provider types."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True)
class SamplingParams:
    """采样参数容器（temperature / top_p / seed）。

    为什么需要一个显式容器而不是散开的形参：这条链路有 6 段
    （sar_orch → a2a → AgentBuildOptions → LLMClient → LLMClientBase →
    请求体 params），散开传参时任何一段漏掉一个字段都会静默丢值 ——
    E-1 就是这么来的（`self._temperature` 三处赋值零读取）。装成一个对象
    后，"漏传"会退化成"整个对象是 None"，是可断言的显式状态。

    `None` 表示**不注入该字段**，让 provider 用自己的默认值。这与
    "注入 0.0" 语义不同，不能合并 —— 显式 0.0 是确定性采样，
    不注入则取决于 gateway。

    frozen=True：采样参数在一次 run 内必须恒定。可变的话，一个
    step_callback 就能悄悄改掉半程温度，而方差基线完全看不出来。
    """

    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None

    def as_request_fields(self, provider: "LLMProvider") -> dict[str, Any]:
        """按 provider 生成可直接塞进请求体的字段（仅非 None 项）。

        Anthropic 的 Messages API **不支持** `seed`，传了会 400。故按
        provider 过滤，而不是无条件展开 —— 这样上层可以统一持有同一个
        SamplingParams，无需为不同 provider 各准备一份。
        """
        fields: dict[str, Any] = {}
        if self.temperature is not None:
            fields["temperature"] = self.temperature
        if self.top_p is not None:
            fields["top_p"] = self.top_p
        if self.seed is not None and provider == LLMProvider.OPENAI:
            fields["seed"] = self.seed
        return fields


class FunctionCall(BaseModel):
    """Function call details."""

    name: str
    arguments: dict[str, Any]  # Function arguments as dict


class ToolCall(BaseModel):
    """Tool call structure."""

    id: str
    type: str  # "function"
    function: FunctionCall


class Message(BaseModel):
    """Chat message."""

    role: str  # "system", "user", "assistant", "tool"
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role


class TokenUsage(BaseModel):
    """Token usage statistics from LLM API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


class LLMResponse(BaseModel):
    """LLM response."""

    content: str
    thinking: str | None = None  # Extended thinking blocks
    tool_calls: list[ToolCall] | None = None
    finish_reason: str
    usage: TokenUsage | None = None  # Token usage from API response


@dataclass
class RunResult:
    """Result returned by Agent.run()."""

    content: str = ""
    success: bool | None = None
    steps_used: int = 0
    task_description: str = ""
    need_input: bool = False
    # True when an explicit terminal tool (e.g. finish_task) completed the run.
    # Distinct from success: mission may fail (success=False) while still being
    # a normal orchestration completion (task_complete=True), not a framework error.
    task_complete: bool = False

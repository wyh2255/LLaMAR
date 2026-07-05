"""Conversion between legacy Message schema and LangChain BaseMessage, plus token estimation and usage conversion."""

from __future__ import annotations

from typing import Any

from Agent.router_agent.schema import FunctionCall, Message, TokenUsage, ToolCall
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


def to_langchain(messages: list[Message]) -> list[BaseMessage]:
    """Convert legacy Message list to LangChain BaseMessage list."""
    result: list[BaseMessage] = []
    for msg in messages:
        content: str = (
            str(msg.content) if isinstance(msg.content, list) else msg.content
        )
        if msg.role == "system":
            result.append(SystemMessage(content=content))
        elif msg.role == "user":
            result.append(HumanMessage(content=content))
        elif msg.role == "assistant":
            tool_calls = None
            if msg.tool_calls:
                tool_calls = [
                    {
                        "name": tc.function.name,
                        "args": tc.function.arguments,
                        "id": tc.id,
                        "type": "tool_call",
                    }
                    for tc in msg.tool_calls
                ]
            kwargs: dict[str, Any] = {}
            if msg.thinking:
                kwargs["additional_kwargs"] = {"reasoning_content": msg.thinking}
            ai = AIMessage(content=content or "", tool_calls=tool_calls, **kwargs)
            result.append(ai)
        elif msg.role == "tool":
            result.append(
                ToolMessage(
                    content=content,
                    tool_call_id=msg.tool_call_id or "",
                    name=msg.name or "",
                )
            )
        else:
            result.append(HumanMessage(content=content))
    return result


def from_langchain(messages: list[BaseMessage]) -> list[Message]:
    """Convert LangChain BaseMessage list to legacy Message list."""
    result: list[Message] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append(Message(role="system", content=str(msg.content)))
        elif isinstance(msg, HumanMessage):
            result.append(Message(role="user", content=str(msg.content)))
        elif isinstance(msg, AIMessage):
            content = str(msg.content)
            thinking = (
                msg.additional_kwargs.get("reasoning_content")
                if hasattr(msg, "additional_kwargs")
                else None
            )
            tool_calls = None
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tool_calls = [
                    ToolCall(
                        id=tc.get("id", ""),
                        type="function",
                        function=FunctionCall(
                            name=tc.get("name", ""), arguments=tc.get("args", {})
                        ),
                    )
                    for tc in msg.tool_calls
                ]
            result.append(
                Message(
                    role="assistant",
                    content=content,
                    thinking=thinking,
                    tool_calls=tool_calls,
                )
            )
        elif isinstance(msg, ToolMessage):
            result.append(
                Message(
                    role="tool",
                    content=str(msg.content),
                    tool_call_id=getattr(msg, "tool_call_id", ""),
                    name=getattr(msg, "name", ""),
                )
            )
        else:
            result.append(
                Message(
                    role=getattr(msg, "type", "user"),
                    content=str(getattr(msg, "content", "")),
                )
            )
    return result


def estimate_tokens(messages: list) -> int:
    """Estimate total tokens across messages (legacy Message or LangChain BaseMessage)."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        use_tiktoken = True
    except Exception:
        use_tiktoken = False

    total = 0
    for msg in messages:
        # Determine if legacy or langchain by attribute presence
        is_lc = not hasattr(msg, "role")

        content_str = ""
        if is_lc:
            raw = getattr(msg, "content", "")
            content_str = str(raw) if not isinstance(raw, str) else raw
        else:
            raw = msg.content
            content_str = str(raw) if isinstance(raw, list) else raw

        extra_str = ""
        if is_lc:
            if hasattr(msg, "additional_kwargs") and isinstance(
                msg.additional_kwargs, dict
            ):
                rc = msg.additional_kwargs.get("reasoning_content")
                if rc:
                    extra_str += str(rc)
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                extra_str += str(msg.tool_calls)
        else:
            if msg.thinking:
                extra_str += msg.thinking
            if msg.tool_calls:
                extra_str += str(msg.tool_calls)

        combined = content_str + extra_str
        if use_tiktoken:
            total += len(enc.encode(combined))
        else:
            total += int(len(combined) / 2.5)
        total += 4  # overhead per message

    return total


def to_token_usage(usage_metadata: dict | None) -> TokenUsage | None:
    """Convert LangChain AIMessage.usage_metadata dict to legacy TokenUsage."""
    if usage_metadata is None:
        return None
    return TokenUsage(
        prompt_tokens=usage_metadata.get("input_tokens", 0),
        completion_tokens=usage_metadata.get("output_tokens", 0),
        total_tokens=usage_metadata.get("total_tokens", 0),
    )

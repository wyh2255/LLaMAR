"""
轻量级 LLM 客户端兼容层 — 替代 mini_agent.llm.LLMClient。
Lightweight LLM client shim — replacement for mini_agent.llm.LLMClient.

当 mini_agent 包未安装时，RouterAgent 的 llm_client 为 None，导致
generate() 调用失败。此模块提供一个 OpenAI API 兼容的轻量客户端，
接口与 mini_agent 的 LLMClient 完全兼容：
  - generate(messages, tools) -> LLMResponse-like object
  - 返回对象具有 .content, .tool_calls, .finish_reason 属性

Lightweight replacement when mini_agent is not installed.
Provides the same generate(messages, tools) interface that RouterAgent expects.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── 兼容 mini_agent.schema 的数据类型 ──────────────────────────────────────
# Data types compatible with mini_agent.schema


@dataclass
class FunctionCall:
    """函数调用详情 — 与 mini_agent.schema.FunctionCall 兼容。"""
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolCall:
    """工具调用结构 — 与 mini_agent.schema.ToolCall 兼容。"""
    id: str
    type: str = "function"
    function: FunctionCall = field(default_factory=lambda: FunctionCall(name="", arguments={}))


@dataclass
class LLMResponse:
    """LLM 响应 — 与 mini_agent.schema.LLMResponse 兼容。"""
    content: str = ""
    thinking: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    finish_reason: str = "stop"
    usage: Any = None


@dataclass
class Message:
    """对话消息 — 与 mini_agent.schema.Message 兼容。"""
    role: str = ""
    content: str = ""
    thinking: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


# ── SimpleLLMClient ────────────────────────────────────────────────────────


class SimpleLLMClient:
    """
    OpenAI API 兼容的轻量 LLM 客户端。
    Lightweight OpenAI-compatible LLM client.

    使用 openai Python 库直接调用 LLM API，支持 tool calling。
    接口与 mini_agent.llm.LLMClient 完全兼容。

    API 密钥查找顺序：
      1. 构造函数参数 api_key
      2. 环境变量 OPENAI_API_KEY
      3. ~/openai_key.json 文件中的 my_openai_api_key 字段
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4-turbo",
    ):
        self.model = model
        self.api_base = api_base.rstrip("/")

        # API 密钥解析
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            # 尝试从 openai_key.json 读取
            key_path = os.path.expanduser("~/openai_key.json")
            if os.path.exists(key_path):
                try:
                    with open(key_path) as f:
                        self.api_key = json.load(f).get("my_openai_api_key", "")
                except Exception:
                    pass
        if not self.api_key:
            # 尝试从 mini_agent config.yaml 读取
            config_path = os.path.expanduser("~/.mini-agent/config/config.yaml")
            if os.path.exists(config_path):
                try:
                    import yaml
                    with open(config_path) as f:
                        cfg = yaml.safe_load(f)
                    if cfg:
                        self.api_key = cfg.get("api_key", "")
                        # 如果 config 中有 api_base 且构造函数未指定，使用 config 中的
                        if self.api_base == "https://api.openai.com/v1" and cfg.get("api_base"):
                            self.api_base = cfg["api_base"].rstrip("/")
                        # 如果 api_base 来自 config（非默认），使用 config 中的 model
                        # 因为不同 API 提供商支持的模型名不同
                        if cfg.get("api_base") and self.api_base == cfg["api_base"].rstrip("/") and cfg.get("model"):
                            self.model = cfg["model"]
                except Exception as e:
                    logger.warning("Failed to read mini_agent config: %s", e)

        if not self.api_key:
            logger.warning("No API key found for SimpleLLMClient")

        # 保存上一次 API 返回的 thinking 内容，用于在下一次请求中回传。
        # DeepSeek 的 thinking 模式要求：如果 API 返回了 thinking block，
        # 后续的 assistant 消息必须包含该 thinking block。
        # Store last thinking content from API response for pass-back.
        # DeepSeek thinking mode requires thinking blocks to be passed back.
        self._last_thinking: Optional[str] = None

        # LLM call log for experiment logging
        # 记录每次 LLM 调用的输入/输出，供 IntegrationLogger 使用
        self._call_log: list[dict] = []

        logger.info(
            "SimpleLLMClient initialized: model=%s, api_base=%s, has_key=%s",
            self.model, self.api_base, bool(self.api_key),
        )

    @property
    def _is_anthropic_api(self) -> bool:
        """判断是否使用 Anthropic 兼容 API（如 DeepSeek /anthropic 端点）。"""
        return "/anthropic" in self.api_base

    def _log_call(self, messages: list[Any], response: LLMResponse) -> None:
        """Record an LLM call in the internal call log.

        将 LLM 调用的 messages 摘要和 response 摘要存入 _call_log。
        每次 generate() 调用后自动调用。

        Args:
            messages: 输入消息列表
            response: LLM 响应对象
        """
        # Extract a summary of input messages (role + first 200 chars of content)
        msg_summary = []
        for msg in messages:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "") or ""
            msg_summary.append(f"{role}: {content[:200]}")

        # Extract tool call names from response
        tool_call_names = []
        if response.tool_calls:
            for tc in response.tool_calls:
                func = getattr(tc, "function", None)
                name = getattr(func, "name", "") if func else ""
                if name:
                    tool_call_names.append(name)

        self._call_log.append({
            "input_summary": " | ".join(msg_summary[-5:]),  # last 5 messages
            "output_content": (response.content or "")[:500],
            "thinking": response.thinking,
            "tool_calls": tool_call_names,
            "finish_reason": response.finish_reason,
        })

    def get_and_flush_call_log(self) -> list[dict]:
        """Return all logged LLM calls and clear the log.

        返回所有已记录的 LLM 调用并清空日志。
        由 SARCoordinator 在 submit_task() 完成后调用。

        Returns:
            list[dict]: 每个元素包含 input_summary, output_content, thinking,
                        tool_calls, finish_reason 键
        """
        log = list(self._call_log)
        self._call_log.clear()
        return log

    async def generate(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """
        调用 LLM 生成响应 — 与 mini_agent.llm.LLMClient.generate() 兼容。
        Generate LLM response — compatible with mini_agent.llm.LLMClient.generate().

        参数:
            messages: Message 对象列表（具有 role, content, tool_calls 等属性）
            tools: 工具 schema 列表（OpenAI function calling 格式）

        返回:
            LLMResponse 对象（具有 content, tool_calls, finish_reason 属性）
        """
        import httpx

        try:
            if self._is_anthropic_api:
                return await self._generate_anthropic(messages, tools)
            else:
                return await self._generate_openai(messages, tools)
        except Exception as e:
            logger.error("LLM API call failed: %s", e)
            return LLMResponse(
                content=f"Error calling LLM: {e}",
                finish_reason="error",
            )

    async def _generate_anthropic(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """调用 Anthropic 兼容 API（DeepSeek /anthropic 端点）。
        使用 anthropic Python SDK 以获得正确的认证处理。"""
        import anthropic

        # 转换消息格式为 Anthropic 格式
        system_text, api_messages = self._convert_messages_anthropic(messages)

        # 使用 anthropic SDK（与 mini_agent 的 AnthropicClient 相同方式）
        client = anthropic.AsyncAnthropic(
            base_url=self.api_base,
            api_key=self.api_key,
            default_headers={"Authorization": f"Bearer {self.api_key}"},
        )

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": api_messages,
        }
        if system_text:
            params["system"] = system_text
        if tools:
            params["tools"] = self._convert_tools_anthropic(tools)

        response = await client.messages.create(**params)

        # 转换 anthropic.Message 为 dict 以复用解析逻辑
        data = {
            "content": [
                {"type": block.type, **self._block_to_dict(block)}
                for block in response.content
            ],
            "stop_reason": response.stop_reason or "end_turn",
        }

        llm_response = self._parse_response_anthropic(data)

        # 保存 thinking 内容，以便在下一次请求中回传给 API。
        # DeepSeek thinking 模式要求：assistant 消息必须包含之前返回的 thinking block。
        # Store thinking content for pass-back in subsequent API calls.
        if llm_response.thinking:
            self._last_thinking = llm_response.thinking

        # Log the LLM call for experiment tracking
        self._log_call(messages, llm_response)

        return llm_response

    @staticmethod
    def _block_to_dict(block: Any) -> dict:
        """将 Anthropic content block 转换为 dict。"""
        if block.type == "text":
            return {"text": block.text}
        elif block.type == "tool_use":
            return {"id": block.id, "name": block.name, "input": block.input}
        elif block.type == "thinking":
            return {"thinking": getattr(block, "thinking", "")}
        return {}

    async def _generate_openai(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """调用 OpenAI 兼容 API。"""
        import httpx

        api_messages = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        url = f"{self.api_base}/chat/completions"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        llm_response = self._parse_response(data)

        # Log the LLM call for experiment tracking
        self._log_call(messages, llm_response)

        return llm_response

    def _convert_messages(self, messages: list[Any]) -> list[dict]:
        """将 Message 对象转换为 OpenAI API 消息格式。"""
        api_messages = []
        for msg in messages:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", "") or ""

            if role == "system":
                api_messages.append({"role": "system", "content": content})
            elif role == "user":
                api_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if content:
                    assistant_msg["content"] = content
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    tc_list = []
                    for tc in tool_calls:
                        func = getattr(tc, "function", None)
                        func_name = getattr(func, "name", "") if func else ""
                        func_args = getattr(func, "arguments", {}) if func else {}
                        if isinstance(func_args, str):
                            try:
                                func_args = json.loads(func_args)
                            except (json.JSONDecodeError, TypeError):
                                func_args = {}
                        tc_list.append({
                            "id": getattr(tc, "id", ""),
                            "type": getattr(tc, "type", "function"),
                            "function": {
                                "name": func_name,
                                "arguments": json.dumps(func_args),
                            },
                        })
                    assistant_msg["tool_calls"] = tc_list
                api_messages.append(assistant_msg)
            elif role == "tool":
                tool_call_id = getattr(msg, "tool_call_id", "") or ""
                api_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                })
            else:
                api_messages.append({"role": "user", "content": content})

        return api_messages

    def _convert_tools(self, tools: list[Any]) -> list[dict]:
        """将工具 schema 转换为 OpenAI function calling 格式。"""
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                if "type" in tool and tool["type"] == "function":
                    result.append(tool)
                elif "name" in tool and "parameters" in tool:
                    result.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool["parameters"],
                        },
                    })
                else:
                    result.append(tool)
            else:
                result.append(tool)
        return result

    def _parse_response(self, data: dict) -> LLMResponse:
        """解析 OpenAI API 响应为 LLMResponse。"""
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        content = message.get("content", "") or ""
        tool_calls = None

        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    type="function",
                    function=FunctionCall(
                        name=func.get("name", ""),
                        arguments=args,
                    ),
                ))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
        )

    # ── Anthropic API 兼容方法 ──────────────────────────────────────────────

    def _convert_messages_anthropic(
        self, messages: list[Any]
    ) -> tuple[str, list[dict]]:
        """将 Message 对象转换为 Anthropic Messages API 格式。
        返回 (system_text, messages_list)。

        特殊处理：连续的 tool 消息会被合并为一个 user 消息（包含多个 tool_result block），
        因为 Anthropic API 要求 tool_result 紧跟在 tool_use 之后的同一条 user 消息中。
        Special handling: consecutive tool messages are merged into a single
        user message with multiple tool_result blocks, as required by Anthropic API.
        """
        system_text = ""
        api_messages = []

        # 用于合并连续 tool 消息的缓冲区
        # Buffer for merging consecutive tool messages
        pending_tool_results: list[dict] = []

        def _flush_tool_results() -> None:
            """将缓冲区中的 tool_result 合并为一条 user 消息。"""
            nonlocal pending_tool_results
            if pending_tool_results:
                api_messages.append({
                    "role": "user",
                    "content": list(pending_tool_results),
                })
                pending_tool_results = []

        for msg in messages:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", "") or ""

            if role == "system":
                system_text = content
            elif role == "user":
                _flush_tool_results()
                api_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                _flush_tool_results()
                assistant_content: list[dict] = []
                # Anthropic 使用 content blocks

                # 回传 thinking block — DeepSeek thinking 模式要求
                # assistant 消息必须包含之前返回的 thinking block。
                # Pass back thinking block — DeepSeek thinking mode requires
                # assistant messages to include previously returned thinking.
                thinking = getattr(msg, "thinking", None)
                if thinking:
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": thinking,
                    })
                elif self._last_thinking:
                    # 如果 Message 对象没有 thinking 属性，使用缓存的 thinking
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": self._last_thinking,
                    })
                    # 使用后清除，避免重复回传
                    # Clear after use to avoid duplicate pass-back
                    self._last_thinking = None

                # 文本内容（放在 thinking 之后、tool_use 之前）
                # Text content (after thinking, before tool_use)
                if content:
                    assistant_content.append({
                        "type": "text",
                        "text": content,
                    })

                # tool_use blocks — 同时支持 dict 和对象格式
                # (RouterAgent 通过 _serialize_tool_calls 将 tool_calls 存为 dict)
                # tool_use blocks — support both dict and object formats
                # (RouterAgent stores tool_calls as dicts via _serialize_tool_calls)
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            # dict 格式 (来自 RouterAgent._serialize_tool_calls)
                            func = tc.get("function", {})
                            func_name = func.get("name", "") if isinstance(func, dict) else ""
                            func_args = func.get("arguments", {}) if isinstance(func, dict) else {}
                            tc_id = tc.get("id", "")
                        else:
                            # 对象格式 (来自 LLMResponse.tool_calls)
                            func = getattr(tc, "function", None)
                            func_name = getattr(func, "name", "") if func else ""
                            func_args = getattr(func, "arguments", {}) if func else {}
                            tc_id = getattr(tc, "id", "")
                        if isinstance(func_args, str):
                            try:
                                func_args = json.loads(func_args)
                            except (json.JSONDecodeError, TypeError):
                                func_args = {}
                        assistant_content.append({
                            "type": "tool_use",
                            "id": tc_id,
                            "name": func_name,
                            "input": func_args,
                        })

                if assistant_content:
                    api_messages.append({
                        "role": "assistant",
                        "content": assistant_content,
                    })
                else:
                    api_messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                # 收集 tool_result 到缓冲区，后续合并为一条 user 消息
                # Accumulate tool_results into buffer for later merge
                tool_call_id = getattr(msg, "tool_call_id", "") or ""
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content,
                })
            else:
                _flush_tool_results()
                api_messages.append({"role": "user", "content": content})

        # 循环结束后，刷新剩余的 tool_result
        # Flush any remaining tool_results after the loop
        _flush_tool_results()

        return system_text, api_messages

    def _convert_tools_anthropic(self, tools: list[Any]) -> list[dict]:
        """将工具 schema 转换为 Anthropic tool_use 格式。"""
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                if "type" in tool and tool["type"] == "function":
                    # OpenAI 格式 -> Anthropic 格式
                    func = tool.get("function", {})
                    result.append({
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                    })
                elif "name" in tool and "input_schema" in tool:
                    # 已经是 Anthropic 格式
                    result.append(tool)
                elif "name" in tool and "parameters" in tool:
                    result.append({
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool["parameters"],
                    })
                else:
                    result.append(tool)
            else:
                result.append(tool)
        return result

    def _parse_response_anthropic(self, data: dict) -> LLMResponse:
        """解析 Anthropic Messages API 响应为 LLMResponse。"""
        content_text = ""
        thinking_text = None
        tool_calls = None

        content_blocks = data.get("content", [])
        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "thinking":
                thinking_text = block.get("thinking", "")
            elif block_type == "text":
                content_text += block.get("text", "")
            elif block_type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    type="function",
                    function=FunctionCall(
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    ),
                ))

        stop_reason = data.get("stop_reason", "end_turn")
        # 映射 Anthropic stop_reason 到 OpenAI finish_reason
        finish_reason_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        finish_reason = finish_reason_map.get(stop_reason, "stop")

        return LLMResponse(
            content=content_text,
            thinking=thinking_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

"""Worker 端 ReAct 循环 — 替代 mini_agent.Agent。"""
from __future__ import annotations
import json
import logging

from integration.coordinator.llm_shim import SimpleLLMClient, Message
from integration.sar_workers.tool_defs import ToolDef

logger = logging.getLogger(__name__)


class WorkerReActAgent:
    """Worker 端 ReAct 循环。

    每步调用 LLM，有 tool_calls 则执行工具并继续，无则返回最终文本。
    """

    def __init__(
        self,
        llm_client: SimpleLLMClient,
        tools: list[ToolDef],
        system_prompt: str,
        max_steps: int = 20,
    ):
        self._llm = llm_client
        self._tools = {t.name: t for t in tools}
        self._tool_schemas = [t.to_schema() for t in tools]
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._messages: list[Message] = []

    def update_system_prompt(self, prompt: str):
        self._system_prompt = prompt

    async def run(self, user_message: str = "") -> str:
        self._messages = [Message(role="system", content=self._system_prompt)]
        if user_message:
            self._messages.append(Message(role="user", content=user_message))

        for step in range(self._max_steps):
            logger.debug(f"ReAct step {step + 1}/{self._max_steps}")
            response = await self._llm.generate(
                messages=self._messages, tools=self._tool_schemas,
            )
            self._messages.append(Message(
                role="assistant", content=response.content or "",
                tool_calls=response.tool_calls,
            ))
            if not response.tool_calls:
                return response.content or ""

            for tc in response.tool_calls:
                tool_name = tc.function.name
                tool_def = self._tools.get(tool_name)
                if tool_def is None:
                    result = f"Error: unknown tool '{tool_name}'"
                else:
                    try:
                        args = tc.function.arguments
                        if isinstance(args, str):
                            args = json.loads(args)
                        result = await tool_def.execute(**args)
                    except Exception as e:
                        logger.error(f"Tool {tool_name} failed: {e}")
                        result = f"Error executing {tool_name}: {e}"
                self._messages.append(Message(
                    role="tool", content=str(result),
                    tool_call_id=tc.id, name=tool_name,
                ))

        return f"Max steps ({self._max_steps}) exceeded"

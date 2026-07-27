"""Agent 核心实现。"""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Awaitable, Callable, Optional

import tiktoken

from .llm import LLMClient
from .logger import AgentLogger
from .hooks import AgentHooks, CoordinatorSARHooks
from .schema import Message, RunResult
from .tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


# ANSI 颜色码
class Colors:
    """终端颜色定义"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # 前景色
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # 亮色
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """单 Agent：携带基本工具和 MCP 支持。"""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # token 超过此值触发摘要
        log_dir: str | Path | None = None,
        # —— 上下文管理 ——
        context_strategy: str = "hybrid",
        context_recent_messages: int = 12,
        context_summary_trigger_ratio: float = 0.8,
        context_pinned_enabled: bool = True,
        hooks: AgentHooks | None = None,
        require_explicit_completion: bool = False,
    ):
        """初始化 Agent。

        Args:
            llm_client: LLM 客户端实例。
            system_prompt: Agent 的系统提示词。
            tools: Agent 可用的工具列表。
            max_steps: 最大步数。
            workspace_dir: 工作目录。
            token_limit: 触发摘要的 token 上限。
            log_dir: Agent 日志目录，传给 AgentLogger。
            context_strategy: 上下文管理策略（"none", "summary", "hybrid"）。
            context_recent_messages: 保留的最近原始消息数量。
            context_summary_trigger_ratio: 触发摘要的 token 比例阈值。
            context_pinned_enabled: 是否使用 pinned 状态记忆。
            hooks: 可选的 Agent 生命周期钩子。
            require_explicit_completion: 若为 True，仅当工具设置 task_complete=True
                时才退出循环；纯文本响应会触发 nudge。
        """
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        # 取消事件（外部设置，例如 Esc 键），用于中断 Agent 执行
        self.cancel_event: Optional[asyncio.Event] = None

        # 上下文管理配置
        self.context_strategy = context_strategy
        self.context_recent_messages = context_recent_messages
        self.context_summary_trigger_ratio = context_summary_trigger_ratio
        self.context_pinned_enabled = context_pinned_enabled
        self.hooks = hooks
        self.require_explicit_completion = require_explicit_completion
        self._max_nudges = 3

        # 显式完成状态
        self._task_complete = False
        self._mission_success: bool | None = None
        self._task_description = ""
        self._nudge_count = 0

        # 确保工作目录存在
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # 如果 system prompt 中没有工作目录信息则注入
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt

        # 初始化消息历史
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

        # 初始化日志器
        self.logger = AgentLogger(log_dir=log_dir)

        # 上一次 API 返回的 token 用量（每次 LLM 调用后更新）
        self.api_total_tokens: int = 0
        self.api_prompt_tokens: int = 0
        self.api_completion_tokens: int = 0
        self.api_cache_hit_tokens: int = 0
        self.api_cache_miss_tokens: int = 0
        # 所有 LLM 调用（含摘要）的累计 token 用量
        self.cumulative_total_tokens: int = 0
        self.cumulative_prompt_tokens: int = 0
        self.cumulative_completion_tokens: int = 0
        self.cumulative_cache_hit_tokens: int = 0
        self.cumulative_cache_miss_tokens: int = 0
        # 跳过摘要后的首次 token 检查（避免连续触发）
        self._skip_next_token_check: bool = False

    def attach_context(self, ctx) -> None:
        """注入 ContextManager 并创建绑定其上的 SAR 钩子。

        A2A 适配器使用此方法将会话记忆注入新创建的 Agent 实例，
        同时保持 ContextManager 在多次 run 之间存活。
        """
        from .context import ContextManager

        if not isinstance(ctx, ContextManager):
            raise TypeError("attach_context() 需要 ContextManager 实例")
        self._context = ctx
        self.hooks = CoordinatorSARHooks(ctx)

    def add_user_message(self, content: str):
        """向消息历史追加一条用户消息。"""
        self.messages.append(Message(role="user", content=content))

    def _check_cancelled(self) -> bool:
        """检查 Agent 执行是否已被取消。

        Returns:
            已取消返回 True，否则返回 False。
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def _cleanup_incomplete_messages(self):
        """删除不完整的 assistant 消息及其部分 tool 结果。

        确保取消后消息历史的一致性：只删除当前步骤未完成的消息，
        保留已完成步骤的消息。
        """
        # 找到最后一条 assistant 消息的索引
        last_assistant_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx == -1:
            # 没有 assistant 消息，无需清理
            return

        # 删除最后一条 assistant 消息及其之后的所有 tool 结果
        removed_count = len(self.messages) - last_assistant_idx
        if removed_count > 0:
            self.messages = self.messages[:last_assistant_idx]
            print(f"{Colors.DIM}   已清理 {removed_count} 条不完整消息{Colors.RESET}")

    def _estimate_tokens(self) -> int:
        """使用 tiktoken 精确计算消息历史的 token 数。

        使用 cl100k_base 编码器（兼容 GPT-4/Claude/M2）。
        """
        try:
            # 使用 cl100k_base 编码器（GPT-4 及大多数现代模型使用）
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # 降级：若 tiktoken 初始化失败，使用简单估算
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.messages:
            # 计算文本内容
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        # 将 dict 转为字符串计算
                        total_tokens += len(encoding.encode(str(block)))

            # 计算 thinking 内容
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # 计算 tool_calls
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # 每条消息的元数据开销（约 4 token）
            total_tokens += 4

        return total_tokens

    def _estimate_tokens_fallback(self) -> int:
        """降级 token 估算方法（tiktoken 不可用时）。"""
        total_chars = 0
        for msg in self.messages:
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))

            if msg.thinking:
                total_chars += len(msg.thinking)

            if msg.tool_calls:
                total_chars += len(str(msg.tool_calls))

        # 粗略估算：平均 2.5 字符 = 1 token
        return int(total_chars / 2.5)

    async def _summarize_messages(self):
        """消息历史摘要：当 token 超限时对用户消息之间的对话进行摘要。

        策略（Agent 模式）：
        - 保留所有用户消息（用户意图）
        - 对每对 user-user 之间的内容（Agent 执行过程）做摘要
        - 如果最后一轮仍在执行（有 agent/tool 消息但无下一条用户消息），也做摘要
        - 结构：system -> user1 -> summary1 -> user2 -> summary2 -> user3 -> summary3（若执行中）

        在以下任一条件满足时触发摘要：
        - 本地 token 估计超过限制
        - API 报告的 total_tokens 超过限制
        """
        # 刚完成摘要时跳过检查（等下一次 LLM 调用更新 api_total_tokens）
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated_tokens = self._estimate_tokens()

        # 检查本地估算和 API 报告两种指标
        should_summarize = (
            estimated_tokens > self.token_limit
            or self.api_total_tokens > self.token_limit
        )

        # 均未超限则无需摘要
        if not should_summarize:
            return

        print(
            f"\n{Colors.BRIGHT_YELLOW}📊 Token 用量 - 本地估计: {estimated_tokens}, API 报告: {self.api_total_tokens}, 上限: {self.token_limit}{Colors.RESET}"
        )
        print(f"{Colors.BRIGHT_YELLOW}🔄 触发消息历史摘要...{Colors.RESET}")

        # 找到所有用户消息的索引（跳过 system prompt）
        user_indices = [
            i for i, msg in enumerate(self.messages) if msg.role == "user" and i > 0
        ]

        # 至少需要 1 条用户消息才能执行摘要
        if len(user_indices) < 1:
            print(f"{Colors.BRIGHT_YELLOW}⚠️  消息不足，无法摘要{Colors.RESET}")
            return

        # 构建新消息列表
        new_messages = [self.messages[0]]  # 保留 system prompt
        summary_count = 0

        # 遍历每条用户消息，对其后的执行过程做摘要
        for i, user_idx in enumerate(user_indices):
            # 添加当前用户消息
            new_messages.append(self.messages[user_idx])

            # 确定需要摘要的消息范围
            # 若是最后一条用户消息，则到消息列表末尾；否则到下一个用户消息之前
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self.messages)

            # 提取本轮的执行消息
            execution_messages = self.messages[user_idx + 1 : next_user_idx]

            # 如果本轮有执行消息，做摘要
            if execution_messages:
                summary_text = await self._create_summary(execution_messages, i + 1)
                if summary_text:
                    summary_message = Message(
                        role="user",
                        content=f"[Assistant Execution Summary]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # 替换消息列表
        self.messages = new_messages

        # 跳过下一次 token 检查，避免连续触发摘要
        # （api_total_tokens 会在下一次 LLM 调用后更新）
        self._skip_next_token_check = True

        new_tokens = self._estimate_tokens()
        print(
            f"{Colors.BRIGHT_GREEN}✓ 摘要完成，本地 token: {estimated_tokens} → {new_tokens}{Colors.RESET}"
        )
        print(
            f"{Colors.DIM}  结构: system + {len(user_indices)} 条用户消息 + {summary_count} 条摘要{Colors.RESET}"
        )
        print(
            f"{Colors.DIM}  注意: API token 数会在下一次 LLM 调用后更新{Colors.RESET}"
        )

    async def _create_summary(self, messages: list[Message], round_num: int) -> str:
        """为单轮执行创建摘要。

        Args:
            messages: 需要摘要的消息列表。
            round_num: 轮次编号。

        Returns:
            摘要文本。
        """
        if not messages:
            return ""

        # 构建摘要内容
        summary_content = f"Round {round_num} execution process:\n\n"
        for msg in messages:
            if msg.role == "assistant":
                content_text = (
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
                summary_content += f"Assistant: {content_text}\n"
                if msg.tool_calls:
                    tool_names = [tc.function.name for tc in msg.tool_calls]
                    summary_content += f"  → Called tools: {', '.join(tool_names)}\n"
            elif msg.role == "tool":
                result_preview = (
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
                summary_content += f"  ← Tool returned: {result_preview}...\n"

        # 调用 LLM 生成简洁摘要
        try:
            summary_prompt = f"""Please provide a concise summary of the following Agent execution process:

{summary_content}

Requirements:
1. Focus on what tasks were completed and which tools were called
2. Keep key execution results and important findings
3. Be concise and clear, within 1000 words
4. Use English
5. Do not include "user" related content, only summarize the Agent's execution process"""

            summary_msg = Message(role="user", content=summary_prompt)
            response = await self.llm.generate(
                messages=[
                    Message(
                        role="system",
                        content="You are an assistant skilled at summarizing Agent execution processes.",
                    ),
                    summary_msg,
                ]
            )

            # 跟踪摘要的 token 用量
            if response.usage:
                self.cumulative_total_tokens += response.usage.total_tokens
                self.cumulative_prompt_tokens += response.usage.prompt_tokens
                self.cumulative_completion_tokens += response.usage.completion_tokens
                self.cumulative_cache_hit_tokens += response.usage.cache_hit_tokens
                self.cumulative_cache_miss_tokens += response.usage.cache_miss_tokens

            summary_text = response.content
            print(f"{Colors.BRIGHT_GREEN}✓ 第 {round_num} 轮摘要生成成功{Colors.RESET}")
            return summary_text

        except Exception as e:
            print(
                f"{Colors.BRIGHT_RED}✗ 第 {round_num} 轮摘要生成失败: {e}{Colors.RESET}"
            )
            # 失败时返回简单的文本摘要
            return summary_content

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        step_callback: Optional[Callable[..., Awaitable[None]]] = None,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> RunResult:
        """执行 Agent 循环，直到任务完成或达到最大步数。

        Args:
            cancel_event: 可选的 asyncio.Event，设置后可取消执行。
                Agent 会在下一个安全检查点停止（完成当前步骤后，保持消息一致）。
            step_callback: 可选异步回调，每步按事件类型调用。
                三种事件类型：
                - "llm_response"(content, tool_calls): LLM 返回后
                - "tool_start"(tool_name, arguments): 工具执行前
                - "tool_result"(tool_name, success, content): 工具返回后
                回调中的异常仅记录，不会传播。
            task_id: 可选任务标识，传给 AgentLogger 作为文件名。
            context_id: 可选上下文标识，包含在 NDJSON 日志条目中。

        Returns:
            包含最终响应内容、成功标志和已用步数的 RunResult。
        """
        # 设置取消事件（也可以在调用 run() 前通过 self.cancel_event 设置）
        if cancel_event is not None:
            self.cancel_event = cancel_event

        # 开始新运行，初始化日志文件
        self.logger.start_new_run(
            task_id=task_id or "",
            context_id=context_id or "",
        )
        print(
            f"{Colors.DIM}📝 日志文件: {self.logger.get_log_file_path()}{Colors.RESET}"
        )

        if self.hooks is not None:
            user_message = ""
            for msg in reversed(self.messages):
                if msg.role == "user":
                    user_message = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    break
            await self.hooks.on_run_start(self, user_message)

        step = 0
        run_start_time = perf_counter()

        while step < self.max_steps:
            # 每步开始时检查取消
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                result = RunResult(content=cancel_msg, success=None, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # 钩子可以信号提前终止（例如某个工具设置了 task_complete）
            if self.hooks is not None and not await self.hooks.should_continue(
                self, step
            ):
                final_content = ""
                for msg in reversed(self.messages):
                    if msg.role == "assistant" and isinstance(msg.content, str):
                        final_content = msg.content
                        break
                result = RunResult(
                    content=final_content,
                    success=self._mission_success,
                    steps_used=step,
                    task_description=self._task_description,
                    task_complete=bool(self._task_complete),
                )
                await self.hooks.on_run_end(self, result)
                return result

            step_start_time = perf_counter()
            # 无 hooks 时使用内置摘要管理上下文（有 hooks 时由 context.py 的
            # prune_history + assemble 接管上下文管理）
            if self.hooks is None:
                await self._summarize_messages()

            # 步骤标题（有 hooks 时由 logger/hook 记录，仅打印简略行）
            print(
                f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 步骤 {step + 1}/{self.max_steps}{Colors.RESET}"
            )

            # 获取工具列表供 LLM 调用
            tool_list = list(self.tools.values())

            # 允许钩子重写发给 LLM 的消息
            messages_for_llm = self.messages
            if self.hooks is not None:
                messages_for_llm = await self.hooks.pre_llm(self, self.messages)

            # 记录 LLM 请求并调用 LLM
            self.logger.log_request(
                messages=messages_for_llm, tools=tool_list, step_index=step
            )

            try:
                response = await self.llm.generate(
                    messages=messages_for_llm, tools=tool_list
                )
            except Exception as e:
                # 检查是否为重试耗尽错误
                from .retry import RetryExhaustedError

                if isinstance(e, RetryExhaustedError):
                    error_msg = f"LLM 调用在 {e.attempts} 次重试后失败\n最后一次错误: {str(e.last_exception)}"
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ 重试失败:{Colors.RESET} {error_msg}"
                    )
                else:
                    error_msg = f"LLM 调用失败: {str(e)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ 错误:{Colors.RESET} {error_msg}")
                result = RunResult(content=error_msg, success=False, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # 累加 API 报告的 token 用量
            if response.usage:
                self.api_total_tokens = response.usage.total_tokens
                self.api_prompt_tokens = response.usage.prompt_tokens
                self.api_completion_tokens = response.usage.completion_tokens
                self.api_cache_hit_tokens = response.usage.cache_hit_tokens
                self.api_cache_miss_tokens = response.usage.cache_miss_tokens
                self.cumulative_total_tokens += response.usage.total_tokens
                self.cumulative_prompt_tokens += response.usage.prompt_tokens
                self.cumulative_completion_tokens += response.usage.completion_tokens
                self.cumulative_cache_hit_tokens += response.usage.cache_hit_tokens
                self.cumulative_cache_miss_tokens += response.usage.cache_miss_tokens

            if self.hooks is not None:
                await self.hooks.post_llm(self, response)

            # 记录 LLM 响应
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cache_hit_tokens": response.usage.cache_hit_tokens,
                    "cache_miss_tokens": response.usage.cache_miss_tokens,
                }
            self.logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=usage,
            )

            # 添加 assistant 消息
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_msg)

            # 通知 step_callback 关于 LLM 响应
            if step_callback is not None:
                try:
                    await step_callback(
                        "llm_response",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        usage=response.usage,
                    )
                except Exception:
                    logger.exception("step_callback(llm_response) 失败")

            # 打印 thinking 内容
            if response.thinking:
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 思考:{Colors.RESET}")
                print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

            # 打印 assistant 响应
            if response.content:
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{response.content}")

            # 检查任务是否完成（无 tool_calls）
            if not response.tool_calls:
                step_elapsed = perf_counter() - step_start_time
                total_elapsed = perf_counter() - run_start_time
                print(
                    f"\n{Colors.DIM}⏱️  步骤 {step + 1} 完成，耗时 {step_elapsed:.2f}s（累计: {total_elapsed:.2f}s）{Colors.RESET}"
                )
                should_exit = True
                if self.hooks is not None:
                    should_exit = not await self.hooks.should_continue(self, step)
                if should_exit:
                    if self._task_complete:
                        result = RunResult(
                            content=response.content,
                            success=self._mission_success,
                            steps_used=step + 1,
                            task_description=self._task_description,
                            task_complete=True,
                        )
                    else:
                        result = RunResult(
                            content=response.content,
                            success=None,
                            steps_used=step + 1,
                        )
                    if self.hooks is not None:
                        await self.hooks.on_run_end(self, result)
                    return result
                if self.require_explicit_completion:
                    self._inject_continue_nudge()
                    self._nudge_count += 1
                    if self._nudge_count > self._max_nudges:
                        result = RunResult(
                            content=response.content,
                            success=False,
                            steps_used=step + 1,
                        )
                        if self.hooks is not None:
                            await self.hooks.on_run_end(self, result)
                        return result
                    continue
                # 防御性降级：不要求显式完成时，纯文本即退出
                result = RunResult(
                    content=response.content, success=None, steps_used=step + 1
                )
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # 执行工具前检查取消
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                result = RunResult(content=cancel_msg, success=None, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # 执行工具调用
            for tool_call in response.tool_calls:
                tool_call_id = tool_call.id
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments

                # 工具调用标题
                print(
                    f"\n{Colors.BRIGHT_YELLOW}🔧 工具调用:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}"
                )

                # 参数展示（格式化输出）
                print(f"{Colors.DIM}   参数:{Colors.RESET}")
                # 截断过长的参数值
                truncated_args = {}
                for key, value in arguments.items():
                    value_str = str(value)
                    if len(value_str) > 200:
                        truncated_args[key] = value_str[:200] + "..."
                    else:
                        truncated_args[key] = value
                args_json = json.dumps(truncated_args, indent=2, ensure_ascii=False)
                for line in args_json.split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

                # 通知 step_callback 工具开始执行
                if step_callback is not None:
                    try:
                        await step_callback(
                            "tool_start",
                            tool_name=function_name,
                            arguments=arguments,
                        )
                    except Exception:
                        logger.exception("step_callback(tool_start) 失败")

                # 允许钩子重写工具参数
                if self.hooks is not None:
                    arguments = await self.hooks.pre_tool(
                        self, function_name, arguments
                    )

                # 执行工具
                if function_name not in self.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"未知工具: {function_name}",
                    )
                else:
                    try:
                        tool = self.tools[function_name]
                        result = await tool.execute(**arguments)
                    except Exception as e:
                        # 捕获工具执行中的所有异常，转为失败的 ToolResult
                        import traceback

                        error_detail = f"{type(e).__name__}: {str(e)}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"工具执行失败: {error_detail}\n\n回溯:\n{error_trace}",
                        )

                # 允许钩子观察/重写工具结果
                if self.hooks is not None:
                    result = await self.hooks.post_tool(self, function_name, result)

                # 记录工具执行结果
                self.logger.log_tool_result(
                    tool_name=function_name,
                    arguments=arguments,
                    success=result.success,
                    result=result.content if result.success else "",
                    error=result.error if not result.success and result.error else "",
                )

                # 打印结果
                if result.success:
                    result_text = result.content
                    if len(result_text) > 300:
                        result_text = (
                            result_text[:300] + f"{Colors.DIM}...{Colors.RESET}"
                        )
                    print(f"{Colors.BRIGHT_GREEN}✓ 结果:{Colors.RESET} {result_text}")
                else:
                    print(
                        f"{Colors.BRIGHT_RED}✗ 错误:{Colors.RESET} {Colors.RED}{result.error}{Colors.RESET}"
                    )

                # 添加工具结果消息
                tool_msg = Message(
                    role="tool",
                    content=result.content
                    if result.success
                    else f"Error: {result.error}",
                    tool_call_id=tool_call_id,
                    name=function_name,
                )
                self.messages.append(tool_msg)

                # 通知 step_callback 工具结果
                if step_callback is not None:
                    try:
                        result_text = (
                            result.content
                            if result.success
                            else f"Error: {result.error}"
                        )
                        await step_callback(
                            "tool_result",
                            tool_name=function_name,
                            success=result.success,
                            content=result_text,
                            data=result.data,
                        )
                    except Exception:
                        logger.exception("step_callback(tool_result) 失败")

                # 每次工具执行后检查取消
                if self._check_cancelled():
                    self._cleanup_incomplete_messages()
                    cancel_msg = "Task cancelled by user."
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                    result = RunResult(
                        content=cancel_msg, success=None, steps_used=step
                    )
                    if self.hooks is not None:
                        await self.hooks.on_run_end(self, result)
                    return result

            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            print(
                f"\n{Colors.DIM}⏱️  步骤 {step + 1} 完成，耗时 {step_elapsed:.2f}s（累计: {total_elapsed:.2f}s）{Colors.RESET}"
            )

            step += 1

        # 达到最大步数
        error_msg = f"任务在 {self.max_steps} 步后未能完成。"
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
        result = RunResult(content=error_msg, success=False, steps_used=self.max_steps)
        if self.hooks is not None:
            await self.hooks.on_run_end(self, result)
        return result

    def _inject_continue_nudge(self) -> None:
        """注入一条用户消息，提示 Agent 继续或完成。"""
        nudge = (
            "You have not yet marked the task as complete. "
            "Please continue working toward the goal. "
            "When the task is finished, call the finish_task tool."
        )
        self.messages.append(Message(role="user", content=nudge))
        print(f"{Colors.YELLOW}🔔 {nudge}{Colors.RESET}")

    def get_history(self) -> list[Message]:
        """获取消息历史。"""
        return self.messages.copy()

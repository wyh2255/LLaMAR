"""Core Agent implementation."""

import asyncio
import contextlib
import inspect
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Awaitable, Callable, Optional

import tiktoken

from .llm import LLMClient
from .logger import AgentLogger
from .hooks import AgentHooks, WorkerSARHooks
from .schema import Message, RunResult
from a2a.worker.need_input import NeedInputError
from .tools.base import Tool, ToolResult

from Agent.error_taxonomy import error_code_for_result
from Agent.redaction import SensitiveTextRedactor

logger = logging.getLogger(__name__)

# Shared defensive redactor: failed ToolResult content/error/recursive data must
# never leak into context, logger, step callback or A2A sinks as raw secrets.
_REDACTOR = SensitiveTextRedactor()


class _RequestCancelled(Exception):
    """Raised by the cancel race helper when cancel_event fires mid-request.

    Distinct from asyncio.CancelledError: this is a business cancellation
    (TASK_CANCEL / Esc). Callers write a status=cancelled log_abort marker
    and return normally.
    """


# ANSI color codes
class Colors:
    """Terminal color definitions"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """Single agent with basic tools and MCP support."""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # Summary triggered when tokens exceed this value
        log_dir: str | Path | None = None,
        task_id: str = "",
        context_id: str = "",
        # —— context management ——
        context_strategy: str = "hybrid",
        context_recent_messages: int = 12,
        context_summary_trigger_ratio: float = 0.8,
        context_pinned_enabled: bool = True,
        output_schema: str = "",
        hooks: AgentHooks | None = None,
        require_explicit_completion: bool = False,
    ):
        """Initialize Agent.

        Args:
            llm_client: LLM client instance.
            system_prompt: System prompt for the agent.
            tools: List of tools available to the agent.
            max_steps: Maximum number of steps.
            workspace_dir: Workspace directory.
            token_limit: Token limit before summarization.
            log_dir: Log directory for agent logs. Passed to AgentLogger.
            context_strategy: Context management strategy ("none", "summary", "hybrid").
            context_recent_messages: Number of recent raw messages to keep.
            context_summary_trigger_ratio: Token ratio at which to trigger summarization.
            context_pinned_enabled: Whether to use pinned state memory.
            output_schema: Expected output format description; folded into the
                stable system prompt ``## Output / Response Contract`` section
                (never the trailing role=user state block).
            hooks: Optional agent lifecycle hooks.
            require_explicit_completion: If True, the loop only exits when a tool
                sets task_complete=True; plain text responses trigger a nudge.
        """
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        # Cancellation event for interrupting agent execution (set externally, e.g., by Esc key)
        self.cancel_event: Optional[asyncio.Event] = None
        # Step of the in-flight LLM request (used by log_abort to annotate the
        # interrupted step; non-None at run() exit means the request never
        # terminated → fallback writes an aborted marker).
        self._active_request_step: int | None = None

        # Context management configuration
        self.context_strategy = context_strategy
        self.context_recent_messages = context_recent_messages
        self.context_summary_trigger_ratio = context_summary_trigger_ratio
        self.context_pinned_enabled = context_pinned_enabled
        self.hooks = hooks
        self.require_explicit_completion = require_explicit_completion
        self._max_nudges = 3

        # Explicit completion state
        self._task_complete = False
        self._mission_success: bool | None = None
        self._task_description = ""
        self._nudge_count = 0

        # Ensure workspace exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace information into system prompt if not already present
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        # Output contract: folded into the stable system prompt, never the
        # trailing role=user state block.
        if output_schema and "## Output / Response Contract" not in system_prompt:
            system_prompt = (
                system_prompt + f"\n\n## Output / Response Contract\n{output_schema}"
            )

        self.system_prompt = system_prompt

        # Initialize message history
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

        # Initialize logger
        self.logger = AgentLogger(
            log_dir=log_dir, task_id=task_id, context_id=context_id
        )

        # Token usage from last API response (updated after each LLM call)
        self.api_total_tokens: int = 0
        self.api_prompt_tokens: int = 0
        self.api_completion_tokens: int = 0
        self.api_cache_hit_tokens: int = 0
        self.api_cache_miss_tokens: int = 0
        # Cumulative token usage across all LLM calls (including summarization)
        self.cumulative_total_tokens: int = 0
        self.cumulative_prompt_tokens: int = 0
        self.cumulative_completion_tokens: int = 0
        self.cumulative_cache_hit_tokens: int = 0
        self.cumulative_cache_miss_tokens: int = 0
        # Flag to skip token check right after summary (avoid consecutive triggers)
        self._skip_next_token_check: bool = False

    def attach_context(self, ctx) -> None:
        """Attach a ContextManager and create SAR hooks bound to it.

        This is used by A2A adapters to inject session memory into a fresh
        Agent instance while keeping the ContextManager alive across runs.
        """
        from .context import ContextManager

        if not isinstance(ctx, ContextManager):
            raise TypeError("attach_context() expects a ContextManager instance")
        self._context = ctx
        self.hooks = WorkerSARHooks(ctx)

    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.messages.append(Message(role="user", content=content))

    def _check_cancelled(self) -> bool:
        """Check if agent execution has been cancelled.

        Returns:
            True if cancelled, False otherwise.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    async def _llm_generate_cancellable(
        self, messages: list[Message], tools: list[Tool]
    ):
        """Race the in-flight LLM call against cancel_event.

        When cancel_event fires the in-flight request is cancelled and
        ``_RequestCancelled`` is raised (root fix for hung LLM requests: the
        Charlie 13-minute hang was caused by generate awaiting forever with no
        cancellation check). Without a cancel_event the call goes direct.

        Returns:
            LLMResponse

        Raises:
            _RequestCancelled: cancel_event fired while the request was in flight
            Exception: exceptions raised by the LLM call itself (timeout / retry
                exhaustion included)
        """
        if self.cancel_event is None:
            return await self.llm.generate(messages, tools)

        generate_task = asyncio.ensure_future(self.llm.generate(messages, tools))
        cancel_wait = asyncio.ensure_future(self.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {generate_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if generate_task in done:
                # Request finished first (or in the same tick as the cancel):
                # prefer the request result.
                cancel_wait.cancel()
                return generate_task.result()
            # cancel_event fired: cancel the in-flight request, drop its result
            generate_task.cancel()
            with contextlib.suppress(BaseException):
                await generate_task
            raise _RequestCancelled()
        except asyncio.CancelledError:
            # Outer task cancelled (process shutdown etc.): do not swallow.
            # run()'s in-flight window fallback writes the aborted marker.
            generate_task.cancel()
            raise
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()

    def _cleanup_incomplete_messages(self):
        """Remove the incomplete assistant message and its partial tool results.

        This ensures message consistency after cancellation by removing
        only the current step's incomplete messages, preserving completed steps.
        """
        # Find the index of the last assistant message
        last_assistant_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx == -1:
            # No assistant message found, nothing to clean
            return

        # Remove the last assistant message and all tool results after it
        removed_count = len(self.messages) - last_assistant_idx
        if removed_count > 0:
            self.messages = self.messages[:last_assistant_idx]
            print(
                f"{Colors.DIM}   Cleaned up {removed_count} incomplete message(s){Colors.RESET}"
            )

    def _estimate_tokens(self) -> int:
        """Accurately calculate token count for message history using tiktoken

        Uses cl100k_base encoder (GPT-4/Claude/M2 compatible)
        """
        try:
            # Use cl100k_base encoder (used by GPT-4 and most modern models)
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback: if tiktoken initialization fails, use simple estimation
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.messages:
            # Count text content
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        # Convert dict to string for calculation
                        total_tokens += len(encoding.encode(str(block)))

            # Count thinking
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # Count tool_calls
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # Metadata overhead per message (approximately 4 tokens)
            total_tokens += 4

        return total_tokens

    def _estimate_tokens_fallback(self) -> int:
        """Fallback token estimation method (when tiktoken is unavailable)"""
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

        # Rough estimation: average 2.5 characters = 1 token
        return int(total_chars / 2.5)

    async def _summarize_messages(self):
        """Message history summarization: summarize conversations between user messages when tokens exceed limit

        Strategy (Agent mode):
        - Keep all user messages (these are user intents)
        - Summarize content between each user-user pair (agent execution process)
        - If last round is still executing (has agent/tool messages but no next user), also summarize
        - Structure: system -> user1 -> summary1 -> user2 -> summary2 -> user3 -> summary3 (if executing)

        Summary is triggered when EITHER:
        - Local token estimation exceeds limit
        - API reported total_tokens exceeds limit
        """
        # Skip check if we just completed a summary (wait for next LLM call to update api_total_tokens)
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated_tokens = self._estimate_tokens()

        # Check both local estimation and API reported tokens
        should_summarize = (
            estimated_tokens > self.token_limit
            or self.api_total_tokens > self.token_limit
        )

        # If neither exceeded, no summary needed
        if not should_summarize:
            return

        print(
            f"\n{Colors.BRIGHT_YELLOW}📊 Token usage - Local estimate: {estimated_tokens}, API reported: {self.api_total_tokens}, Limit: {self.token_limit}{Colors.RESET}"
        )
        print(
            f"{Colors.BRIGHT_YELLOW}🔄 Triggering message history summarization...{Colors.RESET}"
        )

        # Find all user message indices (skip system prompt)
        user_indices = [
            i for i, msg in enumerate(self.messages) if msg.role == "user" and i > 0
        ]

        # Need at least 1 user message to perform summary
        if len(user_indices) < 1:
            print(
                f"{Colors.BRIGHT_YELLOW}⚠️  Insufficient messages, cannot summarize{Colors.RESET}"
            )
            return

        # Build new message list
        new_messages = [self.messages[0]]  # Keep system prompt
        summary_count = 0

        # Iterate through each user message and summarize the execution process after it
        for i, user_idx in enumerate(user_indices):
            # Add current user message
            new_messages.append(self.messages[user_idx])

            # Determine message range to summarize
            # If last user, go to end of message list; otherwise to before next user
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self.messages)

            # Extract execution messages for this round
            execution_messages = self.messages[user_idx + 1 : next_user_idx]

            # If there are execution messages in this round, summarize them
            if execution_messages:
                summary_text = await self._create_summary(execution_messages, i + 1)
                if summary_text:
                    summary_message = Message(
                        role="user",
                        content=f"[Assistant Execution Summary]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # Replace message list
        self.messages = new_messages

        # Skip next token check to avoid consecutive summary triggers
        # (api_total_tokens will be updated after next LLM call)
        self._skip_next_token_check = True

        new_tokens = self._estimate_tokens()
        print(
            f"{Colors.BRIGHT_GREEN}✓ Summary completed, local tokens: {estimated_tokens} → {new_tokens}{Colors.RESET}"
        )
        print(
            f"{Colors.DIM}  Structure: system + {len(user_indices)} user messages + {summary_count} summaries{Colors.RESET}"
        )
        print(
            f"{Colors.DIM}  Note: API token count will update on next LLM call{Colors.RESET}"
        )

    async def _create_summary(self, messages: list[Message], round_num: int) -> str:
        """Create summary for one execution round

        Args:
            messages: List of messages to summarize
            round_num: Round number

        Returns:
            Summary text
        """
        if not messages:
            return ""

        # Build summary content
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

        # Call LLM to generate concise summary
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

            # Track summarization token usage
            if response.usage:
                self.cumulative_total_tokens += response.usage.total_tokens
                self.cumulative_prompt_tokens += response.usage.prompt_tokens
                self.cumulative_completion_tokens += response.usage.completion_tokens
                self.cumulative_cache_hit_tokens += response.usage.cache_hit_tokens
                self.cumulative_cache_miss_tokens += response.usage.cache_miss_tokens

            summary_text = response.content
            print(
                f"{Colors.BRIGHT_GREEN}✓ Summary for round {round_num} generated successfully{Colors.RESET}"
            )
            return summary_text

        except Exception as e:
            print(
                f"{Colors.BRIGHT_RED}✗ Summary generation failed for round {round_num}: {e}{Colors.RESET}"
            )
            # Use simple text summary on failure
            return summary_content

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        step_callback: Optional[Callable[..., Awaitable[None]]] = None,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> RunResult:
        """Execute agent loop until task is complete or max steps reached.

        Args:
            cancel_event: Optional asyncio.Event that can be set to cancel execution.
                          When set, the agent will stop at the next safe checkpoint
                          (after completing the current step to keep messages consistent).
            step_callback: Optional async callback invoked at each step with event type
                           and keyword arguments. Four event types:
                           - "llm_request"(messages, tools, step_index): before LLM call
                           - "llm_response"(content, tool_calls): after LLM returns
                           - "tool_start"(tool_name, arguments): before tool execution
                           - "tool_result"(tool_name, success, content): after tool returns
                           Exceptions in the callback are logged and do not propagate.
            task_id: Optional task identifier (worker logger already has it from __init__).
            context_id: Optional context identifier (worker logger already has it from __init__).

        Returns:
            RunResult with the final response content, success flag, and steps used.
        """
        # Set cancellation event (can also be set via self.cancel_event before calling run())
        if cancel_event is not None:
            self.cancel_event = cancel_event

        # Allow runtime override of task_id/context_id if provided.
        if task_id is not None or context_id is not None:
            self.logger.set_task_context(
                task_id=task_id or self.logger._task_id,
                context_id=context_id or self.logger._context_id,
            )

        print(
            f"{Colors.DIM}📝 Log file: {self.logger.get_log_file_path()}{Colors.RESET}"
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
            # Check for cancellation at start of each step
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                # Cancel checkpoint: write a status=cancelled terminal marker
                # (historical gap: TASK_CANCEL returned with zero NDJSON events)
                self.logger.log_abort(
                    status="cancelled", step_index=step, content=cancel_msg
                )
                result = RunResult(content=cancel_msg, success=None, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # Hooks can signal early termination (e.g. task_complete set by a tool)
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

            # Get tool list for LLM call
            tool_list = list(self.tools.values())

            # Allow hooks to rewrite messages sent to the LLM
            messages_for_llm = self.messages
            if self.hooks is not None:
                messages_for_llm = await self.hooks.pre_llm(self, self.messages)

            # Log LLM request and call LLM with Tool objects directly
            self.logger.log_request(
                messages=messages_for_llm, tools=tool_list, step_index=step
            )
            # Emit llm_request so the main trace (TaskLogger via the
            # coordinator sink) can rebuild the LLM timeline.
            if step_callback is not None:
                try:
                    res = step_callback(
                        "llm_request",
                        messages=messages_for_llm,
                        tools=tool_list,
                        step_index=step,
                    )
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.exception("step_callback(llm_request) failed")

            # Race the in-flight request against cancel_event: cancels on set,
            # eliminating hung LLM requests
            self._active_request_step = step
            try:
                response = await self._llm_generate_cancellable(
                    messages_for_llm, tool_list
                )
            except _RequestCancelled:
                # Cancelled while the request was in flight: write a
                # status=cancelled terminal marker and return
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                self.logger.log_abort(
                    status="cancelled",
                    step_index=self._active_request_step,
                    content=cancel_msg,
                )
                self._active_request_step = None
                result = RunResult(content=cancel_msg, success=None, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result
            except Exception as e:
                # Check if it's a retry exhausted error
                from .retry import RetryExhaustedError

                if isinstance(e, RetryExhaustedError):
                    error_msg = f"LLM call failed after {e.attempts} retries\nLast error: {str(e.last_exception)}"
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ Retry failed:{Colors.RESET} {error_msg}"
                    )
                else:
                    error_msg = f"LLM call failed: {str(e)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {error_msg}")
                # Failure paths never reach the post-LLM callback, so emit a
                # zero-usage llm_response marker event to keep token_usage
                # rows aligned with llm_request rows.
                if step_callback is not None:
                    try:
                        res = step_callback(
                            "llm_response",
                            content=error_msg,
                            tool_calls=[],
                            usage=None,
                            status="error",
                        )
                        if inspect.isawaitable(res):
                            await res
                    except Exception:
                        logger.exception("step_callback(llm_response on error) failed")
                # NDJSON terminal marker: the LLM exception path historically
                # wrote only CSV zero-value rows, no NDJSON events
                self.logger.log_abort(
                    status="error",
                    step_index=self._active_request_step,
                    content=error_msg,
                )
                self._active_request_step = None
                result = RunResult(content=error_msg, success=False, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result
            except BaseException:
                # CancelledError / process-level interruption escaped: write a
                # status=aborted marker within the in-flight window and re-raise
                # (run() finally fallback semantics)
                self.logger.log_abort(
                    status="aborted",
                    step_index=self._active_request_step,
                    content="Agent run interrupted with in-flight LLM request",
                )
                self._active_request_step = None
                raise
            else:
                # Request completed normally
                self._active_request_step = None

            # Accumulate API reported token usage
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

            # Log LLM response
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
                step_index=step,
            )

            # Add assistant message
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_msg)

            # Notify step callback about LLM response
            if step_callback is not None:
                try:
                    res = step_callback(
                        "llm_response",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        usage=response.usage,
                        input_messages=messages_for_llm,
                    )
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.exception("step_callback(llm_response) failed")

            # Print thinking if present
            if response.thinking:
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

            # Print assistant response
            if response.content:
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{response.content}")

            # Check if task is complete (no tool calls)
            if not response.tool_calls:
                step_elapsed = perf_counter() - step_start_time
                total_elapsed = perf_counter() - run_start_time
                print(
                    f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}"
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
                # Defensive fallback: exit on plain text when not requiring completion
                result = RunResult(
                    content=response.content, success=None, steps_used=step + 1
                )
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # Check for cancellation before executing tools
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                # Cancel checkpoint: write a status=cancelled terminal marker
                # (historical gap: TASK_CANCEL returned with zero NDJSON events)
                self.logger.log_abort(
                    status="cancelled", step_index=step, content=cancel_msg
                )
                result = RunResult(content=cancel_msg, success=None, steps_used=step)
                if self.hooks is not None:
                    await self.hooks.on_run_end(self, result)
                return result

            # Execute tool calls
            for tool_call_idx, tool_call in enumerate(response.tool_calls):
                tool_call_id = tool_call.id
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments

                # Tool call header
                print(
                    f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}"
                )

                # Arguments (formatted display)
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                # Truncate each argument value to avoid overly long output
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

                # Notify step callback about tool start
                if step_callback is not None:
                    try:
                        res = step_callback(
                            "tool_start",
                            tool_name=function_name,
                            arguments=arguments,
                        )
                        if inspect.isawaitable(res):
                            await res
                    except Exception:
                        logger.exception("step_callback(tool_start) failed")

                # Log tool start to NDJSON before execution (timeline rebuild)
                self.logger.log_tool_start(
                    tool_name=function_name,
                    arguments=arguments,
                )

                # Allow hooks to rewrite tool arguments
                if self.hooks is not None:
                    arguments = await self.hooks.pre_tool(
                        self, function_name, arguments
                    )

                # Execute tool
                if function_name not in self.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )
                else:
                    try:
                        tool = self.tools[function_name]
                        result = await tool.execute(**arguments)
                    except NeedInputError as e:
                        if step_callback is not None:
                            try:
                                res = step_callback(
                                    "tool_result",
                                    tool_name=function_name,
                                    success=True,
                                    content=e.question,
                                )
                                if inspect.isawaitable(res):
                                    await res
                            except Exception:
                                logger.exception(
                                    "step_callback(tool_result for NeedInputError) failed"
                                )
                        self.logger.log_tool_result(
                            tool_name=function_name,
                            arguments=arguments,
                            success=True,
                            result=e.question,
                            step_index=step,
                        )
                        # Any tool_calls after this one in the same assistant
                        # turn never ran. They still need a "tool" message —
                        # otherwise the next API call has tool_use entries
                        # with no matching tool_result and gets rejected as
                        # malformed. Backfill placeholders; the raising call
                        # itself is answered later by the resumed session.
                        for skipped in response.tool_calls[tool_call_idx + 1 :]:
                            self.messages.append(
                                Message(
                                    role="tool",
                                    content="Skipped: an earlier tool call in this turn requires coordinator input first.",
                                    tool_call_id=skipped.id,
                                    name=skipped.function.name,
                                )
                            )
                        return RunResult(
                            content=e.question,
                            success=False,
                            need_input=True,
                        )
                    except Exception as e:
                        # Catch all exceptions during tool execution, convert to failed ToolResult
                        import traceback

                        error_detail = f"{type(e).__name__}: {str(e)}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
                        )

                # Allow hooks to observe / rewrite tool results
                if self.hooks is not None:
                    result = await self.hooks.post_tool(self, function_name, result)

                # Phase 5: public error_code derived from the structured error
                # BEFORE redaction; never parsed from `content`.
                error_code = error_code_for_result(result)

                # Redact before any sink: context / logger / step callback / A2A
                # never receive the raw error text of a failed ToolResult.
                safe_result = _REDACTOR.redact_tool_result(result)

                # Log tool execution result (only the public error_code of a
                # failed ToolResult may enter the logger, never the raw error).
                self.logger.log_tool_result(
                    tool_name=function_name,
                    arguments=arguments,
                    success=safe_result.success,
                    result=safe_result.content if safe_result.success else "",
                    error=error_code if not safe_result.success else "",
                    step_index=step,
                )

                # Print result
                if safe_result.success:
                    result_text = safe_result.content
                    if len(result_text) > 300:
                        result_text = (
                            result_text[:300] + f"{Colors.DIM}...{Colors.RESET}"
                        )
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {result_text}")
                else:
                    print(
                        f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} "
                        f"{Colors.RED}{error_code}{Colors.RESET}"
                    )

                # Add tool result message (raw error text never enters context;
                # only the public error_code is surfaced for failed tools).
                tool_msg = Message(
                    role="tool",
                    content=safe_result.content
                    if safe_result.success
                    else f"Error: {error_code}",
                    tool_call_id=tool_call_id,
                    name=function_name,
                )
                self.messages.append(tool_msg)

                # Notify step callback about tool result (pass error_code along)
                if step_callback is not None:
                    try:
                        result_text = (
                            safe_result.content
                            if safe_result.success
                            else f"Error: {error_code}"
                        )
                        res = step_callback(
                            "tool_result",
                            tool_name=function_name,
                            success=safe_result.success,
                            content=result_text,
                            data=safe_result.data,
                            error_code=error_code,
                        )
                        if inspect.isawaitable(res):
                            await res
                    except Exception:
                        logger.exception("step_callback(tool_result) failed")

                # Check for cancellation after each tool execution
                if self._check_cancelled():
                    self._cleanup_incomplete_messages()
                    cancel_msg = "Task cancelled by user."
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                    # Cancel checkpoint: write a status=cancelled terminal marker
                    # (historical gap: TASK_CANCEL returned with zero NDJSON events)
                    self.logger.log_abort(
                        status="cancelled", step_index=step, content=cancel_msg
                    )
                    result = RunResult(
                        content=cancel_msg, success=None, steps_used=step
                    )
                    if self.hooks is not None:
                        await self.hooks.on_run_end(self, result)
                    return result

            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            print(
                f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}"
            )

            if step_callback is not None:
                try:
                    res = step_callback(
                        "step_boundary",
                        step=step + 1,
                        max_steps=self.max_steps,
                        elapsed=step_elapsed,
                    )
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.exception("step_callback(step_boundary) failed")

            step += 1

        # Max steps reached
        error_msg = f"Task couldn't be completed after {self.max_steps} steps."
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
        result = RunResult(content=error_msg, success=False, steps_used=self.max_steps)
        if self.hooks is not None:
            await self.hooks.on_run_end(self, result)
        return result

    def _inject_continue_nudge(self) -> None:
        """Inject a user message prompting the agent to continue or finish."""
        nudge = (
            "You have not yet marked the task as complete. "
            "Please continue working toward the goal. "
            "When the task is finished, call the finish_task tool."
        )
        self.messages.append(Message(role="user", content=nudge))
        print(f"{Colors.YELLOW}🔔 {nudge}{Colors.RESET}")

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()

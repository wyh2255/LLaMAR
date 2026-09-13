"""Agent run logger"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Message, ToolCall

from Agent.redaction import SensitiveTextRedactor

# Defensive boundary: NDJSON output never carries raw secrets even if a caller
# bypassed the agent-side redaction step.
_REDACTOR = SensitiveTextRedactor()


class AgentLogger:
    """Agent run logger

    Responsible for recording the complete interaction process of each agent run, including:
    - LLM requests and responses
    - Tool calls and results

    Output format: NDJSON (one JSON object per line), consistent with
    ``Agent.worker_agent.logger.AgentLogger``.
    """

    def __init__(
        self,
        log_dir: str | Path | None = None,
        task_id: str = "",
        context_id: str = "",
    ):
        """Initialize logger

        Args:
            log_dir: Directory for log files. Defaults to ../logs/agent/ relative to cwd.
            task_id: Task identifier (used for filename).
            context_id: Context identifier (included in each log entry).
        """
        if log_dir is not None:
            self._log_dir = Path(log_dir)
        else:
            self._log_dir = Path.cwd().parent / "logs" / "agent"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._task_id = task_id or "unnamed_task"
        self._context_id = context_id
        self._ndjson_fh: Any = None
        self._fh_opened = False

    def set_task_context(self, task_id: str, context_id: str) -> None:
        """Set task_id/context_id before starting a new run."""
        self._task_id = task_id or self._task_id
        self._context_id = context_id

    def start_new_run(self, task_id: str = "", context_id: str = "") -> None:
        """Start new run, close any previous file and open a new NDJSON file."""
        self.close()
        if task_id:
            self._task_id = task_id
        if context_id:
            self._context_id = context_id
        self._fh_opened = False

    def _ensure_file_open(self):
        """Open NDJSON file on first write"""
        if self._fh_opened:
            return
        log_filename = f"{self._task_id}.ndjson"
        log_file = self._log_dir / log_filename
        self._ndjson_fh = open(log_file, "a", encoding="utf-8")
        self._fh_opened = True

    def _write_ndjson(self, entry: dict):
        """Write one NDJSON line

        Args:
            entry: Dictionary to serialize as NDJSON
        """
        self._ensure_file_open()
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        self._ndjson_fh.write(line)
        self._ndjson_fh.flush()

    def _base_entry(self, event: str) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "context_id": self._context_id,
            "event": event,
        }

    def log_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        step_index: int = 0,
    ):
        """Log LLM request

        Args:
            messages: Message list
            tools: Tool list (optional)
            step_index: Step index
        """
        entry = self._base_entry("llm_request")

        entry["messages"] = []
        for msg in messages:
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
            }
            if msg.thinking:
                msg_dict["thinking"] = msg.thinking
            if msg.tool_calls:
                msg_dict["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name

            entry["messages"].append(msg_dict)

        entry["tools"] = [tool.name for tool in tools] if tools else []
        entry["step_index"] = step_index

        self._write_ndjson(entry)

    def log_response(
        self,
        content: str,
        thinking: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str | None = None,
        usage: dict | None = None,
        step_index: int = 0,
    ):
        """Log LLM response

        Args:
            content: Response content
            thinking: Thinking content (optional)
            tool_calls: Tool call list (optional)
            finish_reason: Finish reason (optional)
            usage: Token usage dict (optional)
            step_index: Step index
        """
        entry = self._base_entry("llm_response")
        entry["content"] = _REDACTOR.redact(content)
        entry["step_index"] = step_index

        if thinking:
            entry["thinking"] = _REDACTOR.redact(thinking)

        if tool_calls:
            entry["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        if finish_reason:
            entry["finish_reason"] = finish_reason

        if usage is not None:
            entry["usage"] = usage

        self._write_ndjson(entry)

    def log_abort(
        self,
        status: str,
        step_index: int = 0,
        content: str = "",
    ):
        """Log an aborted LLM request as a terminal marker.

        Reuses the ``llm_response`` event schema plus a ``status`` field
        (``aborted`` / ``cancelled`` / ``error``) so downstream parsers that
        understand llm_response stay schema-compatible while the trace gains an
        explicit termination record for requests that never produced a response.

        Args:
            status: Termination reason: aborted / cancelled / error
            step_index: Step index of the interrupted request (or checkpoint)
            content: Short human-readable reason (optional)
        """
        entry = self._base_entry("llm_response")
        entry["status"] = status
        entry["step_index"] = step_index
        entry["content"] = _REDACTOR.redact(content or f"Request {status}")

        self._write_ndjson(entry)

    def log_tool_start(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ):
        """Log tool execution start

        Args:
            tool_name: Tool name
            arguments: Tool arguments (summary of the incoming call)
        """
        entry = self._base_entry("tool_start")
        entry["tool_name"] = tool_name
        entry["arguments"] = _REDACTOR.redact_data(arguments)

        self._write_ndjson(entry)

    def log_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        result: str = "",
        error: str = "",
        step_index: int = 0,
    ):
        """Log tool execution result

        Args:
            tool_name: Tool name
            arguments: Tool arguments
            success: Whether successful
            result: Result content (on success)
            error: Error message (on failure)
            step_index: Step index
        """
        entry = self._base_entry("tool_result")
        entry["tool_name"] = tool_name
        entry["arguments"] = _REDACTOR.redact_data(arguments)
        entry["success"] = success
        entry["result"] = _REDACTOR.redact(result)
        entry["error"] = _REDACTOR.redact(error)
        entry["step_index"] = step_index

        self._write_ndjson(entry)

    def close(self) -> None:
        """Close the NDJSON file handle."""
        if self._ndjson_fh is not None:
            try:
                self._ndjson_fh.close()
            except OSError:
                pass
            self._ndjson_fh = None
            self._fh_opened = False

    def __del__(self) -> None:
        self.close()

    def get_log_file_path(self) -> Path | None:
        """Get current log file path"""
        if self._ndjson_fh is None:
            return None
        return Path(self._ndjson_fh.name)

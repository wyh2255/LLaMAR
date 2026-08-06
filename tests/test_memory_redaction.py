"""Phase 2 redaction contracts: SensitiveTextRedactor + RedactionPolicy.

Both the backend-independent ``SensitiveTextRedactor`` (``src/Agent/redaction.py``)
and the Coordinator ``RedactionPolicy`` (``src/a2a/coordinator/memory/redaction.py``)
must replace secret / HMAC / proof / Authorization / Cookie / credential /
mailbox-body originals with ``[REDACTED:<kind>:<sha256-prefix>]`` before the
value enters any sink.  Non-sensitive structure must be preserved.
"""

from __future__ import annotations

import json

import pytest

from Agent.redaction import SensitiveTextRedactor
from a2a.coordinator.memory.redaction import RedactionPolicy

SECRET = "SUPERSECRET_VALUE_9f2c1"
HMAC_HEX = "a" * 64
COOKIE = "session=abc123def456"


def test_redact_exact_secret_in_text():
    redactor = SensitiveTextRedactor(secrets=[SECRET.encode("utf-8")])
    out = redactor.redact(f"the token is {SECRET} and nothing else")
    assert SECRET not in out
    assert "[REDACTED:secret:" in out


def test_redact_hmac_hex():
    redactor = SensitiveTextRedactor()
    out = redactor.redact(f"sig={HMAC_HEX}")
    assert HMAC_HEX not in out
    assert "[REDACTED:hmac:" in out


def test_redact_authorization_and_cookie():
    redactor = SensitiveTextRedactor()
    text = "Authorization: Bearer hunter2token\nCookie: " + COOKIE
    out = redactor.redact(text)
    assert "hunter2token" not in out
    assert COOKIE not in out
    assert "Authorization" in out  # header name preserved


def test_redact_api_key_pattern():
    redactor = SensitiveTextRedactor()
    out = redactor.redact("api_key=sk_live_12345abcdef")
    assert "sk_live_12345abcdef" not in out


def test_redact_callback_proof_header():
    redactor = SensitiveTextRedactor()
    proof = "VGhpc0lzQVRlc3RQcm9vZlRva2VuMTIzNDU2Nzg5MEFCQ0RFRg=="
    out = redactor.redact(f"X-A2A-Callback-Proof: {proof}")
    assert proof not in out
    assert "[REDACTED:proof:" in out


def test_redact_data_recursively():
    redactor = SensitiveTextRedactor(secrets=[SECRET.encode("utf-8")])
    payload = {
        "reporter": "Alice",
        "step": 3,
        "position": [1, 2, 0],
        "secret": SECRET,
        "mail_body": "mailbox hello world",
        "mail_body_text": "second mailbox body",
        "nested": {
            "hmac": HMAC_HEX,
            "tags": ["ok", "keep"],
        },
    }
    out = redactor.redact_data(payload)
    assert out["reporter"] == "Alice"
    assert out["step"] == 3
    assert out["position"] == [1, 2, 0]
    serialized = json.dumps(out)
    assert SECRET not in serialized
    assert HMAC_HEX not in serialized
    assert "mailbox hello world" not in serialized
    assert "second mailbox body" not in serialized
    assert out["nested"]["tags"] == ["ok", "keep"]


def test_redact_tool_result_content_error_and_data():
    redactor = SensitiveTextRedactor(secrets=[SECRET.encode("utf-8")])

    class _FakeResult:
        success = False
        content = ""
        error = f"failed with {SECRET}"
        data = {"secret": SECRET, "nested": {"hmac": HMAC_HEX}, "keep": 1}

        def model_copy(self, update=None):
            import copy

            obj = copy.copy(self)
            for key, value in (update or {}).items():
                setattr(obj, key, value)
            return obj

    safe = redactor.redact_tool_result(_FakeResult())
    assert safe.error is not None
    assert SECRET not in safe.error
    serialized = json.dumps(safe.data)
    assert SECRET not in serialized
    assert HMAC_HEX not in serialized
    assert safe.data["keep"] == 1
    # Original object must never be mutated.
    assert SECRET in _FakeResult().error


def test_redaction_policy_sanitize_callback_preserves_structure():
    secret_bytes = b"coordinator-secret-0123456789abcdef"
    policy = RedactionPolicy(secret=secret_bytes)
    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-1",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {
                    "parts": [
                        {"text": "Authorization: Bearer abcdef0123456789 all good"}
                    ]
                },
            },
        }
    }
    out = policy.sanitize_callback(payload)
    assert out["statusUpdate"]["taskId"] == "worker-1"
    assert out["statusUpdate"]["contextId"] == "ctx-1"
    serialized = json.dumps(out)
    assert "abcdef0123456789" not in serialized
    assert "[REDACTED:authorization:" in serialized


def test_redaction_policy_sanitize_event_defensive_boundary():
    policy = RedactionPolicy()
    text = "Authorization: Bearer zzzzz yyy hmac={} mail=hi".format(HMAC_HEX)
    out = policy.sanitize_event(text)
    assert HMAC_HEX not in out


@pytest.mark.asyncio
async def test_router_agent_failed_tool_result_redacted(tmp_path):
    from Agent.router_agent.agent import Agent
    from Agent.router_agent.schema import FunctionCall, LLMResponse, ToolCall
    from Agent.router_agent.tools.base import Tool, ToolResult

    SECRET_TEXT = "hunter2-supersecret-error"
    ERROR = f"boom: Authorization: Bearer {SECRET_TEXT} hmac={HMAC_HEX}"

    class _BadTool(Tool):
        name = "bad_tool"
        description = "bad"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=False, content="", error=ERROR)

    class _LLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        type="function",
                        function=FunctionCall(name="bad_tool", arguments={}),
                    )
                ],
                finish_reason="tool_calls",
            )

    captured: dict = {}

    async def step_cb(type_, **data):
        if type_ == "tool_result":
            captured["content"] = data.get("content", "")

    agent = Agent(
        llm_client=_LLM(),
        system_prompt="test",
        tools=[_BadTool()],
        workspace_dir=str(tmp_path),
        max_steps=1,
        log_dir=str(tmp_path / "logs"),
    )
    await agent.run(step_callback=step_cb)

    history_text = json.dumps([m.content for m in agent.messages])
    assert SECRET_TEXT not in history_text
    assert HMAC_HEX not in history_text
    assert SECRET_TEXT not in (captured.get("content") or "")

    log_file = agent.logger.get_log_file_path()
    if log_file is not None:
        assert SECRET_TEXT not in log_file.read_text(encoding="utf-8")
        assert HMAC_HEX not in log_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_agent_failed_tool_result_redacted(tmp_path):
    from Agent.worker_agent.agent import Agent
    from Agent.worker_agent.schema import FunctionCall, LLMResponse, ToolCall
    from Agent.worker_agent.tools.base import Tool, ToolResult

    SECRET_TEXT = "worker-hunter2-supersecret-error"
    ERROR = f"boom: Authorization: Bearer {SECRET_TEXT} hmac={HMAC_HEX}"

    class _BadTool(Tool):
        name = "bad_tool"
        description = "bad"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=False, content="", error=ERROR)

    class _LLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        type="function",
                        function=FunctionCall(name="bad_tool", arguments={}),
                    )
                ],
                finish_reason="tool_calls",
            )

    captured: dict = {}

    async def step_cb(type_, **data):
        if type_ == "tool_result":
            captured["content"] = data.get("content", "")
            captured["data"] = data.get("data")

    agent = Agent(
        llm_client=_LLM(),
        system_prompt="test",
        tools=[_BadTool()],
        workspace_dir=str(tmp_path),
        max_steps=1,
        log_dir=str(tmp_path / "logs"),
    )
    await agent.run(step_callback=step_cb)

    history_text = json.dumps([m.content for m in agent.messages])
    assert SECRET_TEXT not in history_text
    assert HMAC_HEX not in history_text
    assert SECRET_TEXT not in (captured.get("content") or "")

    log_file = agent.logger.get_log_file_path()
    if log_file is not None:
        assert SECRET_TEXT not in log_file.read_text(encoding="utf-8")
        assert HMAC_HEX not in log_file.read_text(encoding="utf-8")


def test_sink_redacts_before_a2a_enqueue():
    """A2AWorkerSink must redact [DATA] content before enqueueing a status event."""
    import asyncio

    from a2a.worker.sink import A2AWorkerSink

    captured: list = []

    class _Queue:
        async def enqueue_event(self, event):
            captured.append(event)

    sink = A2AWorkerSink(event_queue=_Queue(), task_id="t", context_id="ctx")

    asyncio.run(
        sink.emit(
            "tool_result",
            tool_name="bad_tool",
            success=False,
            content=f"[Error] Authorization: Bearer {SECRET} hmac={HMAC_HEX}",
            data={"secret": SECRET, "hmac": HMAC_HEX, "keep": 2},
        )
    )
    assert captured, "enqueue_event must be called"
    text = captured[0].status.message.parts[0].text
    assert SECRET not in text
    assert HMAC_HEX not in text
    assert "keep" in text

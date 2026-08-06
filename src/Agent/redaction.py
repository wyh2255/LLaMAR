"""Backend-independent sensitive text redaction (Phase 2).

``SensitiveTextRedactor`` is intentionally free of any ``a2a`` or ``sar_orch``
dependency: it is imported by both router/worker agents (failed ``ToolResult``
sinks) and by the Coordinator ``RedactionPolicy`` (callback fan-out).  It
replaces exact secret strings and pattern-matched sensitive values with
``[REDACTED:<kind>:<sha256-prefix>]`` placeholders and never logs or returns the
original value.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = ["REDACTED_PREFIX", "SensitiveTextRedactor", "redacted_marker"]


REDACTED_PREFIX = "[REDACTED:"


def redacted_marker(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{REDACTED_PREFIX}{kind}:{digest}]"


# Sensitive dictionary keys: the value is replaced wholesale with a redaction
# marker (the key itself is kept so structural fields survive).
_SENSITIVE_KEY_KINDS: dict[str, str] = {
    "authorization": "authorization",
    "authorization_header": "authorization",
    "authorization_value": "authorization",
    "cookie": "cookie",
    "cookies": "cookie",
    "credentials": "credential",
    "credential": "credential",
    "password": "credential",
    "passwd": "credential",
    "client_secret": "secret",
    "shared_secret": "secret",
    "secret": "secret",
    "api_key": "api_key",
    "apikey": "api_key",
    "api-key": "api_key",
    "access_token": "credential",
    "refresh_token": "credential",
    "bearer_token": "credential",
    "hmac": "hmac",
    "hmac_signature": "hmac",
    "signature": "hmac",
    "proof": "proof",
    "callback_proof": "proof",
    "signed_envelope": "envelope",
    "mail_body": "mailbox",
    "mail_body_text": "mailbox",
    "mail_body_content": "mailbox",
    "mailbox_body": "mailbox",
    "message_body": "mailbox",
}


# Pattern list: (kind, compiled regex with named group ``value``).  Only the
# matched sensitive value is replaced; surrounding text is preserved.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "proof",
        re.compile(r"(?i)(X-A2A-Callback-Proof\s*[:=]\s*)(?P<value>\S+)"),
    ),
    (
        "authorization",
        re.compile(r"(?i)(Authorization\s*[:=]\s*Bearer\s+)(?P<value>\S+)"),
    ),
    (
        "authorization",
        re.compile(r"(?i)(Authorization\s*[:=]\s*Basic\s+)(?P<value>\S+)"),
    ),
    ("cookie", re.compile(r"(?i)(Cookie\s*[:=]\s*)(?P<value>[^;\n]+)")),
    (
        "api_key",
        re.compile(r"(?i)((?:api[_-]?key|apikey)\s*[:=]\s*)(?P<value>\S+)"),
    ),
    (
        "credential",
        re.compile(
            r"(?i)((?:password|passwd|secret|client_secret|credential|"
            r"access_token|refresh_token|private_key)\s*[:=]\s*)(?P<value>\S+)"
        ),
    ),
    ("hmac", re.compile(r"\b(?P<value>[a-f0-9]{64})\b")),
]


class SensitiveTextRedactor:
    """Deterministic sensitive-text redaction engine.

    ``secrets`` are exact byte/str values that are always replaced (this is the
    mechanism used to bind a specific shared coordinator secret).  ``extra``
    allows callers to add raw regex patterns.  Pattern matching is
    best-effort and never throws.
    """

    def __init__(
        self,
        secrets: list[bytes | str] | tuple[bytes | str, ...] | None = None,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        self._secrets: list[str] = [
            s.decode("utf-8", errors="replace") if isinstance(s, bytes) else str(s)
            for s in (secrets or ())
        ]
        self._secrets = [s for s in self._secrets if s]
        self._patterns = list(_PATTERNS)
        for kind, raw in extra or ():
            try:
                self._patterns.append((kind, re.compile(raw)))
            except re.error:
                continue

    def redact(self, text: str | None) -> str:
        """Redact one text value; returns the safe string."""
        if not text:
            return text or ""
        for kind, pattern in self._patterns:
            text = pattern.sub(
                lambda m: (
                    m.group(0)[: m.start("value") - m.start(0)]
                    + redacted_marker(kind, m.group("value"))
                ),
                text,
            )
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, redacted_marker("secret", secret))
        return text

    def redact_data(self, obj: Any) -> Any:
        """Deep-copy ``obj`` with all sensitive strings/keys redacted."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, (list, tuple)):
            return [self.redact_data(item) for item in obj]
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                kind = _SENSITIVE_KEY_KINDS.get(str(key).lower())
                if kind is not None:
                    out[key] = redacted_marker(kind, str(value))
                else:
                    out[key] = self.redact_data(value)
            return out
        return obj

    def redact_tool_result(self, result: Any) -> Any:
        """Return a redacted copy of a ``ToolResult``-like object.

        ``content``, ``error`` and recursive ``data`` are redacted; the original
        object is never mutated.  ``success`` and other public fields survive.
        """
        content = (
            self.redact(result.content)
            if result.content is not None
            else result.content
        )
        error = self.redact(result.error) if result.error is not None else result.error
        data = self.redact_data(result.data) if result.data is not None else result.data
        if hasattr(result, "model_copy"):
            return result.model_copy(
                update={"content": content, "error": error, "data": data}
            )
        from dataclasses import replace

        if hasattr(result, "__dataclass_fields__"):
            return replace(result, content=content, error=error, data=data)
        result.content = content
        result.error = error
        result.data = data
        return result

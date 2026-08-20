"""Coordinator RedactionPolicy — shared sanitizer for authenticated fan-out.

Every authenticated callback payload must be sanitized before it fans out to
*all* writers (MemoryIngestor, legacy EventStore, SemanticMapStore JSONL,
TaskWatchdog, anomaly/security audit).  A valid signature never exempts a body
from redaction.  ``sanitize_event()`` is the defensive boundary that non-callback
producers (e.g. ``EventStore.append``, ``dispatch_task`` failure adapters) also
apply.
"""

from __future__ import annotations

from typing import Any

from Agent.redaction import SensitiveTextRedactor

__all__ = ["RedactionPolicy"]


class RedactionPolicy:
    """Coordinator-owned redaction policy wrapping ``SensitiveTextRedactor``."""

    def __init__(
        self,
        *,
        secret: bytes | None = None,
        redactor: SensitiveTextRedactor | None = None,
    ) -> None:
        self._redactor = redactor or SensitiveTextRedactor(
            secrets=[secret] if secret else ()
        )

    @property
    def redactor(self) -> SensitiveTextRedactor:
        return self._redactor

    def sanitize_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted deep copy of a parsed callback payload.

        Body digest / event identity / non-sensitive structural fields survive;
        secret / HMAC / proof / Authorization / Cookie / credential / mailbox
        body originals never do.
        """
        sanitized = self._redactor.redact_data(payload)
        return sanitized if isinstance(sanitized, dict) else dict(sanitized or {})

    def sanitize_event(self, text: str | None) -> str:
        """Defensive boundary for a single text field (legacy writers)."""
        return self._redactor.redact(text)

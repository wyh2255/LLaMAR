"""Thread-safe queue for user commands injected into the SAR coordinator.

The SAR console UI (sar_orch/console/) lets a human operator send free-form
instructions to the coordinator mid-run. Commands are queued here and drained
by ``SARCoordinatorStateProvider.snapshot()`` so they appear in the router's
Context Memory at the next ``pre_llm`` round.

Threading: the coordinator HTTP server (uvicorn thread) calls ``put()`` while
the router agent loop calls ``drain()`` — hence ``threading.Lock`` (ADR-011:
workers/coordinator cross threads, never asyncio primitives).
"""

from __future__ import annotations

import threading
import time


class UserCommandQueue:
    """Bounded, thread-safe FIFO of pending user commands."""

    def __init__(self, max_pending: int = 50) -> None:
        self._lock = threading.Lock()
        self._commands: list[dict] = []
        self._max_pending = max_pending

    def put(self, text: str, source: str = "user") -> dict:
        """Enqueue a command. Returns the stored command record."""
        record = {
            "text": text,
            "source": source,
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock:
            if len(self._commands) >= self._max_pending:
                self._commands.pop(0)
            self._commands.append(record)
        return record

    def drain(self) -> list[dict]:
        """Atomically remove and return all pending commands (FIFO order)."""
        with self._lock:
            commands = self._commands
            self._commands = []
        return commands

    def __len__(self) -> int:
        with self._lock:
            return len(self._commands)

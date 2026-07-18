"""BudgetLedger — token budget tracker for experiment rounds.

Tracks cumulative prompt/completion tokens across rounds and provides
a simple remaining/exhausted check.  Used by the coordinator state provider.
"""

from __future__ import annotations

from typing import Any


class BudgetLedger:
    """Track per-round and cumulative token usage against a cap.

    Args:
        max_tokens: Maximum total tokens (prompt + completion) allowed
            for the entire experiment.
    """

    def __init__(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
        self._max: int = max_tokens
        self._total_prompt: int = 0
        self._total_completion: int = 0
        self._rounds: list[dict[str, int]] = []

    def record_round(self, round_no: int, prompt_tokens: int, completion_tokens: int) -> None:
        """Record token usage for a completed round.

        Args:
            round_no: Round number (0-based or 1-based, caller's choice).
            prompt_tokens: Prompt tokens consumed in this round.
            completion_tokens: Completion tokens consumed in this round.
        """
        if prompt_tokens < 0:
            raise ValueError(f"prompt_tokens must be >= 0, got {prompt_tokens}")
        if completion_tokens < 0:
            raise ValueError(f"completion_tokens must be >= 0, got {completion_tokens}")
        self._total_prompt += prompt_tokens
        self._total_completion += completion_tokens
        self._rounds.append(
            {
                "round_no": round_no,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def remaining(self) -> int:
        """Return remaining token budget."""
        return max(0, self._max - self._total_prompt - self._total_completion)

    def exhausted(self) -> bool:
        """Return ``True`` when the budget cap has been reached or exceeded."""
        return self._total_prompt + self._total_completion >= self._max

    def total_used(self) -> int:
        """Return total tokens consumed so far."""
        return self._total_prompt + self._total_completion

    def summary(self) -> dict[str, Any]:
        """Return a dict summary of the ledger state."""
        return {
            "max_tokens": self._max,
            "total_prompt": self._total_prompt,
            "total_completion": self._total_completion,
            "total_used": self.total_used(),
            "remaining": self.remaining(),
            "exhausted": self.exhausted(),
            "rounds": list(self._rounds),
        }

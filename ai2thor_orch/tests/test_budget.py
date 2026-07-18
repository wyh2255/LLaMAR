"""Tests for BudgetLedger — cumulative tracking, remaining, exhausted boundary."""

from __future__ import annotations

import pytest

from ai2thor_orch.budget.ledger import BudgetLedger


class TestRecordRound:
    def test_accumulates_tokens(self):
        ledger = BudgetLedger(max_tokens=1000)
        ledger.record_round(0, prompt_tokens=100, completion_tokens=50)
        assert ledger.total_used() == 150
        assert ledger.remaining() == 850

    def test_multiple_rounds_cumulate(self):
        ledger = BudgetLedger(max_tokens=1000)
        ledger.record_round(0, prompt_tokens=100, completion_tokens=50)
        ledger.record_round(1, prompt_tokens=200, completion_tokens=100)
        assert ledger.total_used() == 450
        assert ledger.remaining() == 550

    def test_negative_tokens_raise(self):
        ledger = BudgetLedger(max_tokens=1000)
        with pytest.raises(ValueError):
            ledger.record_round(0, prompt_tokens=-1, completion_tokens=50)
        with pytest.raises(ValueError):
            ledger.record_round(0, prompt_tokens=10, completion_tokens=-5)


class TestRemaining:
    def test_initial_remaining_is_max(self):
        ledger = BudgetLedger(max_tokens=500)
        assert ledger.remaining() == 500

    def test_remaining_never_below_zero(self):
        ledger = BudgetLedger(max_tokens=100)
        ledger.record_round(0, prompt_tokens=200, completion_tokens=0)
        assert ledger.remaining() == 0


class TestExhausted:
    def test_not_exhausted_initially(self):
        ledger = BudgetLedger(max_tokens=1000)
        assert ledger.exhausted() is False

    def test_exhausted_at_boundary(self):
        ledger = BudgetLedger(max_tokens=100)
        ledger.record_round(0, prompt_tokens=100, completion_tokens=0)
        assert ledger.exhausted() is True

    def test_exhausted_over_boundary(self):
        ledger = BudgetLedger(max_tokens=100)
        ledger.record_round(0, prompt_tokens=60, completion_tokens=60)
        assert ledger.exhausted() is True

    def test_not_exhausted_below_boundary(self):
        ledger = BudgetLedger(max_tokens=100)
        ledger.record_round(0, prompt_tokens=50, completion_tokens=49)
        assert ledger.exhausted() is False


class TestSummary:
    def test_summary_keys(self):
        ledger = BudgetLedger(max_tokens=500)
        ledger.record_round(0, prompt_tokens=50, completion_tokens=50)
        s = ledger.summary()
        assert s["max_tokens"] == 500
        assert s["total_used"] == 100
        assert s["remaining"] == 400
        assert s["exhausted"] is False
        assert len(s["rounds"]) == 1
        assert s["rounds"][0]["round_no"] == 0

    def test_empty_summary(self):
        ledger = BudgetLedger(max_tokens=100)
        s = ledger.summary()
        assert s["total_used"] == 0
        assert s["rounds"] == []


class TestConstructor:
    def test_negative_max_tokens_raises(self):
        with pytest.raises(ValueError):
            BudgetLedger(max_tokens=0)
        with pytest.raises(ValueError):
            BudgetLedger(max_tokens=-1)

"""Tests for NeedInputError — Worker 暂停求助信号。"""

from a2a.worker.need_input import NeedInputError


def test_need_input_error_carries_question():
    err = NeedInputError("Where is the target?")
    assert err.question == "Where is the target?"


def test_need_input_error_is_exception():
    err = NeedInputError("Help me")
    assert isinstance(err, Exception)


def test_need_input_error_str():
    err = NeedInputError("What now?")
    assert "What now?" in str(err)

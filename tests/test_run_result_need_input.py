"""Tests for RunResult.need_input field."""

from Agent.worker_agent.schema import RunResult


def test_run_result_default_need_input_false():
    result = RunResult()
    assert result.need_input is False


def test_run_result_can_set_need_input():
    result = RunResult(success=False, content="Where is the target?", need_input=True)
    assert result.need_input is True
    assert result.content == "Where is the target?"

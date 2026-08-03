"""seed 支持性判定：必须靠行为探测，不能靠 HTTP 状态码。

实测（`packyapi.com/v1` + `deepseek-v4-flash`，2026-08-02）：

- 合法 seed → **200**，且**被静默忽略**（同 seed 两次输出不同）
- 越界 seed → 400 `expected u64`
- 两次请求 `system_fingerprint` 相同 → 不是后端路由差异
- `temperature=0.0` 下长确定性续写 4 次得到 4 个不同结果 → 温度同样不生效
  （对照：`17*23` 三次都是 `391`，证明请求与模型都正常）

DESIGN 1.1 原本规定"注入 seed 后收到 4xx 且响应体含 seed/unsupported/
unrecognized → 判定不支持"。该信号在此网关下**永远不会出现**，照它实现会让
`llm_seed_supported` 永久记 `true` —— 参数被接收、写进 metadata、但从不生效，
正是本项目反复发现的那类缺陷（E-1 / E-20 同形）。

故这里钉住三件事：判定来自行为、"未知"不等于"不支持"、探测失败不能中断 run。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sar_orch.experiment import probe_seed_support


class _FakeCompletions:
    def __init__(self, replies, record=None):
        self._replies = list(replies)
        self._record = record

    async def create(self, **kwargs):
        if self._record is not None:
            self._record.append(kwargs)
        text = self._replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )


def _patch_client(monkeypatch, replies, record=None):
    """Stand in for AsyncOpenAI at the import site inside probe_seed_support."""
    import openai

    class _FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(
                completions=_FakeCompletions(replies, record)
            )

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)


def _probe(**kw):
    base = dict(api_key="k", api_base="https://x/v1", model="m", seed=7)
    base.update(kw)
    return asyncio.run(probe_seed_support(**base))


class TestVerdictComesFromBehaviour:
    def test_identical_replies_mean_supported(self, monkeypatch):
        _patch_client(monkeypatch, ["same answer", "same answer"])
        assert _probe() is True

    def test_differing_replies_mean_ignored(self, monkeypatch):
        """这正是真实网关的行为：200 OK，但 seed 不约束采样。"""
        _patch_client(monkeypatch, ["answer one", "answer two"])
        assert _probe() is False

    def test_whitespace_only_difference_does_not_count_as_ignored(self, monkeypatch):
        """两端 strip 后比较 —— 尾随换行差异不是采样差异，否则会把"支持"
        误判成"忽略"。"""
        _patch_client(monkeypatch, ["  same answer\n", "same answer"])
        assert _probe() is True

    def test_it_actually_sends_the_seed(self, monkeypatch):
        """探测本身若没带 seed，结果毫无意义。"""
        seen: list[dict] = []
        _patch_client(monkeypatch, ["a", "a"], record=seen)
        _probe(seed=12345)
        assert len(seen) == 2
        assert all(c["seed"] == 12345 for c in seen)

    def test_probe_prompt_has_entropy_headroom(self, monkeypatch):
        """温度必须拉高：确定性 prompt 下两次回答本来就会相同，
        那样探测会从假象里得出"seed 有效"。"""
        seen: list[dict] = []
        _patch_client(monkeypatch, ["a", "a"], record=seen)
        _probe()
        assert all(c["temperature"] >= 1.0 for c in seen)


class TestUnknownIsNotUnsupported:
    def test_missing_api_key_returns_none(self):
        assert _probe(api_key="") is None

    def test_request_failure_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, [RuntimeError("connection reset"), "x"])
        assert _probe() is None

    def test_second_request_failure_also_returns_none(self, monkeypatch):
        """第一次成功、第二次失败时不能拿单个样本下结论。"""
        _patch_client(monkeypatch, ["first ok", RuntimeError("timeout")])
        assert _probe() is None

    def test_none_is_distinguishable_from_false(self, monkeypatch):
        """"测不出来"与"测出不支持"是两种状态。合并会让一次网络抖动
        伪装成一个发现。"""
        _patch_client(monkeypatch, ["a", "b"])
        determined = _probe()
        undetermined = _probe(api_key="")
        assert determined is False
        assert undetermined is None
        assert determined is not undetermined

    def test_probe_never_raises(self, monkeypatch):
        """探测失败不得中断实验 —— 它是元数据采集，不是实验的一部分。"""
        _patch_client(monkeypatch, [KeyboardInterrupt if False else Exception("boom"), "x"])
        assert _probe() is None


class TestMetadataSemantics:
    def test_unset_seed_records_none_not_false(self):
        """没请求 seed 时是"不适用"，不是"不支持"。若记 False，
        aggregate 的一致性检查会把"没用 seed 的批次"与"用了但不支持的批次"
        当成同一种情况。"""
        import inspect

        from sar_orch import experiment

        src = inspect.getsource(experiment.run_experiment)
        assert 'metadata["llm_seed_supported"] = None' in src

    def test_status_code_heuristic_is_gone(self):
        """确保没人把基于 4xx 的旧规则加回来。"""
        from pathlib import Path

        src = Path("sar_orch/experiment.py").read_text(encoding="utf-8")
        assert "probe_seed_support" in src
        # 旧规则的特征词不应作为判定依据出现在 seed 逻辑里
        seed_region = src[src.index("llm_seed_supported") - 2000 :]
        assert "unrecognized" not in seed_region.split("def build_run_metadata")[0]

    def test_ignored_seed_is_warned_about_loudly(self):
        """静默接受一个无效参数正是这类缺陷能存活的原因。"""
        from pathlib import Path

        src = Path("sar_orch/experiment.py").read_text(encoding="utf-8")
        assert "gateway ignores it" in src
        assert "NOT deterministically reproducible" in src


@pytest.mark.parametrize(
    "replies,expected",
    [
        (["x", "x"], True),
        (["x", "y"], False),
        (["", ""], True),
    ],
)
def test_verdict_table(monkeypatch, replies, expected):
    _patch_client(monkeypatch, replies)
    assert _probe() is expected

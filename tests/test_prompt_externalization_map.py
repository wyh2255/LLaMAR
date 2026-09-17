"""外置化 prompt 第二批（reef × sar_orch workflow §B1-B3，2026-09-17）：

map_summarizer + map_agent 两个 system prompt 的 sha 不变量与落点契约。

两块契约：

1. sha 不变量（§B3）：
   - map_agent——原串无插值，prompt 文件字节 sha256 == 改造前内联串的 utf-8
     字节 sha256；
   - map_summarizer——原串含 ``{max_summary_chars}`` 插值，以**运行时 composed
     串**与改造前内联 composed 串逐字节相等为准（§B3 明示只比 composed
     string、不比文件字节）。测试直接驱动真实 call site
     （``MapSummarizer._build_messages``），并用 150 / 42 两个预算值验证插值
     语义确实进串。
2. prompt 原文只活在 prompt 文件里：.py 源码不得再残留内联原文，且必须经
   ``load_repo_prompt`` 装载（与第一批 ``tests/test_prompt_loader.py`` 同口径）。

sha 值来历（改造前 HEAD 实测：``git show HEAD:<path>`` 独立重建原表达式后取得）：

- ``MAP_SUMMARIZER_COMPOSED_150_SHA256`` / ``MAP_SUMMARIZER_COMPOSED_42_SHA256``
  = 原 ``summarizer._build_messages`` 中 ``system_prompt`` 拼接表达式在
  ``max_summary_chars`` = 150 / 42 时的 composed 串；
- ``MAP_AGENT_INLINE_SHA256`` = 原 ``llm_query.MAP_AGENT_SYSTEM_PROMPT``
  （无插值，文件字节即该串）。

``MAP_SUMMARIZER_TEMPLATE_SHA256`` 为本次落盘模板文件的字节快照（无 HEAD
对应物，仅作变更探测器）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sar_orch.map import summarizer as summarizer_module
from sar_orch.map.summarizer import MapSummarizer
from sar_orch.map_agent.llm_query import MAP_AGENT_SYSTEM_PROMPT

_REPO_ROOT = Path(__file__).resolve().parent.parent

MAP_SUMMARIZER_PROMPT_FILE = _REPO_ROOT / "sar_orch/prompts/map_summarizer/system.md"
MAP_AGENT_PROMPT_FILE = _REPO_ROOT / "sar_orch/prompts/map_agent/system.md"

#: 改造前内联 composed 串（map_summarizer，``max_summary_chars=150``）的 sha256。
MAP_SUMMARIZER_COMPOSED_150_SHA256 = (
    "f7cc7b786f287056e4181490020c7b6ff7aa004528d27d6b55b86024180e8df7"
)
#: 同上，``max_summary_chars=42``——证明插值确实随配置进串。
MAP_SUMMARIZER_COMPOSED_42_SHA256 = (
    "677b3d6ca37331bb847618c2fce7179cf14cc0ac03509364fbea83479ab01d7c"
)
#: map_summarizer 模板文件字节 sha256（含 ``{max_summary_chars}`` 占位）。
MAP_SUMMARIZER_TEMPLATE_SHA256 = (
    "ee3c7e1581ddc347705fd1f2f1f40032cb79d6f4f26a45113dcb59676b49ccdf"
)
#: 改造前内联串（map_agent，无插值）的 sha256，亦即外置文件字节 sha256。
MAP_AGENT_INLINE_SHA256 = (
    "a42cd41ee36a3af179ca49a9ee800ccf985fae1dd9826a7c028dd6337aa9e08f"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_summarizer(tmp_path: Path, max_summary_chars: int = 150) -> MapSummarizer:
    return MapSummarizer(
        summary_path=tmp_path / "prompt_sha.jsonl",
        token_usage_sink=lambda agent, **kw: None,
        max_summary_chars=max_summary_chars,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. map_summarizer —— 插值 prompt：composed 串不变量
# ─────────────────────────────────────────────────────────────────────────────


class TestMapSummarizerPrompt:
    def test_system_prompt_comes_from_the_prompt_file(self):
        assert summarizer_module._SYSTEM_PROMPT_TEMPLATE == (
            MAP_SUMMARIZER_PROMPT_FILE.read_bytes().decode("utf-8")
        )

    def test_template_file_sha256_is_frozen(self):
        raw = MAP_SUMMARIZER_PROMPT_FILE.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == MAP_SUMMARIZER_TEMPLATE_SHA256
        assert b"{max_summary_chars}" in raw

    def test_composed_system_prompt_equals_head_inline(self, tmp_path):
        """真实 call site 的 composed 串 == 改造前内联串（默认 150 预算）。"""
        messages = _make_summarizer(tmp_path)._build_messages({}, env_step=5)

        assert messages[0].role == "system"
        assert isinstance(messages[0].content, str)
        assert _sha256(messages[0].content) == MAP_SUMMARIZER_COMPOSED_150_SHA256

    def test_composed_system_prompt_interpolates_configured_budget(self, tmp_path):
        """换预算（42）后 composed 串 == 改造前同参数内联串——插值真的生效。"""
        messages = _make_summarizer(tmp_path, max_summary_chars=42)._build_messages(
            {}, env_step=1
        )

        content = messages[0].content
        assert isinstance(content, str)
        assert _sha256(content) == MAP_SUMMARIZER_COMPOSED_42_SHA256
        assert "42" in content
        assert "{max_summary_chars}" not in content


# ─────────────────────────────────────────────────────────────────────────────
# 2. map_agent —— 无插值 prompt：文件字节不变量
# ─────────────────────────────────────────────────────────────────────────────


class TestMapAgentPrompt:
    def test_system_prompt_equals_prompt_file_bytes(self):
        assert MAP_AGENT_SYSTEM_PROMPT == (
            MAP_AGENT_PROMPT_FILE.read_bytes().decode("utf-8")
        )

    def test_prompt_file_sha256_equals_head_inline(self):
        raw = MAP_AGENT_PROMPT_FILE.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == MAP_AGENT_INLINE_SHA256
        assert _sha256(MAP_AGENT_SYSTEM_PROMPT) == MAP_AGENT_INLINE_SHA256

    def test_react_agent_receives_the_loaded_prompt(self, monkeypatch):
        """``build_map_agent_graph`` 传给 ReAct agent 的正是 loader 装载的串。"""
        from sar_orch.map_agent import llm_query

        captured: dict[str, object] = {}
        captured_tools: list[object] = []

        def _fake_create_react_agent(llm, tools=None, prompt=None, **kwargs):
            captured["llm"] = llm
            captured_tools.extend(tools or [])
            captured["prompt"] = prompt
            return "graph-sentinel"

        monkeypatch.setattr(llm_query, "create_react_agent", _fake_create_react_agent)
        sentinel_llm = object()

        assert llm_query.build_map_agent_graph(sentinel_llm) == "graph-sentinel"
        assert captured["llm"] is sentinel_llm
        assert captured["prompt"] == MAP_AGENT_SYSTEM_PROMPT
        assert captured["prompt"] == (
            MAP_AGENT_PROMPT_FILE.read_bytes().decode("utf-8")
        )
        assert len(captured_tools) == 4


# ─────────────────────────────────────────────────────────────────────────────
# 3. prompt 原文不得回流 .py 源码
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptTextLivesOnlyInPromptFiles:
    def test_inline_prompt_text_removed_and_loader_wired(self):
        cases = {
            _REPO_ROOT / "sar_orch/map/summarizer.py": (
                "你是一个SAR（搜索与救援）地图摘要生成助手。"
            ),
            _REPO_ROOT / "sar_orch/map_agent/llm_query.py": (
                "You are the SAR Map Agent."
            ),
        }
        for source, inline_prefix in cases.items():
            body = source.read_bytes().decode("utf-8")
            assert inline_prefix not in body, f"inline prompt still present in {source}"
            assert "load_repo_prompt" in body

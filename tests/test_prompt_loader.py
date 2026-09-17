"""外置化 prompt 第一批（reef × sar_orch workflow §B1-B3，2026-09-17）。

两块契约：

1. ``a2a.utils.prompt_loader`` 的加载行为 —— 自 ``__file__`` 上溯 repo 根
   （``pyproject.toml`` 锚点）、文件缺失/不可读/非 utf-8/空 一律
   ``PromptLoadError``（fail-closed，禁静默回退内联值）；以及 ``a2a.utils``
   命名空间合并（项目侧 prompt_loader 与 a2a-sdk 侧 constants/proto_utils
   同时可解析）。
2. sha 不变量（§B3）—— 外置文件的字节内容与原内联字符串逐字节一致；
   reflection 的 prompt 含 ``{kinds}`` 插值，以**运行时 composed 串**与原
   内联串相等为准。硬编码 sha256 均为改造前（HEAD）实测值。

sha 值来历（改造前 HEAD 实测，`git show HEAD:<path>` 与工作区逐字节一致时测得）：

- ``REFLECTION_COMPOSED_SHA256`` = 原 ``reflection._SYSTEM_PROMPT``
  （``", ".join(LONG_TERM_MEMORY_KINDS)`` 已插值）；
- ``DIAGNOSIS_INLINE_SHA256`` = 原 ``diagnosis_loop._SYSTEM_PROMPT``
  （无插值，文件字节即该串）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from a2a.utils import prompt_loader
from a2a.utils.prompt_loader import PromptLoadError, load_repo_prompt

_REPO_ROOT = Path(__file__).resolve().parent.parent

REFLECTION_PROMPT_FILE = _REPO_ROOT / "sar_orch/prompts/reflection/system.md"
DIAGNOSIS_PROMPT_FILE = _REPO_ROOT / "sar_orch/prompts/diagnosis/system.md"

#: 改造前内联 composed 串（reflection，kinds 已插值）的 sha256。
REFLECTION_COMPOSED_SHA256 = (
    "f9dd3badf8fde0896ffd4ccd051c80924ec872f8d2c3c1abfb5720ff13a923e5"
)
#: reflection prompt 文件字节 sha256（模板，含 ``{kinds}`` 占位）。
REFLECTION_TEMPLATE_SHA256 = (
    "1c0456434f8de69554c8d6af2ca91c2d13bb68dade88662043e6a2338dd1ac98"
)
#: 改造前内联串（diagnosis，无插值）的 sha256，亦即外置文件字节 sha256。
DIAGNOSIS_INLINE_SHA256 = (
    "fab252b05c907e24a99a52f44d2b189b765fbf644d2c050b97a79fe6d60c24ba"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 1. loader 行为契约
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadRepoPrompt:
    def test_reads_committed_prompt_verbatim(self):
        text = load_repo_prompt("sar_orch/prompts/coordinator/system.md")
        raw = (_REPO_ROOT / "sar_orch/prompts/coordinator/system.md").read_bytes()
        assert text == raw.decode("utf-8")
        assert text.strip()

    def test_repo_root_is_anchored_on_pyproject_toml(self):
        root = prompt_loader._find_repo_root(
            Path(prompt_loader.__file__).resolve().parent
        )
        assert root == _REPO_ROOT
        assert (root / "pyproject.toml").is_file()
        assert (root / "src" / "a2a" / "utils" / "prompt_loader.py").is_file()

    def test_find_repo_root_walks_up_to_marker(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert prompt_loader._find_repo_root(nested) == tmp_path.resolve()

    def test_find_repo_root_without_marker_raises(self, tmp_path):
        nested = tmp_path / "no" / "marker"
        nested.mkdir(parents=True)
        with pytest.raises(PromptLoadError, match="repository root not found"):
            prompt_loader._find_repo_root(nested)

    def test_missing_file_raises(self):
        with pytest.raises(PromptLoadError, match="unreadable"):
            load_repo_prompt("sar_orch/prompts/does_not_exist/system.md")

    def test_directory_path_raises(self):
        with pytest.raises(PromptLoadError, match="unreadable"):
            load_repo_prompt("sar_orch/prompts")

    def test_absolute_path_raises(self):
        with pytest.raises(PromptLoadError, match="repository-relative"):
            load_repo_prompt("/etc/hostname")

    def test_non_string_path_raises(self):
        with pytest.raises(PromptLoadError, match="non-empty str"):
            load_repo_prompt(Path("sar_orch/prompts/diagnosis/system.md"))  # type: ignore[arg-type]
        with pytest.raises(PromptLoadError, match="non-empty str"):
            load_repo_prompt("")

    def test_empty_and_blank_files_raise(self, tmp_path, monkeypatch):
        (tmp_path / "empty.md").write_bytes(b"")
        (tmp_path / "blank.md").write_bytes(b"  \n\n\t\n")
        monkeypatch.setattr(prompt_loader, "_find_repo_root", lambda start: tmp_path)
        with pytest.raises(PromptLoadError, match="empty"):
            load_repo_prompt("empty.md")
        with pytest.raises(PromptLoadError, match="empty"):
            load_repo_prompt("blank.md")

    def test_non_utf8_file_raises(self, tmp_path, monkeypatch):
        (tmp_path / "binary.md").write_bytes(b"\xff\xfeprompt")
        monkeypatch.setattr(prompt_loader, "_find_repo_root", lambda start: tmp_path)
        with pytest.raises(PromptLoadError, match="utf-8"):
            load_repo_prompt("binary.md")


class TestA2AUtilsNamespaceMerge:
    """项目侧 ``a2a/utils/`` 不得遮蔽 a2a-sdk 的 ``a2a.utils`` 子模块。"""

    def test_project_package_owns_a2a_utils(self):
        import a2a.utils as utils_pkg

        assert utils_pkg.__file__ is not None
        assert (
            Path(utils_pkg.__file__).resolve()
            == (_REPO_ROOT / "src" / "a2a" / "utils" / "__init__.py").resolve()
        )
        assert str(_REPO_ROOT / "src" / "a2a" / "utils") in list(utils_pkg.__path__)

    def test_sdk_submodules_still_resolve(self):
        from a2a.utils.constants import TransportProtocol
        from a2a.utils.errors import A2AError
        from a2a.utils.proto_utils import to_stream_response
        from a2a.utils.task import apply_history_length

        assert TransportProtocol.HTTP_JSON is not None
        assert callable(to_stream_response)
        assert callable(apply_history_length)
        assert issubclass(A2AError, Exception)

    def test_sdk_public_reexports_are_mirrored(self):
        import a2a.utils as utils_pkg
        from a2a.utils import TransportProtocol as reexported
        from a2a.utils.constants import TransportProtocol

        assert reexported is TransportProtocol
        for name in ("AGENT_CARD_WELL_KNOWN_PATH", "DEFAULT_RPC_URL"):
            assert name in utils_pkg.__all__
            assert getattr(utils_pkg, name)


# ─────────────────────────────────────────────────────────────────────────────
# 2. sha 不变量（§B3）
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptShaInvariant:
    def test_diagnosis_prompt_file_sha256_unchanged(self):
        raw = DIAGNOSIS_PROMPT_FILE.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == DIAGNOSIS_INLINE_SHA256

    def test_reflection_prompt_file_sha256_is_frozen_template(self):
        raw = REFLECTION_PROMPT_FILE.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == REFLECTION_TEMPLATE_SHA256
        assert b"{kinds}" in raw

    def test_diagnosis_system_prompt_equals_file_bytes(self):
        from sar_orch import diagnosis_loop

        assert (
            diagnosis_loop._SYSTEM_PROMPT
            == DIAGNOSIS_PROMPT_FILE.read_bytes().decode("utf-8")
        )
        assert _sha256(diagnosis_loop._SYSTEM_PROMPT) == DIAGNOSIS_INLINE_SHA256

    def test_reflection_system_prompt_composed_equals_inline_sha(self):
        from a2a.coordinator.memory import reflection

        composed = (
            REFLECTION_PROMPT_FILE.read_bytes()
            .decode("utf-8")
            .format(kinds=", ".join(reflection.LONG_TERM_MEMORY_KINDS))
        )
        assert reflection._SYSTEM_PROMPT == composed
        assert _sha256(reflection._SYSTEM_PROMPT) == REFLECTION_COMPOSED_SHA256

    def test_prompt_text_lives_only_in_the_prompt_files(self):
        """呼叫点必须走 loader：py 源码内不得再残留内联 prompt 原文。"""
        cases = {
            _REPO_ROOT / "src/a2a/coordinator/memory/reflection.py": (
                "You are the long-term memory reflection subsystem."
            ),
            _REPO_ROOT / "sar_orch/diagnosis_loop.py": (
                "You are the system-health review subsystem"
            ),
        }
        for source, inline_prefix in cases.items():
            body = source.read_bytes().decode("utf-8")
            assert inline_prefix not in body, f"inline prompt still present in {source}"
            assert "load_repo_prompt" in body

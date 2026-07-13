"""Tests for Agent.sandbox.SandboxedTool wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from Agent.sandbox import (
    SandboxPolicy,
    SandboxViolation,
    SandboxedTool,
    validate_custom_tools_dir,
    wrap_tools_with_sandbox,
)


class _ToolResult:
    """Minimal ToolResult for testing (duck-typed)."""

    def __init__(
        self, success: bool = True, content: str = "", error: str | None = None
    ):
        self.success = success
        self.content = content
        self.error = error


class _MockTool:
    """Mock tool for testing (duck-typed to match Tool interface)."""

    def __init__(self, name: str, *, execute_result: _ToolResult | None = None):
        self._name = name
        self._execute_result = execute_result or _ToolResult(
            success=True, content=f"{name} executed"
        )
        self.execute_calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Mock {self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> _ToolResult:
        self.execute_calls.append(kwargs)
        return self._execute_result

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": self.to_schema()}


@pytest.mark.asyncio
class TestSandboxedToolFileRead:
    """SandboxedTool validates read_file paths."""

    async def test_read_file_valid_path_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "data.txt").write_text("hello")
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("read_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(path=str(ws / "data.txt"))
        assert result.success
        assert inner.execute_calls == [{"path": str((ws / "data.txt").resolve())}]

    async def test_read_file_invalid_path_raises(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("read_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(path=str(tmp_path / "outside.txt"))
        assert not result.success
        assert "outside allowed read roots" in (result.error or "")

    async def test_read_file_relative_path_resolved(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "doc.md").write_text("doc")
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("read_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(path="doc.md")
        assert result.success
        assert inner.execute_calls == [{"path": str((ws / "doc.md").resolve())}]


@pytest.mark.asyncio
class TestSandboxedToolFileWrite:
    """SandboxedTool validates write_file paths."""

    async def test_write_file_valid_path_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("write_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(path=str(ws / "output.txt"), content="data")
        assert result.success
        assert inner.execute_calls == [
            {"path": str((ws / "output.txt").resolve()), "content": "data"}
        ]

    async def test_write_file_invalid_path_raises(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("write_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(
            path=str(tmp_path / "outside.txt"), content="data"
        )
        assert not result.success
        assert "outside allowed write roots" in (result.error or "")


@pytest.mark.asyncio
class TestSandboxedToolFileEdit:
    """SandboxedTool validates edit_file paths."""

    async def test_edit_file_valid_path_passes(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("edit_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(
            path=str(ws / "file.txt"), old_str="old", new_str="new"
        )
        assert result.success
        assert inner.execute_calls == [
            {
                "path": str((ws / "file.txt").resolve()),
                "old_str": "old",
                "new_str": "new",
            }
        ]

    async def test_edit_file_invalid_path_raises(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("edit_file")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(
            path=str(tmp_path / "outside.txt"), old_str="old", new_str="new"
        )
        assert not result.success
        assert "outside allowed write roots" in (result.error or "")


@pytest.mark.asyncio
class TestSandboxedToolBash:
    """SandboxedTool enforces bash_enabled policy."""

    async def test_bash_disabled_returns_error(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws, bash_enabled=False)
        inner = _MockTool("bash")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(command="rm -rf /")
        assert not result.success
        assert result.content == ""
        assert result.error == "Bash is disabled by sandbox policy"
        assert inner.execute_calls == []

    async def test_bash_default_disabled(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("bash")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(command="echo hello")
        assert not result.success
        assert result.error == "Bash is disabled by sandbox policy"
        assert inner.execute_calls == []

    async def test_bash_enabled_passes_through(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws, bash_enabled=True)
        inner = _MockTool("bash")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(command="echo hello")
        assert result.success
        assert inner.execute_calls == [{"command": "echo hello"}]


@pytest.mark.asyncio
class TestSandboxedToolPassthrough:
    """Unknown non-file/non-bash tools pass through unchanged."""

    async def test_unknown_tool_passthrough(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        inner = _MockTool("navigate_to")
        wrapped = SandboxedTool(inner, policy)
        result = await wrapped.execute(x=1, y=2, z=3)
        assert result.success
        assert inner.execute_calls == [{"x": 1, "y": 2, "z": 3}]

    async def test_disabled_policy_passthrough(self, tmp_path: Path) -> None:
        inner = _MockTool("bash")
        wrapped = SandboxedTool(inner, SandboxPolicy.off())
        result = await wrapped.execute(command="echo hello")
        assert result.success
        assert inner.execute_calls == [{"command": "echo hello"}]

    async def test_none_policy_passthrough(self) -> None:
        inner = _MockTool("bash")
        wrapped = SandboxedTool(inner, None)
        result = await wrapped.execute(command="echo hello")
        assert result.success
        assert inner.execute_calls == [{"command": "echo hello"}]


class TestWrapToolsWithSandbox:
    """wrap_tools_with_sandbox utility."""

    def test_enabled_policy_wraps_all(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        tools = [_MockTool("read_file"), _MockTool("navigate_to")]
        wrapped = wrap_tools_with_sandbox(tools, policy)
        assert len(wrapped) == 2
        assert all(isinstance(t, SandboxedTool) for t in wrapped)

    def test_none_policy_returns_original(self) -> None:
        tools = [_MockTool("read_file")]
        wrapped = wrap_tools_with_sandbox(tools, None)
        assert wrapped is tools

    def test_disabled_policy_returns_original(self, tmp_path: Path) -> None:
        tools = [_MockTool("bash")]
        wrapped = wrap_tools_with_sandbox(tools, SandboxPolicy.off())
        assert wrapped is tools


class TestValidateCustomToolsDir:
    """validate_custom_tools_dir utility."""

    def test_valid_path_within_roots(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws, tool_import_roots=[tools_dir])
        result = validate_custom_tools_dir(tools_dir / "my_tool", policy)
        assert result == (tools_dir / "my_tool").resolve()

    def test_invalid_path_outside_roots(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws, tool_import_roots=[tools_dir])
        with pytest.raises(SandboxViolation, match="outside allowed tool import roots"):
            validate_custom_tools_dir(ws / "my_tool", policy)

    def test_none_path_returns_none(self) -> None:
        policy = SandboxPolicy.workspace("/tmp", "/tmp")
        result = validate_custom_tools_dir(None, policy)
        assert result is None

    def test_none_policy_returns_path(self, tmp_path: Path) -> None:
        result = validate_custom_tools_dir(tmp_path / "tools", None)
        assert result == (tmp_path / "tools").resolve()

    def test_disabled_policy_returns_path(self, tmp_path: Path) -> None:
        result = validate_custom_tools_dir(tmp_path / "tools", SandboxPolicy.off())
        assert result == (tmp_path / "tools").resolve()

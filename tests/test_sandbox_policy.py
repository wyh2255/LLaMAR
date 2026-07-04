"""Tests for Agent.sandbox.SandboxPolicy."""

import pytest
from pathlib import Path

from Agent.sandbox import SandboxPolicy, SandboxViolation


class TestSandboxPolicyOff:
    """SandboxPolicy.off() — fully permissive, no restrictions."""

    def test_off_returns_policy(self):
        policy = SandboxPolicy.off()
        assert isinstance(policy, SandboxPolicy)

    def test_off_accepts_any_read(self, tmp_path):
        policy = SandboxPolicy.off()
        result = policy.check_read(tmp_path / "some" / "file.txt")
        assert result == (tmp_path / "some" / "file.txt").resolve()

    def test_off_accepts_any_write(self, tmp_path):
        policy = SandboxPolicy.off()
        result = policy.check_write(tmp_path / "output" / "data.csv")
        assert result == (tmp_path / "output" / "data.csv").resolve()

    def test_off_accepts_any_tool_import(self, tmp_path):
        policy = SandboxPolicy.off()
        result = policy.check_tool_import_dir(tmp_path / "tools")
        assert result == (tmp_path / "tools").resolve()

    def test_off_resolves_relative_paths(self):
        policy = SandboxPolicy.off()
        result = policy.check_read("relative/path.txt")
        assert result == Path("relative/path.txt").resolve()


class TestSandboxPolicyWorkspace:
    """SandboxPolicy.workspace() — restricted sandbox."""

    def test_workspace_returns_policy(self, tmp_path):
        policy = SandboxPolicy.workspace(tmp_path, tmp_path / "ws")
        assert isinstance(policy, SandboxPolicy)

    def test_read_within_workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "data.txt").write_text("hello")
        policy = SandboxPolicy.workspace(tmp_path, ws)
        result = policy.check_read(ws / "data.txt")
        assert result == (ws / "data.txt").resolve()

    def test_write_within_workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        result = policy.check_write(ws / "output.txt")
        assert result == (ws / "output.txt").resolve()

    def test_read_workspace_relative(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "doc.md").write_text("doc")
        policy = SandboxPolicy.workspace(tmp_path, ws)
        result = policy.check_read("doc.md")
        assert result == (ws / "doc.md").resolve()

    def test_write_workspace_relative(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        result = policy.check_write("output.log")
        assert result == (ws / "output.log").resolve()

    def test_absolute_path_outside_workspace_denied(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        policy = SandboxPolicy.workspace(tmp_path, ws)
        with pytest.raises(SandboxViolation, match="outside allowed read roots"):
            policy.check_read(outside)

    def test_write_outside_write_roots_denied(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(
            tmp_path, ws,
            write_roots=[ws / "sub"],
        )
        (tmp_path / "sub").mkdir()
        with pytest.raises(SandboxViolation, match="outside allowed write roots"):
            policy.check_write(ws / "no_write.txt")

    def test_dot_dot_escape_denied(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(tmp_path, ws)
        with pytest.raises(SandboxViolation, match="Escape attempt"):
            policy.check_read(ws / "sub" / ".." / ".." / "secret.txt")

    def test_tool_import_within_tool_roots(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        policy = SandboxPolicy.workspace(
            tmp_path, ws,
            tool_import_roots=[tools_dir],
        )
        result = policy.check_tool_import_dir(tools_dir / "my_tool")
        assert result == (tools_dir / "my_tool").resolve()

    def test_tool_import_outside_tool_roots_denied(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        policy = SandboxPolicy.workspace(
            tmp_path, ws,
            tool_import_roots=[tmp_path / "tools"],
        )
        with pytest.raises(SandboxViolation, match="outside allowed tool import roots"):
            policy.check_tool_import_dir(ws / "my_tool")

    def test_read_extra_root_allowed(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        extra = tmp_path / "shared"
        extra.mkdir()
        (extra / "lib.txt").write_text("lib")
        policy = SandboxPolicy.workspace(
            tmp_path, ws,
            read_roots=[extra],
        )
        result = policy.check_read(extra / "lib.txt")
        assert result == (extra / "lib.txt").resolve()

    def test_symlink_outside_denied(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        link = ws / "link.txt"
        link.symlink_to(outside)
        policy = SandboxPolicy.workspace(tmp_path, ws)
        with pytest.raises(SandboxViolation, match="Escape attempt|outside allowed read roots"):
            policy.check_read(link)

    def test_project_root_required_for_relative(self, tmp_path):
        """relative paths require a valid project_root."""
        with pytest.raises(ValueError, match="project_root"):
            SandboxPolicy.workspace("/nonexistent", "/other")

    def test_explicit_write_root_allowed(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        extra = tmp_path / "output_dir"
        extra.mkdir()
        policy = SandboxPolicy.workspace(
            tmp_path, ws,
            write_roots=[extra],
        )
        result = policy.check_write(extra / "result.json")
        assert result == (extra / "result.json").resolve()

    def test_tool_import_project_local_default(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tools = tmp_path / "tools" / "my_tool"
        tools.mkdir(parents=True)
        policy = SandboxPolicy.workspace(tmp_path, ws)
        result = policy.check_tool_import_dir(tools)
        assert result == tools.resolve()

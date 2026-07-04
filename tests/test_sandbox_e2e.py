"""End-to-end acceptance tests for sandbox workspace policy in SAR context."""

from pathlib import Path

import pytest

from Agent.sandbox import SandboxPolicy, SandboxViolation


class TestWorkspaceDeniesSystemFiles:
    """Workspace policy must deny access to system files outside project."""

    def test_denies_etc_passwd_read(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        policy = SandboxPolicy.workspace(project, ws)
        with pytest.raises(SandboxViolation):
            policy.check_read("/etc/passwd")

    def test_denies_etc_passwd_write(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        policy = SandboxPolicy.workspace(project, ws)
        with pytest.raises(SandboxViolation):
            policy.check_write("/etc/passwd")


class TestWorkspaceDeniesSourceFileWrite:
    """Workspace policy must deny writing project source files outside workspace."""

    def test_denies_write_to_src_file(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        src_file = project / "src" / "Agent" / "sandbox.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("dummy")
        policy = SandboxPolicy.workspace(project, ws)
        with pytest.raises(SandboxViolation):
            policy.check_write(src_file)


class TestWorkspaceAllowsResultDirWrite:
    """Workspace policy must allow write to explicitly granted result directories."""

    def test_allows_write_to_result_dir(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        result_dir = project / "results"
        result_dir.mkdir()
        policy = SandboxPolicy.workspace(
            project, ws,
            write_roots=[result_dir],
        )
        resolved = policy.check_write(result_dir / "output.csv")
        assert resolved == (result_dir / "output.csv").resolve()


class TestWorkspaceDisablesBash:
    """Workspace policy must disable Bash by default."""

    def test_bash_disabled_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        policy = SandboxPolicy.workspace(project, ws)
        assert policy._bash_enabled is False


class TestWorkspaceRejectsExternalToolDir:
    """Workspace policy must reject tool import from outside project tree."""

    def test_external_tool_dir_rejected(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        external_tools = tmp_path / "external" / "tools"
        external_tools.mkdir(parents=True)
        policy = SandboxPolicy.workspace(project, ws)
        with pytest.raises(SandboxViolation):
            policy.check_tool_import_dir(external_tools)


class TestWorkspaceAcceptsProjectToolDir:
    """Workspace policy must accept tool import from within project tree."""

    def test_project_tool_dir_accepted(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        ws = project / "workspace"
        ws.mkdir()
        project_tools = project / "tools" / "my_tool"
        project_tools.mkdir(parents=True)
        policy = SandboxPolicy.workspace(project, ws)
        resolved = policy.check_tool_import_dir(project_tools)
        assert resolved == project_tools.resolve()


class TestSandboxOffPermissive:
    """SandboxPolicy.off() must allow everything in E2E context."""

    def test_off_allows_system_file_read(self, tmp_path: Path) -> None:
        policy = SandboxPolicy.off()
        resolved = policy.check_read("/etc/passwd")
        assert resolved == Path("/etc/passwd").resolve()

    def test_off_allows_any_write(self, tmp_path: Path) -> None:
        policy = SandboxPolicy.off()
        resolved = policy.check_write(tmp_path / "anywhere" / "file.txt")
        assert resolved == (tmp_path / "anywhere" / "file.txt").resolve()

    def test_off_does_not_enable_bash(self) -> None:
        policy = SandboxPolicy.off()
        assert policy._bash_enabled is False  # off has no effect on bash; SandboxedTool handles bypass

    def test_off_allows_any_tool_import(self, tmp_path: Path) -> None:
        policy = SandboxPolicy.off()
        resolved = policy.check_tool_import_dir(tmp_path / "external" / "tools")
        assert resolved == (tmp_path / "external" / "tools").resolve()

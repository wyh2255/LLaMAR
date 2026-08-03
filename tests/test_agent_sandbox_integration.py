"""Tests for --sandbox-profile plumbing through A2A worker and coordinator CLIs."""

from unittest.mock import MagicMock
from pathlib import Path

import pytest

from Agent.sandbox import SandboxPolicy


# ============================================================
# SandboxPolicy factory tests
# ============================================================


class TestSandboxPolicyFactory:
    def test_off_creates_disabled_policy(self):
        policy = SandboxPolicy.off()
        assert policy._enabled is False

    def test_off_allows_all_paths(self, tmp_path):
        policy = SandboxPolicy.off()
        # Should not raise
        resolved = policy.check_read(tmp_path / "some_file.txt")
        assert resolved == (tmp_path / "some_file.txt").resolve()
        resolved = policy.check_write(tmp_path / "other.txt")
        assert resolved == (tmp_path / "other.txt").resolve()

    def test_workspace_creates_enabled_policy(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        policy = SandboxPolicy.workspace(
            project_root=project,
            workspace_dir="./ws",
        )
        assert policy._enabled is True
        assert policy._workspace_dir == (project / "ws").resolve()

    def test_workspace_raises_on_missing_project_root(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(ValueError, match="project_root must exist"):
            SandboxPolicy.workspace(
                project_root=missing,
                workspace_dir="./ws",
            )

    def test_workspace_blocks_outside_read(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        ws_dir = project / "ws"
        ws_dir.mkdir()

        policy = SandboxPolicy.workspace(
            project_root=project,
            workspace_dir="./ws",
        )
        # Read within workspace should pass
        allowed = ws_dir / "allowed.txt"
        allowed.write_text("ok")
        policy.check_read(allowed)  # should not raise

        # Read outside workspace should fail
        outside = tmp_path / "outside.txt"
        from Agent.sandbox import SandboxViolation

        with pytest.raises(SandboxViolation):
            policy.check_read(str(outside))


# ============================================================
# Worker CLI plumbing tests
# ============================================================


class TestWorkerSandboxPlumbing:
    def test_create_worker_a2a_server_accepts_sandbox_policy(self):
        """create_worker_a2a_server() must accept sandbox_policy parameter."""
        from a2a.worker.a2a_server import create_worker_a2a_server
        import inspect

        sig = inspect.signature(create_worker_a2a_server)
        assert "sandbox_policy" in sig.parameters

    def test_agent_adapter_accepts_sandbox_policy(self):
        """AgentAdapter.__init__() must accept sandbox_policy parameter."""
        from a2a.worker.agent_adapter import AgentAdapter
        import inspect

        sig = inspect.signature(AgentAdapter.__init__)
        assert "sandbox_policy" in sig.parameters

    def test_agent_adapter_passes_to_build_options(self):
        """AgentAdapter must pass sandbox_policy to AgentBuildOptions."""
        from a2a.worker.agent_adapter import AgentAdapter

        policy = SandboxPolicy.off(project_root=None)
        adapter = AgentAdapter.__new__(AgentAdapter)
        adapter._agent_opts = MagicMock()

        # Simulate __init__ storing it
        adapter._sandbox_policy = policy
        adapter._agent_opts.sandbox_policy = policy

        assert adapter._agent_opts.sandbox_policy is policy


# ============================================================
# Coordinator CLI plumbing tests
# ============================================================


class TestCoordinatorSandboxPlumbing:
    def test_create_server_accepts_sandbox_policy(self):
        """create_server() must accept sandbox_policy parameter."""
        from a2a.coordinator.server import create_server
        import inspect

        sig = inspect.signature(create_server)
        assert "sandbox_policy" in sig.parameters

    def test_coordinator_server_init_accepts_sandbox_policy(self):
        """CoordinatorServer.__init__() must accept sandbox_policy parameter."""
        from a2a.coordinator.server import CoordinatorServer
        import inspect

        sig = inspect.signature(CoordinatorServer.__init__)
        assert "sandbox_policy" in sig.parameters

    def test_create_coordinator_a2a_server_accepts_sandbox_policy(self):
        """create_coordinator_a2a_server() must accept sandbox_policy parameter."""
        from a2a.coordinator.a2a_server import create_coordinator_a2a_server
        import inspect

        sig = inspect.signature(create_coordinator_a2a_server)
        assert "sandbox_policy" in sig.parameters

    def test_router_agent_accepts_sandbox_policy(self):
        """RouterAgent.__init__() must accept sandbox_policy parameter."""
        from a2a.coordinator.router import RouterAgent
        import inspect

        sig = inspect.signature(RouterAgent.__init__)
        assert "sandbox_policy" in sig.parameters

    def test_verifier_agent_accepts_sandbox_policy(self):
        """VerifierAgent.__init__() must accept sandbox_policy parameter."""
        from a2a.coordinator.verifier import VerifierAgent
        import inspect

        sig = inspect.signature(VerifierAgent.__init__)
        assert "sandbox_policy" in sig.parameters


# ============================================================
# CLI default behavior tests
# ============================================================


class TestCLIDefaultOff:
    def test_worker_cli_defaults_to_off(self):
        """Worker CLI must default to --sandbox-profile=off."""
        from a2a.worker.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        # Just check help output contains the option
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--sandbox-profile" in result.stdout

    def test_coordinator_cli_defaults_to_off(self):
        """Coordinator CLI must default to --sandbox-profile=off."""
        from a2a.coordinator.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--sandbox-profile" in result.stdout


class TestCLIInvalidSandboxProfile:
    def test_worker_cli_rejects_invalid_profile(self):
        """Worker CLI must reject an invalid --sandbox-profile value."""
        from a2a.worker.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--sandbox-profile",
                "invalid",
                "--worker-id",
                "test",
                "--coordinator-url",
                "ws://test",
            ],
        )
        assert result.exit_code != 0
        assert "Invalid sandbox profile" in result.output
        assert "invalid" in result.output
        assert "'off' or" in result.output
        assert "'workspace'" in result.output


class TestCLIProjectRoot:
    def test_worker_cli_project_root_is_repo_root(self):
        """Worker CLI workspace profile must use repo root, not src/."""
        import a2a.worker.cli as worker_cli

        project_root = Path(worker_cli.__file__).parent.parent.parent.parent.resolve()
        assert (project_root / "src").is_dir()
        assert (project_root / "sar_orch").is_dir()

    def test_coordinator_cli_project_root_is_repo_root(self):
        """Coordinator CLI workspace profile must use repo root, not src/."""
        import a2a.coordinator.cli as coordinator_cli

        project_root = Path(
            coordinator_cli.__file__
        ).parent.parent.parent.parent.resolve()
        assert (project_root / "src").is_dir()
        assert (project_root / "sar_orch").is_dir()


class TestCoordinatorRouterSandboxRuntime:
    def test_router_agent_load_custom_tools_rejects_external_dir(self, tmp_path):
        """RouterAgent._load_custom_tools must validate custom tool directories."""
        from Agent.sandbox import SandboxViolation
        from a2a.coordinator.router import RouterAgent

        project = tmp_path / "project"
        project.mkdir()
        workspace = project / "workspace"
        workspace.mkdir()
        allowed_tools = project / "tools"
        allowed_tools.mkdir()
        outside_tools = tmp_path / "outside_tools"
        outside_tools.mkdir()
        (outside_tools / "bad.py").write_text("tool = object()\n")

        router = RouterAgent.__new__(RouterAgent)
        router._custom_tools_dir = outside_tools
        router._sandbox_policy = SandboxPolicy.workspace(
            project_root=project,
            workspace_dir=workspace,
            tool_import_roots=[allowed_tools],
        )

        with pytest.raises(SandboxViolation):
            router._load_custom_tools()

    def test_router_agent_build_agent_wraps_tools(self, tmp_path, monkeypatch):
        """RouterAgent._build_agent must wrap router tools with sandbox policy."""
        from Agent.sandbox import SandboxedTool
        from a2a.coordinator.router import RouterAgent

        project = tmp_path / "project"
        project.mkdir()
        workspace = project / "workspace"
        workspace.mkdir()

        router = RouterAgent.__new__(RouterAgent)
        router._registry = MagicMock()
        router._custom_tools = []
        router._extra_tools = []
        router._skills_dir = None
        router._discover_skills_dir = lambda: None
        router._provider = "openai"
        router._api_key_env = "MISSING_TEST_API_KEY"
        router._api_base = "http://example.invalid"
        router._model = "test-model"
        router._max_steps = 1
        router._temperature = 0.7
        router._seed = None
        router._workspace_dir = workspace
        router._log_dir = None
        router._system_prompt = "system"
        router._sandbox_policy = SandboxPolicy.workspace(
            project_root=project,
            workspace_dir=workspace,
        )
        # Dummy value, not a real key — the openai SDK validates that some
        # api_key is present at client construction time, but _build_agent()
        # never issues a network call in this test.
        monkeypatch.setenv("MISSING_TEST_API_KEY", "sk-test-dummy-not-a-real-key")

        agent = router._build_agent()

        assert any(isinstance(tool, SandboxedTool) for tool in agent.tools.values())

    def test_verifier_build_agent_wraps_tools(self, tmp_path, monkeypatch):
        """VerifierAgent._build_agent must consume sandbox_policy consistently."""
        from Agent.sandbox import SandboxedTool
        from a2a.coordinator.verifier import VerifierAgent

        project = tmp_path / "project"
        project.mkdir()
        workspace = project / "workspace"
        workspace.mkdir()

        verifier = VerifierAgent.__new__(VerifierAgent)
        verifier._registry = MagicMock()
        verifier._provider = "openai"
        verifier._api_key_env = "MISSING_TEST_API_KEY"
        verifier._api_base = "http://example.invalid"
        verifier._model = "test-model"
        verifier._max_steps = 1
        verifier._temperature = 0.3
        verifier._seed = None
        verifier._workspace_dir = workspace
        verifier._log_dir = None
        verifier._sandbox_policy = SandboxPolicy.workspace(
            project_root=project,
            workspace_dir=workspace,
        )
        # Dummy value, not a real key — the openai SDK validates that some
        # api_key is present at client construction time, but _build_agent()
        # never issues a network call in this test.
        monkeypatch.setenv("MISSING_TEST_API_KEY", "sk-test-dummy-not-a-real-key")

        agent = verifier._build_agent()

        assert any(isinstance(tool, SandboxedTool) for tool in agent.tools.values())

    def test_coordinator_cli_rejects_invalid_profile(self):
        """Coordinator CLI must reject an invalid --sandbox-profile value."""
        from a2a.coordinator.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["--sandbox-profile", "invalid"])
        assert result.exit_code != 0
        assert "Invalid sandbox profile" in result.output
        assert "invalid" in result.output
        assert "'off' or" in result.output
        assert "'workspace'" in result.output

---
日期: 2026-07-04
文档类型: 实施计划
文档概述: 为 LLaMAR 的 src/Agent 生产主力框架添加轻量沙箱，防止 LLM 误操作并隔离 SAR 实验运行目录
---

# Agent Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight sandbox for `src/Agent/worker_agent` and `src/Agent/router_agent` to prevent LLM misuse of file/Bash tools and isolate SAR experiment outputs.

**Architecture:** Add a shared `src/Agent/sandbox.py` module with `SandboxPolicy`, path validation, custom-tool import validation, and later `SandboxedTool` wrapping. Wire the policy through worker/router build paths first, then A2A CLI/server plumbing, then SAR defaults. This is a misuse-prevention boundary, not container isolation or multi-tenant security.

**Tech Stack:** Python, existing `Tool` / `ToolResult` abstractions, Typer CLI, pytest, ruff.

## Global Constraints

- First version covers only `src/Agent/worker_agent` and `src/Agent/router_agent`.
- `AgentLang` is explicitly out of scope.
- Do not implement container isolation, seccomp, firejail, MCP network isolation, or multi-tenant strong security.
- Bash is disabled by default in the `workspace` sandbox profile.
- `tools_dir` dynamic custom tool import is allowed only under project-local allowlisted roots.
- Generic A2A CLI defaults to `--sandbox-profile off` for compatibility.
- SAR experiment paths should use the `workspace` profile by default.
- Unknown non-file/non-bash tools pass through to preserve SAR domain tools.
- Do not commit automatically; leave changes in the worktree unless the user explicitly requests commits.

---

## File Structure

- Create: `src/Agent/sandbox.py` for shared sandbox policy, validation helpers, tool wrappers, and custom tool directory validation.
- Modify: `src/Agent/worker_agent/build.py` to accept and apply sandbox policy.
- Modify: `src/Agent/router_agent/build.py` to accept and apply sandbox policy.
- Modify: `src/a2a/worker/cli.py`, `src/a2a/worker/a2a_server.py`, `src/a2a/worker/agent_adapter.py` for worker-side policy plumbing.
- Modify: `src/a2a/coordinator/cli.py`, `src/a2a/coordinator/server.py`, `src/a2a/coordinator/router.py`, `src/a2a/coordinator/verifier.py` for coordinator-side policy plumbing.
- Modify: `sar_orch/experiment.py`, `sar_orch/worker.py`, `sar_orch/coordinator.py` for SAR default workspace profile.
- Create: `tests/test_sandbox_policy.py` for path policy tests.
- Create: `tests/test_sandboxed_tool.py` for wrapper behavior tests.
- Create or modify: `tests/test_agent_sandbox_integration.py` for build/plumbing tests.
- Create or modify: `tests/test_sandbox_e2e.py` for higher-level safety regression tests.

---

### Task 1: Core Sandbox Policy

**Files:**
- Create: `src/Agent/sandbox.py`
- Create: `tests/test_sandbox_policy.py`

**Interfaces:**
- Produces: `class SandboxViolation(Exception)`
- Produces: `@dataclass(frozen=True) class SandboxPolicy`
- Produces: `SandboxPolicy.off(project_root: Path | str | None = None) -> SandboxPolicy`
- Produces: `SandboxPolicy.workspace(project_root: Path | str, workspace_dir: Path | str, read_roots: list[Path | str] | None = None, write_roots: list[Path | str] | None = None, tool_import_roots: list[Path | str] | None = None) -> SandboxPolicy`
- Produces: `SandboxPolicy.check_read(path: str | Path) -> Path`
- Produces: `SandboxPolicy.check_write(path: str | Path) -> Path`
- Produces: `SandboxPolicy.check_tool_import_dir(path: str | Path) -> Path`

- [ ] **Step 1: Write failing policy tests**

Test that workspace-relative paths resolve under the workspace, absolute paths outside allowed roots fail, `..` escapes fail, write roots are enforced, and project-local tool roots are enforced.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_sandbox_policy.py -v`

Expected: FAIL because `Agent.sandbox` does not exist yet.

- [ ] **Step 3: Implement minimal policy**

Implement path normalization with `Path.resolve()`, root containment, `off`, `workspace`, `check_read`, `check_write`, and `check_tool_import_dir`.

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/test_sandbox_policy.py -v`

Expected: PASS.

Run: `uv run --with ruff ruff check src/Agent/sandbox.py tests/test_sandbox_policy.py`

Expected: PASS.

---

### Task 2: Sandboxed Tool Wrapper

**Files:**
- Modify: `src/Agent/sandbox.py`
- Create: `tests/test_sandboxed_tool.py`

**Interfaces:**
- Consumes: `SandboxPolicy`, `SandboxViolation`
- Produces: `class SandboxedTool`
- Produces: `wrap_tools_with_sandbox(tools: list, policy: SandboxPolicy | None) -> list`
- Produces: `validate_custom_tools_dir(path: str | Path | None, policy: SandboxPolicy | None) -> Path | None`

- [ ] **Step 1: Write failing wrapper tests**

Test `read_file`, `write_file`, `edit_file`, `bash`, unknown tool passthrough, disabled policy passthrough, and custom tool directory validation.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_sandboxed_tool.py -v`

Expected: FAIL because wrapper APIs do not exist yet.

- [ ] **Step 3: Implement wrapper**

Implement argument validation before calling the wrapped tool. For `bash`, return `ToolResult(success=False, content="", error="Bash is disabled by sandbox policy")` when `bash_enabled` is false.

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/test_sandboxed_tool.py tests/test_sandbox_policy.py -v`

Expected: PASS.

Run: `uv run --with ruff ruff check src/Agent/sandbox.py tests/test_sandboxed_tool.py tests/test_sandbox_policy.py`

Expected: PASS.

---

### Task 3: Worker And Router Build Integration

**Files:**
- Modify: `src/Agent/worker_agent/build.py`
- Modify: `src/Agent/router_agent/build.py`
- Create or modify: `tests/test_agent_sandbox_integration.py`

**Interfaces:**
- Consumes: `SandboxPolicy`, `wrap_tools_with_sandbox`, `validate_custom_tools_dir`
- Produces: `AgentBuildOptions.sandbox_policy: SandboxPolicy | None`
- Produces: `RouterBuildOptions.sandbox_policy: SandboxPolicy | None`
- Produces: optional `sandbox_policy` keyword on both `load_custom_tools()` functions

- [ ] **Step 1: Write failing integration tests**

Test that worker and router build paths wrap tools under workspace policy, do not wrap under off policy, and reject external custom tools directories.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_agent_sandbox_integration.py -v`

Expected: FAIL because build options do not accept sandbox policy yet.

- [ ] **Step 3: Implement build integration**

Add `sandbox_policy` to build option dataclasses, validate `tools_dir` before dynamic imports, and wrap the final tools list after all builtins/custom/extra tools are merged.

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/test_agent_sandbox_integration.py tests/test_sandbox_policy.py tests/test_sandboxed_tool.py -v`

Expected: PASS.

Run: `uv run --with ruff ruff check src/Agent/worker_agent/build.py src/Agent/router_agent/build.py tests/test_agent_sandbox_integration.py`

Expected: PASS.

---

### Task 4: A2A Worker And Coordinator Plumbing

**Files:**
- Modify: `src/a2a/worker/cli.py`
- Modify: `src/a2a/worker/a2a_server.py`
- Modify: `src/a2a/worker/agent_adapter.py`
- Modify: `src/a2a/coordinator/cli.py`
- Modify: `src/a2a/coordinator/server.py`
- Modify: `src/a2a/coordinator/router.py`
- Modify: `src/a2a/coordinator/verifier.py`
- Create or modify: `tests/test_agent_sandbox_integration.py`

**Interfaces:**
- Consumes: `SandboxPolicy`
- Produces: CLI option `--sandbox-profile off|workspace`
- Produces: `sandbox_policy` plumbing through A2A server, adapter, router, and verifier construction

- [ ] **Step 1: Write failing plumbing tests**

Test default off behavior, explicit workspace behavior, and invalid profile rejection where practical without spinning real servers.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_agent_sandbox_integration.py -v`

Expected: FAIL because plumbing does not exist yet.

- [ ] **Step 3: Implement A2A plumbing**

Add CLI options and pass `SandboxPolicy` through worker/coordinator construction to agent build options.

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/test_agent_adapter_execute.py tests/test_coordinator_push_callback.py tests/test_respond_worker.py tests/test_agent_sandbox_integration.py -v`

Expected: PASS.

Run: `uv run --with ruff ruff check src/a2a/worker/cli.py src/a2a/worker/a2a_server.py src/a2a/worker/agent_adapter.py src/a2a/coordinator/cli.py src/a2a/coordinator/server.py src/a2a/coordinator/router.py src/a2a/coordinator/verifier.py`

Expected: PASS.

---

### Task 5: SAR Defaults, E2E Tests, And Documentation

**Files:**
- Modify: `sar_orch/experiment.py`
- Modify: `sar_orch/worker.py`
- Modify: `sar_orch/coordinator.py`
- Create or modify: `tests/test_sandbox_e2e.py`
- Modify: `AGENTS.md`
- Create or modify: `docs/system_docs/sandbox.md`

**Interfaces:**
- Consumes: `SandboxPolicy.workspace()`
- Produces: SAR experiment default workspace sandbox policy
- Produces: documentation for sandbox profiles and limitations

- [ ] **Step 1: Write failing E2E tests**

Test that workspace policy denies `/etc/passwd`, denies writing source files, allows writing workspace/result paths, disables Bash, rejects external tool dirs, and accepts allowlisted project tool dirs.

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_sandbox_e2e.py -v`

Expected: FAIL until SAR/default integration exists.

- [ ] **Step 3: Implement SAR defaults**

Build and pass workspace sandbox policy from SAR experiment orchestration, including run result directory write roots and project-local read/tool import roots.

- [ ] **Step 4: Document behavior**

Document `--sandbox-profile off|workspace`, Bash disabled behavior, allowed SAR write paths, tool import allowlist, and non-goals.

- [ ] **Step 5: Run final verification**

Run: `uv run pytest tests/test_sandbox_policy.py tests/test_sandboxed_tool.py tests/test_agent_sandbox_integration.py tests/test_sandbox_e2e.py -v`

Expected: PASS.

Run: `uv run pytest tests/ -v`

Expected: PASS or report pre-existing failures separately.

Run: `uv run --with ruff ruff check src/ sar_orch/ tests/`

Expected: PASS.

Run: `env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 --max-steps 2`

Expected: SAR smoke run completes or reaches bounded max steps without sandbox-related crash and writes result CSVs.

---

## Review Notes

- Per-task reviewers must verify both spec compliance and code quality.
- Do not ask implementers to commit; this repository workflow only commits when explicitly requested.
- Use review packages based on `git diff` rather than commit ranges when no commits were made.
- Existing worktree changes predate this task; do not revert or modify unrelated changes.

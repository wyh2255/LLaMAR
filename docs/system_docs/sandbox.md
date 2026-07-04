---
日期: 2026-07-04
文档类型: 技术文档
文档概述: 沙箱策略（Sandbox Policy）的设计、配置与行为说明 — 包括 profiles、路径校验、和限制
---

# Sandbox Policy

## Overview

The sandbox restricts agent file-system access and disables Bash execution.
It applies to **all A2A agent tools** — workers, coordinators (router), and
verifiers — by wrapping tools in `SandboxedTool` that intercepts
`read_file`/`write_file`/`edit_file` and `bash`/`bash_output`/`bash_kill`.

## Profiles

Two profiles are available via `--sandbox-profile`:

### `off` (default for standalone CLI)

- `SandboxPolicy.off()` — **no restrictions**
- Every `check_read` / `check_write` / `check_tool_import_dir` succeeds
- Bash is **not blocked by the policy** (the `SandboxedTool` passes through
  when `_enabled` is `False`)
- Useful for local debugging and development

### `workspace` (default for SAR experiments)

- `SandboxPolicy.workspace()` — **path-restricted**, **Bash disabled**
- All file paths are resolved relative to the workspace directory
- Only paths within allowed **read roots**, **write roots**, and
  **tool import roots** are permitted
- Bash is **always disabled** (`bash_enabled=False`)

#### Allowed SAR Write Paths

SAR experiments configure explicit write roots:

| Write Root | Content |
|------------|---------|
| `./workspace/` | Agent scratch files (relative paths resolve here) |
| `<experiment_log_dir>/` | Result CSVs (trajectory, agent interactions, token usage, summary) |

The `<experiment_log_dir>` is the auto-generated timestamp directory created
by `ExperimentLogger` (e.g.
`sar_orch/results/sar_experiment_20260704_120000/`).

#### Allowed Read Paths

Only the workspace directory is readable by default. Any read of files
outside `./workspace/` (including system files, project source files, etc.)
is denied.

#### Tool Import Allowlist

Tool import roots default to the **project root** (the repository root).
Project-local tool directories (e.g. `sar_orch/tools/`) are accepted.
External directories outside the project tree are rejected.

#### Escape Prevention

- `..` path components trigger `SandboxViolation("Escape attempt")`
- Symlinks that resolve outside allowed roots are detected via `Path.resolve()`
- Absolute paths outside allowed roots are always denied

## Non‑goals

- **No network sandboxing** — agents can make outbound HTTP/WS connections
- **No memory sandboxing** — agents share the Python process heap
- **No subprocess sandboxing** — Python subprocesses inherit the parent's
  sandbox (or lack thereof)
- **No OS-level isolation** — no seccomp, AppArmor, or container boundaries

## Implementation

- `src/Agent/sandbox.py` — `SandboxPolicy`, `SandboxedTool`,
  `wrap_tools_with_sandbox`, `validate_custom_tools_dir`
- `src/Agent/worker_agent/build.py` — wraps worker tools at build time
- `src/Agent/router_agent/build.py` — wraps router/verifier tools at build time
- `src/a2a/worker/cli.py` — standalone worker CLI `--sandbox-profile`
- `src/a2a/coordinator/cli.py` — standalone coordinator CLI `--sandbox-profile`
- `sar_orch/experiment.py` — SAR experiment `--sandbox-profile` (default: `workspace`)
- `tests/test_sandbox_policy.py` — policy unit tests
- `tests/test_sandboxed_tool.py` — sandbox tool wrapper tests
- `tests/test_agent_sandbox_integration.py` — CLI plumbing signature tests
- `tests/test_sandbox_e2e.py` — acceptance tests for workspace behavior

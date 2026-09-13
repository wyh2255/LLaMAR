---
日期: 2026-07-26（2026-09-03 校准对齐代码现状）
文档类型: 技术文档
文档概述: 沙箱策略（Sandbox Policy）的设计、配置与行为说明 — 包括 profiles、路径校验、和限制
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）；核对口径：类/函数名 grep -n，行号以当前工作区实测为准。`src/Agent/sandbox.py` 自 ffee5a6（2026-07-04）后零改动。
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

SAR experiments configure explicit write roots
(`sar_orch/experiment.py:524–530`：`SandboxPolicy.workspace(project_root=…,
workspace_dir="./workspace", write_roots=[<experiment_log_dir>])`)：

| Write Root | Content |
|------------|---------|
| `<experiment_log_dir>/` | Result CSVs (trajectory, agent interactions, token usage, summary) |

⚠ 注意：显式传入 `write_roots` 会**整体替换**默认值（`sandbox.py:49–52`），
因此在 SAR 实验的 workspace profile 下 `./workspace/` **不再是 write root**——
它默认可读（read root）但不可写；agent 写相对路径 scratch 文件会被拒绝。
（standalone worker/coordinator CLI 的 workspace profile 不传 write_roots，
此时 `./workspace/` 仍是唯一读写根。）

The `<experiment_log_dir>` is the auto-generated timestamp directory created
by `ExperimentLogger` (e.g.
`sar_orch/results/20260704_120000_s1_s42_a2/`).

#### Allowed Read Paths

Only the workspace directory is readable by default. Any read of files
outside `./workspace/` (including system files, project source files, etc.)
is denied.

#### Tool Import Allowlist

Tool import roots default to the **project root** (the repository root).
Project-local tool directories (e.g. `sar_orch/tools/`) are accepted.
External directories outside the project tree are rejected.

#### Escape Prevention

- `..` path components in `check_read` / `check_write` trigger
  `SandboxViolation("Escape attempt")` (note: `check_tool_import_dir`
  does not have this special-case detection; it rejects `..` via
  `Path.resolve()` with a generic violation message)
- Symlinks that resolve outside allowed roots are detected via `Path.resolve()`
- Absolute paths outside allowed roots are always denied

## Non‑goals

- **No network sandboxing** — agents can make outbound HTTP/WS connections
- **No memory sandboxing** — agents share the Python process heap
- **No subprocess sandboxing** — Python subprocesses inherit the parent's
  sandbox (or lack thereof)
- **No OS-level isolation** — no seccomp, AppArmor, or container boundaries
- **Host-side persistence is out of scope** — the long-term memory and
  diagnosis stores (`<coordinator_log_dir>/long_term/long_term.sqlite3`,
  `diagnosis/diagnosis.sqlite3`) are written directly by coordinator Python
  code via `sqlite3.connect`, never through an LLM tool call, so they are
  neither intercepted by nor require permission from `SandboxPolicy`. The
  evaluator-private truth recorder writes to `--truth-output-dir`, which the
  runtime forces to be *outside* the run results dir
  (`sar_orch/experiment.py:476–485`) — also host-side, not tool-channel.

## Behavioral Details (verified against code, 2026-09-03)

- `SandboxPolicy.off(project_root=…)` accepts but **silently ignores** the
  `project_root` argument (`sandbox.py:24–26`).
- Violations surface to the agent as `ToolResult(success=False, error=…)`
  fed back into the LLM loop, **not** as raised exceptions
  (`sandbox.py:187–188`) — the agent can see and react to the denial.
- Path checks trigger only when the tool call passes a `path` **keyword
  argument (`sandbox.py:180`, `_FILE_TOOLS` at :128); a tool that took a
  positional path would bypass validation. Current file tools
  (`worker_agent/tools/file_tools.py:109/197/260`) all use `path=` kwargs,
  and the agent loop calls `tool.execute(**arguments)`
  (`worker_agent/agent.py:751`), so the interception holds for the shipped
  toolset.
- `VerifierAgent`'s toolset (`query_workers`) contains no controlled tool
  names, so wrapping it is effectively a no-op (`verifier.py:181`).
- Bash disabling: `workspace()` defaults `bash_enabled=False`
  (`sandbox.py:36`); when a policy is enabled and a `_BASH_TOOLS` name
  (`bash`/`bash_output`/`bash_kill`, `sandbox.py:129`) is called,
  `SandboxedTool.execute` returns an error `ToolResult` without invoking the
  wrapped tool (`sandbox.py:170–176`).
- Additional SAR entry point not listed before:
  `sar_orch/launch_dashboard.py:73–76,105–112` (default `workspace`, same
  `write_roots=[log_dir]` substitution as experiment.py).

## Implementation

- `src/Agent/sandbox.py` — `SandboxPolicy` (:14), `SandboxViolation` (:10),
  `SandboxedTool` (:132–191), `wrap_tools_with_sandbox` (:194–201),
  `validate_custom_tools_dir` (:204–217)
- `src/Agent/worker_agent/build.py` — wraps worker tools at build time
  (`wrap_tools_with_sandbox` :179, `validate_custom_tools_dir` :297)
- `src/Agent/router_agent/build.py` — wraps tools for the `build_router_agent`/`build_router_controller` path (used by `agent_executor.py`'s `_controller`, a separate code path from `RouterAgent`/`VerifierAgent`) — wrap :176, validate :316
- `src/a2a/coordinator/router.py` / `src/a2a/coordinator/verifier.py` — wrap `RouterAgent` / `VerifierAgent` tools directly via `wrap_tools_with_sandbox()` (not through `router_agent/build.py`) — router :551, verifier :181
- `src/a2a/worker/cli.py` — standalone worker CLI `--sandbox-profile`
  (default `off` :54–56; workspace branch :115–118 passes only
  project_root+workspace_dir)
- `src/a2a/coordinator/cli.py` — standalone coordinator CLI `--sandbox-profile`
  (default `off` :77–79; workspace branch :120–123)
- `sar_orch/experiment.py` — SAR experiment `--sandbox-profile` (default:
  `workspace`; argparse :1130–1135, policy build :519–534, shared policy
  injected into coordinator :625 and every worker :728)
- `tests/test_sandbox_policy.py` — policy unit tests (off/workspace factories,
  read/write/tool-import boundary, `..` escape, symlink escape)
- `tests/test_sandboxed_tool.py` — sandbox tool wrapper tests
- `tests/test_agent_sandbox_integration.py` — CLI plumbing signature tests
- `tests/test_sandbox_e2e.py` — acceptance tests for workspace behavior

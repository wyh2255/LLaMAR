"""Sandbox policy — path sandboxing for Agent code execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxViolation(Exception):
    """Raised when a path access violates the sandbox policy."""


@dataclass(frozen=True)
class SandboxPolicy:
    _enabled: bool = True
    _project_root: Path | None = None
    _workspace_dir: Path | None = None
    _read_roots: tuple[Path, ...] = ()
    _write_roots: tuple[Path, ...] = ()
    _tool_import_roots: tuple[Path, ...] = ()
    _bash_enabled: bool = False

    @classmethod
    def off(cls, project_root: Path | str | None = None) -> SandboxPolicy:
        return cls(_enabled=False)

    @classmethod
    def workspace(
        cls,
        project_root: Path | str,
        workspace_dir: Path | str,
        read_roots: list[Path | str] | None = None,
        write_roots: list[Path | str] | None = None,
        tool_import_roots: list[Path | str] | None = None,
        bash_enabled: bool = False,
    ) -> SandboxPolicy:
        project_root = Path(project_root).resolve()
        if not project_root.is_dir():
            raise ValueError(f"project_root must exist: {project_root}")

        ws = (project_root / workspace_dir).resolve()

        if read_roots is None:
            read_list = [ws]
        else:
            read_list = [_resolve_root(project_root, r) for r in read_roots]

        if write_roots is None:
            write_list = [ws]
        else:
            write_list = [_resolve_root(project_root, r) for r in write_roots]

        if tool_import_roots is None:
            tool_list = [project_root]
        else:
            tool_list = [_resolve_root(project_root, r) for r in tool_import_roots]

        return cls(
            _enabled=True,
            _project_root=project_root,
            _workspace_dir=ws,
            _read_roots=tuple(read_list),
            _write_roots=tuple(write_list),
            _tool_import_roots=tuple(tool_list),
            _bash_enabled=bash_enabled,
        )

    def check_read(self, path: str | Path) -> Path:
        resolved = self._resolve(path)
        if not self._enabled:
            return resolved
        if not _within_any(resolved, self._read_roots):
            if ".." in Path(path).parts:
                raise SandboxViolation(f"Escape attempt: {path}")
            raise SandboxViolation(f"Path {path} is outside allowed read roots")
        return resolved

    def check_write(self, path: str | Path) -> Path:
        resolved = self._resolve(path)
        if not self._enabled:
            return resolved
        if not _within_any(resolved, self._write_roots):
            if ".." in Path(path).parts:
                raise SandboxViolation(f"Escape attempt: {path}")
            raise SandboxViolation(f"Path {path} is outside allowed write roots")
        return resolved

    def check_tool_import_dir(self, path: str | Path) -> Path:
        resolved = self._resolve(path)
        if not self._enabled:
            return resolved
        if not _within_any(resolved, self._tool_import_roots):
            raise SandboxViolation(f"Path {path} is outside allowed tool import roots")
        return resolved

    def _resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if not self._enabled:
            return p.resolve()
        if not p.is_absolute():
            ws = self._workspace_dir
            assert ws is not None
            p = ws / p
        return p.resolve()


def _resolve_root(project_root: Path, root: Path | str) -> Path:
    p = Path(root)
    if not p.is_absolute():
        p = project_root / p
    return p.resolve()


def _within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(root in path.parents or root == path for root in roots)


class ToolResult:
    """Sandbox tool execution result (duck-typed compat with worker/router ToolResult)."""

    def __init__(self, success: bool, content: str = "", error: str | None = None):
        self.success = success
        self.content = content
        self.error = error


_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})
_BASH_TOOLS = frozenset({"bash", "bash_output", "bash_kill"})


class SandboxedTool:
    """Wraps a Tool to enforce sandbox policy on file/bash operations.

    Delegates all properties to the wrapped tool. Intercepts ``execute()``
    to validate file paths via ``SandboxPolicy.check_read`` / ``check_write``
    and to block bash when ``_bash_enabled`` is ``False``.
    Unknown tools (e.g. SAR domain tools) pass through unchanged.
    """

    def __init__(self, tool: Any, policy: SandboxPolicy | None):
        self._wrapped = tool
        self._policy = policy

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def description(self) -> str:
        return self._wrapped.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._wrapped.parameters

    def to_schema(self) -> dict[str, Any]:
        return self._wrapped.to_schema()

    def to_openai_schema(self) -> dict[str, Any]:
        return self._wrapped.to_openai_schema()

    async def execute(self, *args: Any, **kwargs: Any) -> ToolResult | Any:
        policy = self._policy
        if policy is None or not policy._enabled:
            return await self._wrapped.execute(*args, **kwargs)

        name = self.name

        if name in _BASH_TOOLS:
            if not policy._bash_enabled:
                return ToolResult(
                    success=False,
                    content="",
                    error="Bash is disabled by sandbox policy",
                )
            return await self._wrapped.execute(*args, **kwargs)

        if name in _FILE_TOOLS:
            if "path" in kwargs:
                try:
                    if name == "read_file":
                        resolved = policy.check_read(kwargs["path"])
                    else:
                        resolved = policy.check_write(kwargs["path"])
                    kwargs = {**kwargs, "path": str(resolved)}
                except SandboxViolation as e:
                    return ToolResult(success=False, content="", error=str(e))
            return await self._wrapped.execute(*args, **kwargs)

        return await self._wrapped.execute(*args, **kwargs)


def wrap_tools_with_sandbox(tools: list, policy: SandboxPolicy | None) -> list:
    """Wrap each tool in a ``SandboxedTool`` if the policy is enabled.

    Returns the original list unchanged when *policy* is ``None`` or disabled.
    """
    if policy is None or not policy._enabled:
        return tools
    return [SandboxedTool(t, policy) for t in tools]


def validate_custom_tools_dir(
    path: str | Path | None, policy: SandboxPolicy | None
) -> Path | None:
    """Validate a custom-tools directory against the sandbox policy.

    Returns ``None`` when *path* is ``None``.
    When *policy* is ``None`` or disabled, returns the resolved *path* without
    validation.  Otherwise calls ``policy.check_tool_import_dir(path)``.
    """
    if path is None:
        return None
    if policy is None or not policy._enabled:
        return Path(path).resolve()
    return policy.check_tool_import_dir(path)

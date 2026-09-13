"""依赖方向守卫：内核（src/a2a、src/Agent）不得依赖环境编排层。

内核必须对具体环境（SAR / AI2Thor）保持零硬依赖——环境能力一律由装配层
经 ``create_server`` 注入点提供（finish_task_tool_factory /
environment_state_provider_factory / map_mcp_mount_hook /
mcp_session_lifecycle_provider）。

本测试用 AST 扫描而非文本 grep：

- 只统计真实 import（``import x.y`` / ``from x.y import z``）；
- 只统计动态导入的字面量调用（``importlib.import_module("x.y")``，含
  ``from importlib import import_module`` 后的裸名调用形式）；
- 注释 / docstring / 普通字符串字面量中的提及不算（AST 天然排除）；
- 相对 import（``from . import x``，level>0）不可能跨到环境层包，忽略。

P2b-2 改造后本文件必须 0 命中通过——任何命中 = 依赖方向回归。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KERNEL_TREES = ("src/a2a", "src/Agent")
FORBIDDEN_TOP_LEVEL = ("sar_orch", "ai2thor_orch")


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def _dynamic_import_literal_arg(node: ast.Call) -> str | None:
    """``importlib.import_module("x.y")`` / 裸 ``import_module("x.y")`` 的字面量参数。"""
    func = node.func
    is_import_module = (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    ) or (isinstance(func, ast.Name) and func.id == "import_module")
    if not is_import_module or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _collect_violations(tree: ast.AST, rel_path: Path) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_level(alias.name) in FORBIDDEN_TOP_LEVEL:
                    violations.append(f"{rel_path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level == 0
                and node.module
                and _top_level(node.module) in FORBIDDEN_TOP_LEVEL
            ):
                violations.append(
                    f"{rel_path}:{node.lineno}: from {node.module} import ..."
                )
        elif isinstance(node, ast.Call):
            target = _dynamic_import_literal_arg(node)
            if target is not None and _top_level(target) in FORBIDDEN_TOP_LEVEL:
                violations.append(
                    f"{rel_path}:{node.lineno}: import_module({target!r})"
                )
    return violations


def test_kernel_trees_have_no_env_orch_dependency():
    """src/a2a 与 src/Agent 对 sar_orch / ai2thor_orch 零实引用（含动态 import）。"""
    violations: list[str] = []
    scanned = 0
    for rel_root in KERNEL_TREES:
        root = REPO_ROOT / rel_root
        files = _iter_py_files(root)
        assert files, f"内核树为空或路径不存在（守卫失效）: {root}"
        for path in files:
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_collect_violations(tree, path.relative_to(REPO_ROOT)))
    assert scanned > 0, "未扫描到任何内核文件（守卫失效）"
    assert not violations, (
        "内核（src/a2a、src/Agent）不得 import 环境编排层（sar_orch / "
        "ai2thor_orch）；环境能力必须由装配层注入。命中：\n" + "\n".join(violations)
    )


def test_detector_flags_imports_but_not_comments():
    """自检：检测器对真实 import / 动态 import 命中，对注释、docstring、字符串跳过。"""
    sample = textwrap.dedent(
        '''
        """Docstring mentions sar_orch and  import ai2thor_orch  — 不算。"""
        import ast  # 合法
        import sar_orch.map_agent  # 注释里提及 ai2thor_orch 不算
        from ai2thor_orch.env import EnvStub
        from . import relative_ok
        import importlib
        _ = importlib.import_module("sar_orch.tools")
        from importlib import import_module
        _ = import_module("ai2thor_orch.env")
        TEXT = "sar_orch string literal not an import"
        '''
    )
    tree = ast.parse(sample)
    violations = _collect_violations(tree, Path("sample.py"))

    assert len(violations) == 4, f"检测器漏检或误报: {violations}"
    joined = "\n".join(violations)
    assert "import sar_orch.map_agent" in joined
    assert "from ai2thor_orch.env import ..." in joined
    assert "import_module('sar_orch.tools')" in joined
    assert "import_module('ai2thor_orch.env')" in joined

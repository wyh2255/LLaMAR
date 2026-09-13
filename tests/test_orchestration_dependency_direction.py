"""依赖方向守卫：通用编排层（src/orchestration）不得依赖任何环境层。

``src/orchestration`` 是 env-contract 的通用骨架（P4-2 起）：只允许依赖内核
（``a2a`` / ``Agent``）与标准库；一切环境能力必须经 ``EnvPack`` 契约注入。
本测试用 AST 扫描（含 ``importlib.import_module`` 字面量动态导入）——
任何对 ``sar_orch`` / ``ai2thor_orch`` 的实引用 = 契约回归。

扫描口径与 ``test_kernel_dependency_direction.py`` 一致：

- 只统计真实 import（``import x.y`` / ``from x.y import z``）；
- 只统计动态导入的字面量调用（``importlib.import_module("x.y")``，含
  ``from importlib import import_module`` 后的裸名调用形式）；
- 注释 / docstring / 普通字符串字面量中的提及不算（AST 天然排除）；
- 相对 import（``from . import x``，level>0）不跨层，忽略。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_TREE = "src/orchestration"
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


def test_orchestration_tree_has_no_env_dependency():
    """src/orchestration 对 sar_orch / ai2thor_orch 零实引用（含动态 import）。"""
    root = REPO_ROOT / ORCHESTRATION_TREE
    files = _iter_py_files(root)
    assert files, f"通用编排树为空或路径不存在（守卫失效）: {root}"
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_collect_violations(tree, path.relative_to(REPO_ROOT)))
    assert not violations, (
        "src/orchestration 不得 import 环境层（sar_orch / ai2thor_orch）；"
        "环境能力必须经 EnvPack 契约注入。命中：\n" + "\n".join(violations)
    )


def test_detector_flags_imports_but_not_comments():
    """自检：检测器对真实 import / 动态 import 命中，对注释、docstring、字符串跳过。"""
    sample = textwrap.dedent(
        '''
        """Docstring mentions sar_orch and  import ai2thor_orch  — 不算。"""
        import ast  # 合法
        import sar_orch.env_pack  # 注释里提及 ai2thor_orch 不算
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
    assert "import sar_orch.env_pack" in joined
    assert "from ai2thor_orch.env import ..." in joined
    assert "import_module('sar_orch.tools')" in joined
    assert "import_module('ai2thor_orch.env')" in joined

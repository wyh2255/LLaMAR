"""
MARoS a2a_lib 子模块兼容层 — 绕过 __init__.py 避免 ROS 2 rclpy 依赖。

a2a_lib/__init__.py 导入了 A2AWorkerNode、MapServerClient 等依赖 ROS 2 的模块。
当环境中没有 ROS 2 map_server 等包时，导入 a2a_lib 的任何子模块都会触发 __init__.py
从而导致 ModuleNotFoundError。

本模块使用 importlib 按文件路径直接加载需要的子模块，完全不触发 __init__.py。
这个方案不需要 sys.path 修改，也不需要安装 ROS 2。

Compat layer for MARoS a2a_lib submodules — bypasses __init__.py to avoid rclpy.

a2a_lib/__init__.py imports A2AWorkerNode and MapServerClient which depend on
ROS 2 and map_server. When these aren't installed, importing any a2a_lib
submodule triggers __init__.py and fails.

This module uses importlib to load the specific submodules we need by file path,
completely bypassing __init__.py. No sys.path manipulation needed.
"""
import importlib.util
import os
import sys
from pathlib import Path

# a2a_lib 子模块的物理路径（直接指向 a2a_lib/a2a_lib/，不经过包路径）
# 可通过环境变量 MAROS_A2A_LIB_DIR 覆盖，方便不同开发环境
_A2A_LIB_DIR = Path(
    os.environ.get("MAROS_A2A_LIB_DIR",
                   "/home/wyh/daily_work/MARoS/maros_ws/a2a_lib/a2a_lib")
)


def _load_by_path(fake_name: str, filename: str):
    """Load a Python file as a module by absolute path, bypassing package __init__.py.

    Args:
        fake_name: Module name to register in sys.modules (use unique prefix
                   to avoid collisions with the real package).
        filename: Relative filename under _A2A_LIB_DIR (e.g. "tool_decorator.py").
    """
    path = _A2A_LIB_DIR / filename
    spec = importlib.util.spec_from_file_location(fake_name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so that @dataclass / other
    # decorators can resolve type annotations via cls.__module__ lookup.
    sys.modules[fake_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── @tool decorator ──────────────────────────────────────────────────────────
_tool_decorator = _load_by_path("_maros_td", "tool_decorator.py")
tool = _tool_decorator.tool

# ── Skill abstraction ────────────────────────────────────────────────────────
_skill_mod = _load_by_path("_maros_skill", "skill.py")
Skill = _skill_mod.Skill

# ── a2a_lib shim ─────────────────────────────────────────────────────────────
# transport.py 使用 `from a2a_lib.skill import Skill` 这样的常规导入。
# 因为我们绕过了 a2a_lib/__init__.py（避免 ROS 2 依赖），需要在 sys.modules
# 中注册 shim 包，让 transport.py 的常规 import 能找到已加载的模块。
# transport.py uses normal imports like `from a2a_lib.skill import Skill`.
# Since we bypass a2a_lib/__init__.py (to avoid ROS 2 deps), we register shim
# packages in sys.modules so that transport.py's regular imports resolve.
import types as _types
if "a2a_lib" not in sys.modules:
    _a2a_lib_shim = _types.ModuleType("a2a_lib")
    _a2a_lib_shim.__path__ = [str(_A2A_LIB_DIR.parent)]  # __path__ needed for submodule imports
    sys.modules["a2a_lib"] = _a2a_lib_shim
if "a2a_lib.skill" not in sys.modules:
    sys.modules["a2a_lib.skill"] = _skill_mod

# ── A2A transport（惰性加载 — 需要 uvicorn，只在调用 start_a2a_transport 时加载） ──
def get_start_a2a_transport():
    """Lazily load and return start_a2a_transport from a2a_lib.transport.

    transport.py imports uvicorn which may not be installed in minimal setups.
    Use lazy loading so that importing _maros_compat doesn't require uvicorn.
    """
    _transport_mod = _load_by_path("_maros_transport", "transport.py")
    return _transport_mod.start_a2a_transport

__all__ = ["tool", "Skill", "get_start_a2a_transport"]

"""AI2Thor orchestration-layer tests — import-path bootstrap.

与仓库根 ``tests/conftest.py`` 同款修正：venv 的可编辑安装 .pth 把项目根与
``src`` 追加到 sys.path 末尾，site-packages 里的 PyPI ``a2a-sdk`` 会遮蔽项目
自身的 ``src/a2a``（其 ``coordinator`` / ``worker`` / ``builtin_tools`` 子包随之
不可导入，collection 直接报 ``ModuleNotFoundError: No module named
'a2a.coordinator'``）。无条件把 ``src`` 移到 sys.path 最前，保证项目 ``a2a``
优先；单跑本目录（``pytest ai2thor_orch/tests/``）时也能正确解析。
"""

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent.parent / "src")
if SRC_DIR in sys.path:
    sys.path.remove(SRC_DIR)
sys.path.insert(0, SRC_DIR)

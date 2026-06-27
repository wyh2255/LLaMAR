"""A2A - 局域网多智能体协同系统。"""

from __future__ import annotations

import os
import sys


# Extend __path__ with SDK's a2a directories found in sys.path.
# The project's a2a package (src/a2a/) contains coordinator/, worker/, shared/
# subpackages, while the a2a-sdk package (site-packages/a2a/) contains server/,
# client/, types/, etc.  By adding the SDK's directory to __path__ we let Python
# resolve subpackages from both locations under the single `a2a` namespace.
_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _candidate = os.path.join(_p, "a2a")
    if (
        os.path.isdir(_candidate)
        and _candidate != _this_dir
        and _candidate not in __path__
    ):
        __path__.append(_candidate)

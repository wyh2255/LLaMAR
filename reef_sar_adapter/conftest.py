"""Test bootstrap.

The tests import ``reef_sar_adapter`` itself, which the editable install in the
reef environment provides; putting the source parent on ``sys.path`` as well
keeps a plain checkout runnable without installing anything. The reef imports
inside the tests resolve from that same environment (the acceptance step
installs both).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent
_PARENT = str(_PACKAGE.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

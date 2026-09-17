"""Project-side additions to ``a2a.utils`` — shared repo-prompt loader.

``a2a.utils`` is the first subpackage name claimed by BOTH sides of the
``a2a`` namespace: the installed ``a2a-sdk`` ships
``a2a/utils/{constants,proto_utils,errors,task,telemetry,...}`` while this
repo adds ``a2a/utils/prompt_loader.py``.  Because ``src/a2a/__init__.py``
extends ``__path__`` with the SDK directory, a plain project-side package
would win the name lookup and hide the SDK modules (breaking e.g.
``from a2a.utils.constants import TransportProtocol``), so this module
repeats the same ``__path__`` merge one level down and re-exports the SDK's
public names — resolution behaves exactly as before the directory existed.

See ``src/a2a/__init__.py`` for the parent-level merge.
"""

from __future__ import annotations

import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _candidate = os.path.join(_p, "a2a", "utils")
    if (
        os.path.isdir(_candidate)
        and _candidate != _this_dir
        and _candidate not in __path__
    ):
        __path__.append(_candidate)

# Re-export the a2a-sdk public ``a2a.utils`` surface (mirrors the installed
# ``a2a/utils/__init__.py`` so ``from a2a.utils import <name>`` keeps working).
from a2a.utils import proto_utils  # noqa: E402
from a2a.utils.constants import (  # noqa: E402
    AGENT_CARD_WELL_KNOWN_PATH,
    DEFAULT_RPC_URL,
    TransportProtocol,
)
from a2a.utils.proto_utils import to_stream_response  # noqa: E402

__all__ = [
    "AGENT_CARD_WELL_KNOWN_PATH",
    "DEFAULT_RPC_URL",
    "TransportProtocol",
    "proto_utils",
    "to_stream_response",
]

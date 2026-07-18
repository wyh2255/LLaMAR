"""AliasRegistry — bi-directional mapping between raw AI2Thor objectIds and safe aliases.

Raw objectIds (e.g. ``Mug|-01.5|+00.9|+02.3``) contain absolute world coordinates
and must never be exposed to worker agents.  The AliasRegistry replaces them with
stable aliases like ``Mug_1``, ``Mug_2``, ``CounterTop_1``, etc.

Usage::

    reg = AliasRegistry()
    alias = reg.register("Mug|-01.5|+00.9|+02.3")   # -> "Mug_1"
    reg.alias("Mug|-01.5|+00.9|+02.3")               # -> "Mug_1"
    reg.redact("You see Mug|-01.5|+00.9|+02.3 on CounterTop|+00.0|...")
    # -> "You see Mug_1 on CounterTop_1"
    reg.is_raw_id_leaked("Still Mug_1 visible")       # -> False
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import DefaultDict


class AliasRegistry:
    """Maps raw AI2Thor objectIds to human-readable aliases.

    Thread-safe for concurrent reads (reads from :meth:`alias` and
    :meth:`redact` are lock-free).  :meth:`register` may be called from
    multiple threads safely because :class:`defaultdict` + ``list.append``
    are atomic for CPython's GIL.
    """

    _RAW_OBJECT_ID_PATTERN: re.Pattern = re.compile(
        r"[A-Za-z]\w*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*\|[+-]?\d+\.?\d*"
    )
    """Matches raw AI2Thor objectIds like ``Mug|-01.5|+00.9|+02.3``."""

    def __init__(self) -> None:
        # raw_id -> alias
        self._raw_to_alias: dict[str, str] = {}
        # alias -> raw_id
        self._alias_to_raw: dict[str, str] = {}
        # type_name -> counter (e.g. "Mug" -> 2)
        self._counters: DefaultDict[str, int] = defaultdict(int)

    def register(self, raw_id: str) -> str:
        """Register a raw objectId and return its alias.

        Repeated calls with the same ``raw_id`` return the same alias.
        """
        existing = self._raw_to_alias.get(raw_id)
        if existing is not None:
            return existing
        # Extract type name: everything before the first '|'
        pipe_idx = raw_id.find("|")
        type_name = raw_id[:pipe_idx] if pipe_idx > 0 else raw_id
        self._counters[type_name] += 1
        alias = f"{type_name}_{self._counters[type_name]}"
        self._raw_to_alias[raw_id] = alias
        self._alias_to_raw[alias] = raw_id
        return alias

    def alias(self, raw_id: str) -> str | None:
        """Return the alias for a registered raw_id, or ``None``."""
        return self._raw_to_alias.get(raw_id)

    def raw(self, alias: str) -> str | None:
        """Reverse-lookup: return the raw_id for a given alias, or ``None``."""
        return self._alias_to_raw.get(alias)

    def redact(self, text: str) -> str:
        """Replace every raw objectId in ``text`` with its alias.

        Unregistered raw ids are registered on the fly.
        """
        def _replace(match: re.Match) -> str:
            raw_id = match.group(0)
            return self.register(raw_id)
        return self._RAW_OBJECT_ID_PATTERN.sub(_replace, text)

    def is_raw_id_leaked(self, text: str) -> bool:
        """Return ``True`` if ``text`` contains any raw objectId pattern.

        This is an audit helper for test assertions::

            assert not registry.is_raw_id_leaked(worker_snapshot)
        """
        return bool(self._RAW_OBJECT_ID_PATTERN.search(text))

    @property
    def size(self) -> int:
        """Number of registered mappings."""
        return len(self._raw_to_alias)

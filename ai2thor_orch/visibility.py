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
    reg.dump("<run_dir>/alias_registry.json")         # run 终结落盘（F-seed）
    loaded = AliasRegistry.load("<run_dir>/alias_registry.json")

持久化（``dump`` / ``load``）供 F-frame replay 重建 alias→rawObjectId 映射
（replay 需把 ``Teleport(Fridge_1)`` 这类 alias 动作解析回 raw id；见设计
2026-09-17 §1.4/R3）。格式为双向映射 + 计数器快照，round-trip 无损。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict


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

    def aliases_for_type(self, type_name: str) -> list[str]:
        """All registered aliases whose raw id carries this AI2Thor type name.

        ``type_name`` is compared against the type segment of the raw objectId
        (``"Fridge|+00.0|+00.0|+01.0"`` → ``"Fridge"``), so the match is exact
        and independent of alias spelling.  Returns a **sorted** list: callers
        that accept a bare type name (e.g. ``navigate(target="Fridge")``)
        resolve only the unique-match case and otherwise fail closed with the
        candidate aliases — ambiguity is surfaced, never guessed.
        """
        if not type_name:
            return []
        matches = [
            alias
            for alias, raw_id in self._alias_to_raw.items()
            if raw_id.split("|", 1)[0] == type_name
        ]
        return sorted(matches)

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

    # ── 持久化（F-seed：run 终结 alias_registry.json）─────────────────────

    #: 落盘 JSON 的 schema 版本（``load`` 严格校验，未知版本 fail-fast）。
    DUMP_SCHEMA_VERSION = 1

    def to_dict(self) -> dict[str, Any]:
        """双向映射 + 计数器快照（``dump`` 的载荷；供测试/审计直接读）。"""
        return {
            "schema_version": self.DUMP_SCHEMA_VERSION,
            "raw_to_alias": dict(self._raw_to_alias),
            "alias_to_raw": dict(self._alias_to_raw),
            "counters": dict(self._counters),
        }

    def dump(self, path: str | Path) -> Path:
        """把注册表落盘到 ``path``（JSON；父目录自动创建，tmp+rename 原子替换）。

        F-frame replay 的 R3 前提：replay 需用 alias→rawObjectId 映射重放
        alias 动作。计数器一并落盘，``load`` 恢复后新注册不撞已有 alias。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AliasRegistry:
        """从 ``to_dict`` 载荷重建注册表（round-trip；非法载荷 fail-fast）。"""
        schema = data.get("schema_version")
        if schema != cls.DUMP_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 alias_registry schema_version: {schema!r}"
                f"（本版本只接受 {cls.DUMP_SCHEMA_VERSION}）"
            )
        missing = [
            key for key in ("raw_to_alias", "alias_to_raw", "counters") if key not in data
        ]
        if missing:
            raise ValueError(f"alias_registry 载荷缺字段: {missing}")

        registry = cls()
        registry._raw_to_alias = {
            str(raw_id): str(alias) for raw_id, alias in data["raw_to_alias"].items()
        }
        registry._alias_to_raw = {
            str(alias): str(raw_id) for alias, raw_id in data["alias_to_raw"].items()
        }
        # 双向一致性校验：任一方向的映射必须与另一方向互逆（防手改/截断的
        # 半张表让 replay 静默解析错误）。
        for raw_id, alias in registry._raw_to_alias.items():
            if registry._alias_to_raw.get(alias) != raw_id:
                raise ValueError(
                    f"alias_registry 载荷双向映射不一致: {raw_id!r} <-> {alias!r}"
                )
        counters = data["counters"]
        registry._counters = defaultdict(
            int, {str(name): int(value) for name, value in counters.items()}
        )
        return registry

    @classmethod
    def load(cls, path: str | Path) -> AliasRegistry:
        """从 ``dump`` 产物重建注册表（round-trip；文件缺失/非法 fail-fast）。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(
                f"alias_registry.json 顶层必须是对象: {type(data).__name__}"
            )
        return cls.from_dict(data)

    @property
    def size(self) -> int:
        """Number of registered mappings."""
        return len(self._raw_to_alias)

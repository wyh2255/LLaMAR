#!/usr/bin/env python3
"""Phase 5 evaluator-private truth trace recorder (H1 card C7).

During a run the recorder reads SAR ground truth **read-only** from the
``SARBarrier`` (via ``get_env_snapshot()``) and emits one JSONL truth record
per canonical field claim, aligned with the naming conventions the canonical
Memory projection pipeline uses (``src/a2a/coordinator/server.py``
``_normalize_observation_projection_inputs`` and ``sar_orch/map/store.py``):

- ``domain``: ``embodied`` for agents, ``spatial`` for everything else.
- ``entity_id``: the exact object name workers observe (``CaldorFire_Region_1``,
  ``LostPersonTimmy``, ``DepositFacility``, ``Alice``, ...).  Unnamed objects
  are skipped — the canonical pipeline ignores nameless evidence.
- ``field`` / ``value``: canonical projection field names and canonical value
  shapes — ``position`` as ``[x, y, z]`` ints, ``intensity`` /
  ``average_intensity`` / ``status`` as readable enum names (``Low``,
  ``Grounded``), ``fire_type`` / ``resource_type`` as readable labels
  (``Chemical`` / ``Non-chemical`` / ``Sand`` / ``Water``), agent ``inventory``
  as the normalized sorted resource list, and deposit supply counts per
  resource (``Sand`` / ``Water`` / ``Person``).

Hard H1 boundary: the recorder NEVER writes to canonical Memory, the Context,
the semantic map, or any artifact agents/workers can read during the run.  It
only appends to ``<truth-output-dir>/truth_trace.jsonl`` (default location is
outside the results dir) and, at run terminal, writes the evaluator-private
``truth_manifest.json``.  Everything is terminal-only evidence.

Step convention: claims use the 0-based env-step that worker observations /
canonical projections carry (``_build_structured_obs`` stamps ``step`` before
the barrier counter increments).  ``record_step(step_num)`` receives the 1-based
drained step-log number and emits claims at ``step_num - 1``.

Manifest (``truth_manifest.json``) carries the keys the terminal-only
``memory_projection_quality`` evaluator requires (``schema_version``,
``scope_id``, ``trace``) plus run context and a sha256 of the trace.  When no
canonical scope can be resolved (legacy memory mode), ``finalize`` returns
``None`` and nothing is written — the run fails cleanly.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from a2a.coordinator.memory.contracts import normalize_inventory

SCHEMA_VERSION = 1
RECORDER_VERSION = "truth-recorder-1.0.0"

#: Canonical claims always carry this marker inside the manifest so the
#: artifact is unambiguously evaluator-private (never candidate/agent evidence).
EVALUATOR_PRIVATE = True

TRACE_FILENAME = "truth_trace.jsonl"
MANIFEST_FILENAME = "truth_manifest.json"

#: Raw SAR resource/fire codes -> canonical readable labels (matches
#: ``Field.READABLE_TYPE_MAPPER_RESOURCE/FIRE`` after ``.capitalize()``).
_READABLE_RESOURCE = {"A": "Sand", "B": "Water", "PERSON": "Person"}
_READABLE_FIRE_TYPE = {"A": "Chemical", "B": "Non-chemical"}

#: Categories -> ``domain`` used by the canonical projection pipeline.
_EMBODIED_CATEGORY = "agents"


def _readable_enum(value: Any) -> str | None:
    """Normalize ``'Intensity.LOW'`` / ``'PersonStatus.GROUNDED'`` -> ``'Low'`` /
    ``'Grounded'`` — the same shape workers observe and canonical projects."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "." in text:
        text = text.rsplit(".", 1)[1]
    text = text.lower()
    return text[:1].upper() + text[1:] if text else None


def _readable_fire_type(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    return _READABLE_FIRE_TYPE.get(text, text.capitalize())


def _readable_resource(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    return _READABLE_RESOURCE.get(text, text.capitalize())


def _as_position(value: Any) -> list[int] | None:
    """Normalize any barrier position shape into ``[x, y, z]`` ints."""
    if value is None:
        return None
    if isinstance(value, dict):
        if all(axis in value for axis in ("x", "y", "z")):
            value = [value["x"], value["y"], value["z"]]
        else:
            return None
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            return None
        try:
            return [int(item) for item in value]
        except (TypeError, ValueError):
            return None
    if hasattr(value, "get") and callable(value.get):
        coords = value.get()
        if isinstance(coords, (list, tuple)) and len(coords) == 3:
            try:
                return [int(item) for item in coords]
            except (TypeError, ValueError):
                return None
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TruthRecorder:
    """Record per-env-step SAR ground truth into an evaluator-private trace.

    Reads only from ``barrier`` (never canonical Memory / Context / semantic
    map) and writes only under ``output_dir``.
    """

    def __init__(
        self,
        barrier: Any,
        output_dir: str | Path,
        *,
        run_id: str = "",
        scene: int | None = None,
        num_agents: int | None = None,
        seed: int | None = None,
    ) -> None:
        self._barrier = barrier
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id
        self._scene = scene
        self._num_agents = num_agents
        self._seed = seed
        self._scope_id: str | None = None
        self._trace_path = self._output_dir / TRACE_FILENAME
        self._last_recorded_step = -1
        self._claim_count = 0

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def trace_path(self) -> Path:
        return self._trace_path

    @property
    def manifest_path(self) -> Path:
        return self._output_dir / MANIFEST_FILENAME

    @property
    def scope_id(self) -> str | None:
        return self._scope_id

    def set_scope_id(self, scope_id: str | None) -> None:
        self._scope_id = scope_id

    # ── per-step recording ────────────────────────────────────────────────

    def record_step(self, step_num: int) -> list[dict[str, Any]]:
        """Emit claims for the env step that just completed (idempotent).

        ``step_num`` is the 1-based drained step-log number; claims carry the
        canonical 0-based observation step (``step_num - 1``).
        """
        if step_num <= self._last_recorded_step:
            return []
        claims = self._claims_for_step(step_num - 1)
        self._last_recorded_step = step_num
        if claims:
            self._append_claims(claims)
            self._claim_count += len(claims)
        return claims

    def _append_claims(self, claims: list[dict[str, Any]]) -> None:
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._trace_path, "a", encoding="utf-8") as fh:
            fh.writelines(
                json.dumps(claim, ensure_ascii=False, sort_keys=True) + "\n"
                for claim in claims
            )

    # ── claim construction ────────────────────────────────────────────────

    def _claims_for_step(self, step: int) -> list[dict[str, Any]]:
        try:
            snapshot = self._barrier.get_env_snapshot()
        except Exception:  # noqa: BLE001 - recorder must never break the run
            return []
        if not isinstance(snapshot, dict):
            return []
        claims: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for category in (
            "fires",
            "persons",
            "deposits",
            "reservoirs",
            "agents",
            "flammables",
        ):
            for obj_dict in snapshot.get(category, []) or []:
                if not isinstance(obj_dict, dict):
                    continue
                name = obj_dict.get("name")
                if not name:
                    continue
                key = (category, str(name))
                if key in seen:
                    continue
                seen.add(key)
                claims.extend(self._object_claims(category, obj_dict, str(name), step))
        return claims

    def _resolve_live(self, name: str) -> Any:
        """Best-effort read-only lookup of the live SAR object for enrichment.

        Fields the snapshot does not carry (deposit storage, reservoir
        resource type, flammable parent fire / fire type) are read from the
        live object when available; otherwise the affected claims are skipped.
        """
        try:
            env = getattr(self._barrier, "env", None)
            controller = getattr(env, "controller", None)
            field = getattr(controller, "field", None)
            if field is None or not hasattr(field, "name_get"):
                return None
            obj = field.name_get(name)
            return obj if obj is not None else None
        except Exception:  # noqa: BLE001 - defensive read-only fallback
            return None

    def _object_claims(
        self, category: str, obj_dict: dict, name: str, step: int
    ) -> list[dict[str, Any]]:
        domain = "embodied" if category == _EMBODIED_CATEGORY else "spatial"
        live = self._resolve_live(name)
        claims: list[dict[str, Any]] = []

        pos = _as_position(obj_dict.get("position"))
        if pos is not None:
            claims.append(self._claim(step, domain, name, "position", pos))

        if category == "agents":
            inventory = self._agent_inventory(obj_dict, live)
            if inventory is not None:
                claims.append(self._claim(step, domain, name, "inventory", inventory))
        elif category == "persons":
            status = self._readable_obj_field(obj_dict, live, "status")
            if status:
                claims.append(self._claim(step, domain, name, "status", status))
            load = self._int_obj_field(obj_dict, live, "load")
            if load is not None:
                claims.append(self._claim(step, domain, name, "load", load))
        elif category == "fires":
            fire_type = _readable_fire_type(
                getattr(live, "fire_type", None) or obj_dict.get("fire_type")
            )
            if fire_type:
                claims.append(self._claim(step, domain, name, "fire_type", fire_type))
            avg_intensity = _readable_enum(
                getattr(live, "average_intensity", None)
                or obj_dict.get("average_intensity")
            )
            if avg_intensity:
                claims.append(
                    self._claim(step, domain, name, "average_intensity", avg_intensity)
                )
        elif category == "flammables":
            intensity = _readable_enum(
                getattr(live, "intensity", None) or obj_dict.get("intensity")
            )
            if intensity:
                claims.append(self._claim(step, domain, name, "intensity", intensity))
            fire_type = _readable_fire_type(getattr(live, "fire_type", None))
            if fire_type:
                claims.append(self._claim(step, domain, name, "fire_type", fire_type))
            parent_fire = getattr(live, "parent_name", None) or obj_dict.get(
                "parent_fire"
            )
            if parent_fire:
                claims.append(
                    self._claim(step, domain, name, "parent_fire", str(parent_fire))
                )
        elif category == "reservoirs":
            resource_type = _readable_resource(getattr(live, "type", None))
            if resource_type:
                claims.append(
                    self._claim(step, domain, name, "resource_type", resource_type)
                )
            available = self._reservoir_available(obj_dict, live)
            if available is not None:
                claims.append(self._claim(step, domain, name, "available", available))
        elif category == "deposits":
            supplies = self._deposit_supplies(live)
            for resource, count in supplies:
                claims.append(self._claim(step, domain, name, resource, count))
        return claims

    @staticmethod
    def _claim(step: int, domain: str, entity_id: str, field: str, value: Any) -> dict:
        return {
            "step": step,
            "domain": domain,
            "entity_id": entity_id,
            "field": field,
            "value": value,
        }

    # ── per-category value readers ────────────────────────────────────────

    def _agent_inventory(self, obj_dict: dict, live: Any) -> list[str] | None:
        raw = None
        if live is not None:
            inventory = getattr(live, "inventory", None)
            if isinstance(inventory, dict):
                raw = {
                    _readable_resource(key) or key: value
                    for key, value in inventory.items()
                }
        if raw is None:
            raw = obj_dict.get("inventory")
        if raw is None:
            return None
        return normalize_inventory(raw)

    def _deposit_supplies(self, live: Any) -> list[tuple[str, int]]:
        if live is None:
            return []
        storage = getattr(live, "storage", None)
        if not isinstance(storage, dict):
            return []
        supplies: list[tuple[str, int]] = []
        for resource, count in storage.items():
            label = _readable_resource(resource)
            if label is None:
                continue
            try:
                supplies.append((label, int(count)))
            except (TypeError, ValueError):
                continue
        return supplies

    @staticmethod
    def _readable_obj_field(obj_dict: dict, live: Any, field: str) -> str | None:
        live_value = getattr(live, field, None) if live is not None else None
        value = live_value if live_value is not None else obj_dict.get(field)
        return _readable_enum(value)

    @staticmethod
    def _int_obj_field(obj_dict: dict, live: Any, field: str) -> int | None:
        live_value = getattr(live, field, None) if live is not None else None
        value = live_value if live_value is not None else obj_dict.get(field)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _reservoir_available(obj_dict: dict, live: Any) -> int | str | None:
        """Remaining reservoir supply: ``int`` when finite, ``'infinite'`` for
        unlimited reservoirs, ``None`` when unknown.

        Mirrors the barrier snapshot's ``available`` normalization so the
        recorder stays JSON-safe even though ``Reservoir.available`` is
        ``math.inf`` by default (all shipped scenes).  A raw ``Infinity``
        would break strict JSON consumers of ``truth_trace.jsonl``.
        """
        live_value = getattr(live, "available", None) if live is not None else None
        value = live_value if live_value is not None else obj_dict.get("available")
        if value is None:
            return None
        if isinstance(value, str):
            # Barrier snapshot fallback already normalizes inf -> "infinite";
            # a raw float(inf) would raise instead.
            return "infinite" if value.strip().lower() == "infinite" else None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return "infinite"
        return int(numeric)

    # ── terminal manifest ─────────────────────────────────────────────────

    def finalize(self, terminal_status: str) -> dict[str, Any] | None:
        """Freeze ``truth_manifest.json``; return it or ``None`` when the run has
        no canonical scope (legacy memory mode) so callers can skip cleanly."""
        if not self._scope_id:
            return None
        if not self._trace_path.is_file():
            # Touch an empty trace so the manifest digest is stable.
            self._append_claims([])
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "scope_id": self._scope_id,
            "trace": TRACE_FILENAME,
            "run_id": self._run_id,
            "scene": self._scene,
            "agents": self._num_agents,
            "seed": self._seed,
            "terminal_status": terminal_status,
            "truth_trace_sha256": _sha256_file(self._trace_path),
            "evaluator_private": EVALUATOR_PRIVATE,
            "generated_by": RECORDER_VERSION,
        }
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return manifest


__all__ = [
    "EVALUATOR_PRIVATE",
    "MANIFEST_FILENAME",
    "RECORDER_VERSION",
    "SCHEMA_VERSION",
    "TRACE_FILENAME",
    "TruthRecorder",
    "_as_position",
    "_readable_enum",
    "_readable_fire_type",
    "_readable_resource",
]

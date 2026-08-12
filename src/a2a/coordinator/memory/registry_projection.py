"""Phase 3: AgentCard registry projection（主方案 §3.2 / Phase 3）。

Pure functions that turn AgentCard metadata (as parsed into ``AgentInfo`` by
``AgentRegistry.register_from_agent_card``) into deterministic
``NormalizedProjectionInputV1`` claims with ``provenance=registry``,
``domain=embodied`` and ``env_step=None`` — static registry metadata is never
compared against physical steps (主方案 §3.2).

Scope boundary (D2, 2026-08-12): new-worker first-registration bootstrap only.
Reconnect capability sync / ``agent_card_changed`` messages / in-process
mutation are explicitly NOT in V1 and must not be added here.

``AgentInfo`` is only referenced for type hints (imported lazily under
``TYPE_CHECKING``) so this module stays importable from
``a2a.coordinator.agent_registry`` without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from a2a.coordinator.memory.contracts import (
    NormalizedProjectionInputV1,
    canonical_json_bytes,
    digest_bytes,
)

if TYPE_CHECKING:
    from a2a.coordinator.agent_registry import AgentInfo

__all__ = [
    "build_scope_bootstrap_inputs",
    "parse_agent_card_capabilities",
    "parse_agent_card_sensor_types",
    "registry_projection_inputs",
]

#: Skill ids reserved for backend/model metadata (never capability claims).
_METADATA_SKILL_IDS = ("backend", "model")

_SENSOR_TAG_PREFIX = "sensor_type:"


def parse_agent_card_capabilities(card: dict) -> list[str]:
    """``AgentSkill(id=<capability>)`` → sorted unique capability id list.

    Backend/model metadata skills (tags contain ``"backend"``/``"model"`` or
    skill id in ``("backend", "model")``) are excluded, matching the existing
    ``register_from_agent_card`` parser semantics (agent_registry.py:133-142).
    m1（P3 review）：tags 含 ``"metadata"`` 的 skill（如 sensors 噪音能力）同样
    排除，与 backend/model 的 metadata 语义统一；m2（P3 review）：非 dict 的
    畸形/恶意 skill 条目直接跳过，不抛 AttributeError。
    """
    capabilities: set[str] = set()
    for skill in card.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue  # m2：畸形/恶意 card 条目容错
        tags = skill.get("tags", []) or []
        skill_id = skill.get("id", "")
        if (
            "backend" in tags
            or "model" in tags
            or "metadata" in tags  # m1：sensors 等 metadata 噪音能力排除
            or skill_id in _METADATA_SKILL_IDS
        ):
            continue
        if skill_id:
            capabilities.add(skill_id)
    return sorted(capabilities)


def parse_agent_card_sensor_types(card: dict) -> list[str]:
    """``sensor_type:<slug>`` metadata tags → sorted unique slug list.

    A skill may carry multiple sensor tags; duplicate slugs collapse.  Cards
    without any valid tag yield ``[]`` (D3: no valid tag → no fact to write).
    """
    sensors: set[str] = set()
    for skill in card.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue  # m2（P3 review）：畸形/恶意 card 条目容错
        for tag in skill.get("tags", []) or []:
            if isinstance(tag, str) and tag.startswith(_SENSOR_TAG_PREFIX):
                slug = tag[len(_SENSOR_TAG_PREFIX):]
                if slug:
                    sensors.add(slug)
    return sorted(sensors)


def _claim_event_id(agent_id: str, payload_digest: str, field_name: str) -> str:
    """Deterministic per-field event id (same inputs → same id, no storm)."""
    return f"reg:{agent_id}:{payload_digest}:{field_name}"


def registry_projection_inputs(
    *,
    agent_id: str,
    capabilities: list[str],
    sensor_types: list[str],
    card_available: bool,
    scope_id: str,
) -> list[NormalizedProjectionInputV1]:
    """Build deterministic registry-sourced projection claims for one agent.

    - ``card_available=False`` (AgentCard fetch failed / minimal registration)
      → zero claims: *unknown* capability must never be persisted as *no
      capability* (D2).
    - ``card_available=True`` → a ``capability`` claim is always generated
      (even for an empty list — the card was readable); a ``sensor_type``
      claim is generated only when ``sensor_types`` is non-empty (D3).
    - ``event_id`` is fully deterministic in the inputs: the same card values
      produce the same ids, so the ingestor's content-keyed idempotency turns
      re-ingestion of the same snapshot into a typed ``duplicate`` with zero
      writes (same-card duplicate, D2).
    """
    if not card_available:
        return []
    # m3（P3 review）：内部防御性排序——调用方传未排序列表时，payload_digest
    # 与 event_id 仍稳定（content-keyed duplicate 不失效）；claim value 与
    # event_id 使用同一排序后列表，保证值与 id 一致。
    capabilities = sorted(capabilities)
    sensor_types = sorted(sensor_types)
    payload_digest = digest_bytes(
        canonical_json_bytes(
            {"capabilities": capabilities, "sensor_types": sensor_types}
        )
    )[:16]
    claims: list[NormalizedProjectionInputV1] = [
        NormalizedProjectionInputV1(
            scope_id=scope_id,
            event_id=_claim_event_id(agent_id, payload_digest, "capability"),
            sequence=0,
            env_step=None,  # 静态 registry 元数据：不与物理 step 比较（主方案 §3.2）
            actor_id=agent_id,
            provenance="registry",
            domain="embodied",
            entity_id=agent_id,
            entity_type="agent",
            field_name="capability",
            value=list(capabilities),
        )
    ]
    if sensor_types:
        claims.append(
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id=_claim_event_id(agent_id, payload_digest, "sensor_type"),
                sequence=0,
                env_step=None,
                actor_id=agent_id,
                provenance="registry",
                domain="embodied",
                entity_id=agent_id,
                entity_type="agent",
                field_name="sensor_type",
                value=list(sensor_types),
            )
        )
    return claims


def build_scope_bootstrap_inputs(
    *,
    scope_id: str,
    agents: list[AgentInfo],
) -> list[NormalizedProjectionInputV1]:
    """Scope bootstrap bundle: snapshot every card-available agent's
    capability/sensor_type metadata into one deterministic input list.

    Called from the server runtime-created hook *after* the canonical scope is
    activated (activate scope → snapshot online available cards → attach
    receipt sink).  Agents whose card fetch failed (``agent_card_available``
    False) contribute zero claims.
    """
    inputs: list[NormalizedProjectionInputV1] = []
    for agent in agents:
        inputs.extend(
            registry_projection_inputs(
                agent_id=agent.agent_id,
                capabilities=agent.capabilities,
                sensor_types=agent.sensor_types,
                card_available=agent.agent_card_available,
                scope_id=scope_id,
            )
        )
    return inputs

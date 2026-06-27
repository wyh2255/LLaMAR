"""A2A 直连引导器 - 负责 Worker 间的直连引导。"""

from __future__ import annotations

from a2a.coordinator.agent_registry import AgentRegistry, AgentNotFoundError
from a2a.shared.types import build_direct_connect_info


class MeshGuide:
    """A2A 直连引导器。使用统一的 AgentRegistry。"""

    def __init__(self, agent_registry: AgentRegistry) -> None:
        self._registry = agent_registry

    async def guide_direct_connection(
        self, from_worker_id: str, to_agent_id: str
    ) -> dict:
        try:
            target = self._registry.get(to_agent_id)
        except AgentNotFoundError as e:
            raise AgentNotFoundError(to_agent_id) from e

        return build_direct_connect_info(
            relay=False,
            target_endpoint=target.endpoint,
            target_worker_id=target.agent_id,
        )

    async def relay_via_coordinator(
        self, from_worker_id: str, to_agent_id: str
    ) -> dict:
        try:
            target = self._registry.get(to_agent_id)
        except AgentNotFoundError as e:
            raise AgentNotFoundError(to_agent_id) from e

        return build_direct_connect_info(
            relay=True,
            target_endpoint=target.endpoint,
            target_worker_id=target.agent_id,
        )

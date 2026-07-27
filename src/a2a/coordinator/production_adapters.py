"""Production MissionRuntime / TeamPartition network adapters.

Phase 4–6 unit tests inject fakes.  Real SAR production must wire these
adapters onto the Coordinator-owned MissionRuntimeManager and
TeamPartitionService so ``activate_plan_node`` and team ACK sagas reach
Workers over A2A.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from a2a.coordinator.team_partition_service import PartitionTransition

logger = logging.getLogger(__name__)


def build_dispatch_adapter(
    router: Any,
    *,
    coordinator_host: str,
    coordinator_port: int,
) -> Callable[[str, str, str, str, str], Awaitable[str]]:
    """Build a MissionRuntime dispatch adapter backed by RouterAgent.

    Signature matches ``DispatchAdapter``:
    ``(worker_id, prompt, callback_url, dispatch_id, context_id) -> worker_task_id``.
    """

    callback_url = (
        f"http://{coordinator_host}:{coordinator_port}/a2a/push-callback"
    )

    async def adapter(
        worker_id: str,
        prompt: str,
        _callback_url: str,
        dispatch_id: str,
        context_id: str,
    ) -> str:
        effective_callback = _callback_url or callback_url
        worker_task_id = await router.send_task_async(
            worker_id,
            prompt,
            effective_callback,
            dispatch_id,
            context_id=context_id,
        )
        if not worker_task_id:
            raise RuntimeError(
                f"Worker '{worker_id}' returned empty task id for dispatch {dispatch_id}"
            )
        return worker_task_id

    return adapter


def build_delivery_adapter(
    sender: Any,
    agent_registry: Any,
    worker_registry: Any | None = None,
) -> Callable[[PartitionTransition], Awaitable[dict[str, bool]]]:
    """Build a TeamPartition delivery adapter via CoordinatorSenderService.

    For each affected worker, deliver either TEAM_UPDATE (collaborative after
    assignment) or TEAM_REVOKE (singleton after assignment), waiting for the
    control-message terminal ACK.
    """

    def _resolve_endpoint(worker_id: str) -> str | None:
        if worker_registry is not None:
            try:
                worker = worker_registry.get(worker_id)
                endpoint = getattr(worker, "a2a_endpoint", None)
                if endpoint:
                    return endpoint
            except Exception:
                pass
        if agent_registry is not None:
            try:
                agent = agent_registry.get(worker_id)
                endpoint = getattr(agent, "endpoint", None)
                if endpoint:
                    return endpoint
            except Exception:
                pass
        return None

    async def adapter(transition: PartitionTransition) -> dict[str, bool]:
        outcomes: dict[str, bool] = {}
        after = transition.after or {}
        before = transition.before or {}
        team_secret = transition.team_secret

        # Shared collaborative roster for TEAM_UPDATE bodies.
        collab_members: list[str] = []
        for assignment in after.values():
            if assignment is None:
                continue
            if not getattr(assignment, "is_singleton", False) and not str(
                assignment.team_id
            ).startswith("solo:"):
                collab_members = list(assignment.member_ids)
                break
        if not collab_members:
            collab_members = list(transition.affected_workers)

        endpoints: dict[str, str] = {}
        for worker_id in collab_members:
            ep = _resolve_endpoint(worker_id)
            if ep:
                endpoints[worker_id] = ep

        for worker_id in transition.affected_workers:
            assignment = after.get(worker_id)
            endpoint = _resolve_endpoint(worker_id)
            if endpoint is None:
                logger.warning(
                    "Team delivery: no endpoint for worker %s (transition %s)",
                    worker_id,
                    transition.transition_id,
                )
                outcomes[worker_id] = False
                continue
            if assignment is None:
                outcomes[worker_id] = False
                continue

            try:
                to_singleton = bool(
                    getattr(assignment, "is_singleton", False)
                    or str(assignment.team_id).startswith("solo:")
                )
                if to_singleton:
                    # Moving to singleton: revoke the collaborative BEFORE team.
                    prev = before.get(worker_id)
                    revoke_team_id = (
                        prev.team_id if prev is not None else assignment.team_id
                    )
                    revoke_epoch = (
                        prev.epoch if prev is not None else assignment.epoch
                    )
                    result = await sender.send_team_revoke(
                        worker_id=worker_id,
                        worker_endpoint=endpoint,
                        team_id=revoke_team_id,
                        epoch=revoke_epoch,
                    )
                else:
                    if team_secret is None:
                        logger.warning(
                            "Team delivery: missing team_secret for collaborative "
                            "transition %s worker %s",
                            transition.transition_id,
                            worker_id,
                        )
                        outcomes[worker_id] = False
                        continue
                    result = await sender.send_team_update(
                        worker_id=worker_id,
                        worker_endpoint=endpoint,
                        team_id=assignment.team_id,
                        epoch=assignment.epoch,
                        members=list(assignment.member_ids) or collab_members,
                        endpoints=endpoints,
                        team_secret=team_secret,
                        objective=assignment.objective,
                    )
                outcomes[worker_id] = bool(result.get("success"))
                if not outcomes[worker_id]:
                    logger.warning(
                        "Team delivery failed for %s: %s",
                        worker_id,
                        result.get("error"),
                    )
            except Exception as exc:
                logger.error(
                    "Team delivery exception for %s: %s", worker_id, exc
                )
                outcomes[worker_id] = False

        return outcomes

    return adapter


def wire_production_adapters(
    *,
    mission_runtime_manager: Any,
    team_partition_service: Any,
    router: Any,
    agent_registry: Any,
    worker_registry: Any | None = None,
    coordinator_host: str,
    coordinator_port: int,
    coordinator_secret: bytes | None = None,
    sender: Any | None = None,
) -> dict[str, Any]:
    """Install production adapters on the Coordinator-owned services.

    Returns a small diagnostic dict of what was wired.
    """
    from a2a.coordinator.sender_service import CoordinatorSenderService

    dispatch_adapter = build_dispatch_adapter(
        router,
        coordinator_host=coordinator_host,
        coordinator_port=coordinator_port,
    )
    if hasattr(mission_runtime_manager, "set_dispatch_adapter"):
        mission_runtime_manager.set_dispatch_adapter(dispatch_adapter)
    active = getattr(mission_runtime_manager, "active_runtime", None)
    if active is not None and hasattr(active, "set_dispatch_adapter"):
        active.set_dispatch_adapter(dispatch_adapter)

    delivery_wired = False
    effective_sender = sender
    if coordinator_secret:
        if effective_sender is None:
            effective_sender = CoordinatorSenderService(
                coordinator_secret=coordinator_secret,
            )
        delivery_adapter = build_delivery_adapter(
            effective_sender,
            agent_registry,
            worker_registry,
        )
        team_partition_service.set_delivery_adapter(delivery_adapter)
        delivery_wired = True

    logger.info(
        "Production adapters wired: dispatch=True delivery=%s",
        delivery_wired,
    )
    return {
        "dispatch": True,
        "delivery": delivery_wired,
        "sender": effective_sender,
    }

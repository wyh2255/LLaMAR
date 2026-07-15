"""ConfigureTeamTool, DisbandTeamTool, and SyncTeamTool LLM-visible team management.

All tools use the CoordinatorTeamRegistry for state and the
CoordinatorSenderService for A2A envelope delivery.

Configure flow: validate  commit registry (atomic DeliveryPlan)  deliver
updates to new members  revoke removed/old members.  Registry commit is the
single atomic point; delivery failures are reported per-worker.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Agent.router_agent.tools.base import Tool, ToolResult

from a2a.coordinator.team_registry import CoordinatorTeamRegistry
from a2a.coordinator.sender_service import CoordinatorSenderService

if TYPE_CHECKING:
    from a2a.coordinator.agent_registry import AgentRegistry
    from a2a.coordinator.worker_registry import WorkerRegistry

logger = logging.getLogger(__name__)


def _resolve_endpoint(
    worker_id: str,
    worker_registry: WorkerRegistry | None,
    agent_registry: AgentRegistry | None,
) -> str | None:
    if worker_registry is not None:
        try:
            w = worker_registry.get(worker_id)
            return w.a2a_endpoint
        except Exception:
            pass
    if agent_registry is not None:
        try:
            a = agent_registry.get(worker_id)
            return a.endpoint
        except Exception:
            pass
    return None


class ConfigureTeamTool(Tool):
    """Configure a team of workers.

    Atomically replaces the entire team roster.  Delivers TEAM_UPDATE
    to new members and TEAM_REVOKE to removed members.

    Registry commit is atomic via configure_for_delivery which returns
    the exact DTO+secret committed.  Delivery is best-effort with
    explicit per-worker results.  On partial failure returns
    success=False with structured data so the LLM can use sync_team to
    retry.
    """

    def __init__(
        self,
        registry: CoordinatorTeamRegistry,
        sender: CoordinatorSenderService,
        agent_registry: AgentRegistry | None = None,
        worker_registry: WorkerRegistry | None = None,
    ):
        self._registry = registry
        self._sender = sender
        self._agent_registry = agent_registry
        self._worker_registry = worker_registry

    @property
    def name(self) -> str:
        return "configure_team"

    @property
    def description(self) -> str:
        return (
            "Configure a team of workers. Provide member_ids (list of worker names) "
            "and an optional objective. This atomically replaces the entire team roster. "
            "Returns per-worker delivery results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "member_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Worker IDs to include in the team (e.g. ['Alice', 'Bob'])",
                },
                "objective": {
                    "type": "string",
                    "description": "Optional team objective",
                },
            },
            "required": ["member_ids"],
        }

    async def execute(
        self,
        member_ids: list[str],
        objective: str | None = None,
    ) -> ToolResult:
        if not member_ids:
            return ToolResult(success=False, content="member_ids must be non-empty", error="empty_members")
        if len(member_ids) != len(set(member_ids)):
            return ToolResult(success=False, content=f"Duplicate member IDs: {member_ids}", error="duplicate_members")

        # Resolve member endpoints; missing = fail closed
        endpoints: dict[str, str] = {}
        missing: list[str] = []
        for mid in member_ids:
            ep = _resolve_endpoint(mid, self._worker_registry, self._agent_registry)
            if ep is None:
                missing.append(mid)
            else:
                endpoints[mid] = ep

        if missing:
            return ToolResult(
                success=False, content=f"Workers not found in registry: {missing}", error="worker_not_found",
            )
        if self._worker_registry is None and self._agent_registry is None:
            return ToolResult(
                success=False, content="No registry available cannot verify worker endpoints", error="no_registry",
            )

        # Capture previous team snapshot (BEFORE commit, for revoke)
        prev_plan = self._registry.snapshot_for_delivery()

        # Commit new team state atomically returns DeliveryPlan
        try:
            plan = self._registry.configure_for_delivery(
                member_endpoints=endpoints,
                objective=objective,
                agent_registry=self._agent_registry,
                worker_registry=self._worker_registry,
            )
        except (ValueError, Exception) as exc:
            return ToolResult(success=False, content=f"Team configuration failed: {exc}", error="configure_failed")

        delivery_results: list[dict[str, Any]] = []
        failed_ids: list[str] = []
        all_ok = True

        # Deliver TEAM_UPDATE to all new members using atomic plan
        for member_id, endpoint in endpoints.items():
            result = await self._sender.send_team_update(
                worker_id=member_id, worker_endpoint=endpoint,
                team_id=plan.dto.team_id, epoch=plan.dto.epoch,
                members=plan.dto.member_ids, endpoints=plan.dto.endpoints,
                team_secret=plan.team_secret, objective=plan.dto.objective,
            )
            if not result.get("success"):
                all_ok = False
                failed_ids.append(member_id)
            delivery_results.append({
                "worker": member_id,
                "status": "ok" if result.get("success") else "delivery_failed",
                "error": result.get("error"),
            })

        # Revoke removed members using previous plan epoch
        if prev_plan is not None:
            prev_member_ids = set(prev_plan.dto.member_ids)
            new_member_ids = set(member_ids)
            for removed_id in prev_member_ids - new_member_ids:
                removed_endpoint = prev_plan.dto.endpoints.get(removed_id)
                if removed_endpoint:
                    revoke_result = await self._sender.send_team_revoke(
                        worker_id=removed_id, worker_endpoint=removed_endpoint,
                        team_id=prev_plan.dto.team_id, epoch=prev_plan.dto.epoch,
                    )
                    if not revoke_result.get("success"):
                        all_ok = False
                        failed_ids.append(removed_id)
                    delivery_results.append({
                        "worker": removed_id,
                        "status": "revoked" if revoke_result.get("success") else "revoke_failed",
                        "error": revoke_result.get("error"),
                    })

        committed_str = f"Team '{plan.dto.team_id}' committed (epoch {plan.dto.epoch})."
        delivery_str = f"Delivery results: {delivery_results}."

        if all_ok:
            return ToolResult(success=True, content=f"{committed_str} All deliveries OK. {delivery_str}")

        return ToolResult(
            success=False,
            content=(
                f"{committed_str} Some deliveries failed. "
                f"Failed members: {failed_ids}. "
                f"Use 'sync_team' to retry these members. {delivery_str}"
            ),
            error="partial_delivery_failure",
            data={
                "committed": True,
                "team_id": plan.dto.team_id,
                "epoch": plan.dto.epoch,
                "failed_member_ids": failed_ids,
            },
        )


class DisbandTeamTool(Tool):
    """Disband the active team.

    Atomically captures the prior DeliveryPlan via disband_for_delivery
    (registry is authoritative immediately), then best-effort revokes
    members.
    """

    def __init__(self, registry: CoordinatorTeamRegistry, sender: CoordinatorSenderService):
        self._registry = registry
        self._sender = sender

    @property
    def name(self) -> str:
        return "disband_team"

    @property
    def description(self) -> str:
        return "Disband the currently active team. Sends revoke to all members."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        plan = self._registry.disband_for_delivery()
        if plan is None:
            return ToolResult(success=False, content="No active team to disband", error="no_active_team")

        revoked: list[str] = []
        failures: list[dict[str, Any]] = []

        for member_id in plan.dto.member_ids:
            endpoint = plan.dto.endpoints.get(member_id)
            if endpoint is None:
                failures.append({"worker": member_id, "error": "no_endpoint"})
                continue
            result = await self._sender.send_team_revoke(
                worker_id=member_id, worker_endpoint=endpoint,
                team_id=plan.dto.team_id, epoch=plan.dto.epoch,
            )
            if result.get("success"):
                revoked.append(member_id)
            else:
                failures.append({"worker": member_id, "error": result.get("error", "unknown")})

        if failures:
            return ToolResult(
                success=False,
                content=f"Team '{plan.dto.team_id}' disbanded but some revokes failed: {failures}",
                error="partial_revoke_failure",
                data={
                    "committed": True,
                    "disbanded_team_id": plan.dto.team_id,
                    "failed_member_ids": [f["worker"] for f in failures],
                },
            )
        return ToolResult(success=True, content=f"Team '{plan.dto.team_id}' disbanded. Revoked: {revoked}.")


class SyncTeamTool(Tool):
    """Resend the current team snapshot to specific workers.

    Does NOT rotate the secret or increment epoch.
    """

    def __init__(self, registry: CoordinatorTeamRegistry, sender: CoordinatorSenderService):
        self._registry = registry
        self._sender = sender

    @property
    def name(self) -> str:
        return "sync_team"

    @property
    def description(self) -> str:
        return (
            "Resend the current team snapshot to one or more workers. "
            "Provide member_ids to sync specific members, or omit to sync all. "
            "Does NOT rotate the team secret or increment epoch."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "member_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Worker IDs to sync (omit to sync all members)",
                },
            },
        }

    async def execute(self, member_ids: list[str] | None = None) -> ToolResult:
        plan = self._registry.snapshot_for_delivery()
        if plan is None:
            return ToolResult(success=False, content="No active team to sync", error="no_active_team")

        target_ids = member_ids if member_ids is not None else plan.dto.member_ids
        if not target_ids:
            return ToolResult(success=False, content="member_ids must be non-empty if provided", error="empty_members")

        invalid = [mid for mid in target_ids if mid not in plan.dto.member_ids]
        if invalid:
            return ToolResult(success=False, content=f"Not team members: {invalid}", error="not_team_members")

        results: list[dict[str, Any]] = []
        all_ok = True
        for member_id in target_ids:
            endpoint = plan.dto.endpoints.get(member_id)
            if endpoint is None:
                results.append({"worker": member_id, "status": "error", "error": "no_endpoint"})
                all_ok = False
                continue
            result = await self._sender.send_team_update(
                worker_id=member_id, worker_endpoint=endpoint,
                team_id=plan.dto.team_id, epoch=plan.dto.epoch,
                members=plan.dto.member_ids, endpoints=plan.dto.endpoints,
                team_secret=plan.team_secret, objective=plan.dto.objective,
            )
            if result.get("success"):
                results.append({"worker": member_id, "status": "ok"})
            else:
                results.append({"worker": member_id, "status": "delivery_failed", "error": result.get("error")})
                all_ok = False

        if all_ok:
            return ToolResult(success=True, content=f"Team sync complete: {results}")
        return ToolResult(success=False, content=f"Some syncs failed: {results}", error="partial_sync_failure")

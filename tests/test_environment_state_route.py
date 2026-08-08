"""Phase 4 — task-bound identity binding for ``/environment-state`` (security).

Identity must NOT come from a caller-selected ``worker_id`` with a forgeable
coordinator-wide shared-secret proof (any secret holder could mint a proof for
any worker).  Instead the route binds identity to the **server-issued, opaque
``worker_task_id``** (the A2A task id the coordinator assigned when dispatching
to this worker) and resolves ``worker_task_id → dispatch → worker_id``
server-side, exactly like the push-callback route.  The shared-secret proof
provides freshness + replay protection only.

Tests prove: an active worker sees its own data; a secret holder with valid
own task credentials cannot mint/use another worker's identity or see that
worker's data while it is active; unknown tasks, expired / replayed proofs, and
wrong-dispatch claims are rejected.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from a2a.coordinator.team_status_auth import TeamStatusProof
from a2a.shared.types import WorkerNode

SECRET = b"ENV-STATE-SECRET-0123456789abcdef"


@pytest.fixture
def env_state_server(tmp_path):
    """A real CoordinatorServer in shadow mode with canonical Memory attached."""
    from a2a.coordinator.memory.contracts import MemoryConfig
    from a2a.coordinator.memory.ingestor import MemoryIngestor, MemoryScopeFactory
    from a2a.coordinator.memory.store import MemoryStore
    from a2a.coordinator.server import create_server

    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope_factory = MemoryScopeFactory(
        MemoryConfig(experiment_id="run-1", memory_root=tmp_path)
    )
    ingestor = MemoryIngestor(store, scope_factory)
    server = create_server(
        verifier_enabled=False,
        log_dir=str(tmp_path / "logs"),
        memory_read_mode="shadow",
        callback_secret=SECRET,
        memory_config=MemoryConfig(experiment_id="run-1", memory_root=tmp_path),
        memory_ingestor=ingestor,
        coordinator_secret=SECRET,
    )
    assert server.memory_ingestor is not None
    return server, store, ingestor, tmp_path


def _register_worker(server, worker_id: str) -> None:
    """Register a worker in both registries (membership check requires it)."""
    from a2a.coordinator.agent_registry import AgentInfo

    server._agent_registry.register(
        AgentInfo(
            agent_id=worker_id,
            description="test worker",
            endpoint=f"http://localhost:9999/{worker_id}/",
            capabilities=["sar"],
        )
    )
    server._registry.register(
        WorkerNode(worker_id=worker_id, a2a_endpoint="http://localhost:9999/")
    )


def _admit_task(
    server, ingestor, *, worker_id: str, worker_task_id: str, state="DISPATCHING"
):
    """Admit a context, create an active dispatch, and bind the server-issued
    opaque ``worker_task_id`` to it (what the coordinator does on dispatch)."""
    manager = server.mission_runtime_manager
    runtime = manager.admit(f"ctx-{worker_id}")
    dispatch = runtime.create_dispatch("logical", worker_id)
    runtime.register_worker_task(dispatch.dispatch_id, worker_task_id)
    runtime.apply_physical_status(dispatch.dispatch_id, state, source="dispatch")
    scope_id = ingestor.scope_id_for(f"ctx-{worker_id}", 0)
    return runtime, dispatch, scope_id


def _admit_terminal_task(
    server, ingestor, *, worker_id: str, worker_task_id: str, terminal="COMPLETED"
):
    """Admit a context and drive a dispatch to a terminal state via a valid
    transition chain, with the task id bound (PREPARED->DISPATCHING->ACCEPTED->terminal)."""
    manager = server.mission_runtime_manager
    runtime = manager.admit(f"ctx-{worker_id}")
    dispatch = runtime.create_dispatch("logical", worker_id)
    runtime.register_worker_task(dispatch.dispatch_id, worker_task_id)
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    runtime.apply_physical_status(dispatch.dispatch_id, "ACCEPTED", source="dispatch")
    runtime.apply_physical_status(dispatch.dispatch_id, terminal, source="dispatch")
    scope_id = ingestor.scope_id_for(f"ctx-{worker_id}", 0)
    return runtime, dispatch, scope_id


def _task_proof_post(
    client, payload, worker_task_id, *, proof=None, timestamp=None, nonce=None
):
    """POST /environment-state with a proof bound to the opaque worker_task_id."""
    body_payload = dict(payload)
    if proof is None:
        proof = TeamStatusProof.generate(
            SECRET, worker_task_id, timestamp=timestamp, nonce=nonce
        )
    body_payload["proof"] = proof
    body = json.dumps(body_payload).encode("utf-8")
    return client.post(
        "/environment-state",
        content=body,
        headers={"content-type": "application/json"},
    )


def _seed_alice_embodied(ingestor, scope_id):
    from a2a.coordinator.memory.contracts import NormalizedProjectionInputV1

    ingestor.ingest_projection(
        [
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_pos",
                sequence=0,
                env_step=8,
                actor_id="Alice",
                provenance="worker_sensor_tool",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="position",
                value=[3, 4, 0],
            ),
            NormalizedProjectionInputV1(
                scope_id=scope_id,
                event_id="evt_inv",
                sequence=0,
                env_step=8,
                actor_id="Alice",
                provenance="worker_sensor_tool",
                domain="embodied",
                entity_id="Alice",
                entity_type="agent",
                field_name="inventory",
                value=["Water"],
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Positive: an active worker with its own task sees its own view
# ---------------------------------------------------------------------------


def test_positive_active_worker_sees_own_data(env_state_server):
    server, store, ingestor, tmp_path = env_state_server
    worker_id, task_id = "Alice", "task-alice-0001"
    _register_worker(server, worker_id)
    runtime, dispatch, scope_id = _admit_task(
        server, ingestor, worker_id=worker_id, worker_task_id=task_id
    )
    _seed_alice_embodied(ingestor, scope_id)

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await _task_proof_post(
                client,
                {"worker_task_id": task_id, "token_budget": 1000},
                task_id,
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["freshness"] == "FRESH"
    embodied = data["sections"]["embodied_state"]
    assert worker_id in embodied
    assert embodied[worker_id]["fields"]["position"]["value"] == [3, 4, 0]
    assert embodied[worker_id]["fields"]["inventory"]["value"] == ["Water"]


# ---------------------------------------------------------------------------
# Negative: secret holder with valid own credentials cannot impersonate Alice
# ---------------------------------------------------------------------------


def test_negative_secret_holder_cannot_impersonate_active_alice(env_state_server):
    """Mallory holds the shared secret and has her OWN valid task credentials,
    but she cannot mint/use Alice's identity or see Alice's data even while
    Alice is active.  Her task id resolves to Mallory, and the viewer claim
    must match the server-derived worker."""
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _register_worker(server, "Mallory")

    # One active runtime: Alice's dispatch is active and holds Alice's data.
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-team")
    alice_dispatch = runtime.create_dispatch("logical-alice", "Alice")
    runtime.register_worker_task(alice_dispatch.dispatch_id, "task-alice-1")
    runtime.apply_physical_status(
        alice_dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    mallory_dispatch = runtime.create_dispatch("logical-mallory", "Mallory")
    runtime.register_worker_task(mallory_dispatch.dispatch_id, "task-mallory-1")
    runtime.apply_physical_status(
        mallory_dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    scope_id = ingestor.scope_id_for("ctx-team", 0)
    _seed_alice_embodied(ingestor, scope_id)

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            # Mallory presents HER OWN task id + valid proof, but claims
            # viewer_id=Alice.  Server derives worker_id=Mallory from the task
            # binding → worker_mismatch, and Alice's data is never returned.
            resp = await _task_proof_post(
                client,
                {
                    "worker_task_id": "task-mallory-1",
                    "viewer_id": "Alice",
                    "token_budget": 1000,
                },
                "task-mallory-1",
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "environment_state_worker_mismatch" in resp.json()["detail"]
    assert "3, 4, 0" not in resp.text
    assert "Water" not in resp.text


def test_negative_unknown_worker_task_is_403(env_state_server):
    """A forged / unknown task id (which no dispatch binds) fails closed even
    with a valid proof, because identity comes from the server-side binding."""
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _admit_task(server, ingestor, worker_id="Alice", worker_task_id="task-alice-1")

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            # Mallory guesses a task id that was never issued → unknown task.
            resp = await _task_proof_post(
                client,
                {"worker_task_id": "task-ghost-999", "token_budget": 1000},
                "task-ghost-999",
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "environment_state_unknown_worker_task" in resp.json()["detail"]


def test_negative_unregistered_worker_task_is_403(env_state_server):
    """A task bound to a worker that is not a known member fails closed."""
    server, store, ingestor, tmp_path = env_state_server
    # Dispatch exists for Ghost but Ghost is NOT registered.
    _admit_task(server, ingestor, worker_id="Ghost", worker_task_id="task-ghost-1")

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            resp = await _task_proof_post(
                client,
                {"worker_task_id": "task-ghost-1", "token_budget": 1000},
                "task-ghost-1",
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "environment_state_unknown_worker" in resp.json()["detail"]


def test_negative_no_active_dispatch_is_403(env_state_server):
    """A worker with a registered task bound to a terminal dispatch fails closed."""
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _admit_terminal_task(
        server,
        ingestor,
        worker_id="Alice",
        worker_task_id="task-alice-done",
        terminal="COMPLETED",
    )

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            resp = await _task_proof_post(
                client,
                {"worker_task_id": "task-alice-done", "token_budget": 1000},
                "task-alice-done",
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "environment_state_no_active_dispatch" in resp.json()["detail"]


def test_negative_wrong_dispatch_claim_is_403(env_state_server):
    """Alice's own task may not claim another dispatch id (cross-dispatch)."""
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _register_worker(server, "Bob")

    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-team")
    alice_d = runtime.create_dispatch("la", "Alice")
    runtime.register_worker_task(alice_d.dispatch_id, "task-alice-1")
    runtime.apply_physical_status(alice_d.dispatch_id, "DISPATCHING", source="dispatch")
    bob_d = runtime.create_dispatch("lb", "Bob")
    runtime.register_worker_task(bob_d.dispatch_id, "task-bob-1")
    runtime.apply_physical_status(bob_d.dispatch_id, "DISPATCHING", source="dispatch")

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            resp = await _task_proof_post(
                client,
                {
                    "worker_task_id": "task-alice-1",
                    "current_dispatch_id": bob_d.dispatch_id,
                    "token_budget": 1000,
                },
                "task-alice-1",
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "environment_state_dispatch_mismatch" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Negative: expired / replayed proof
# ---------------------------------------------------------------------------


def test_negative_expired_proof_is_403(env_state_server):
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _admit_task(server, ingestor, worker_id="Alice", worker_task_id="task-alice-1")

    expired_ts = int(time.time()) - 120  # > max_age (60s) + skew (10s)

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            resp = await _task_proof_post(
                client,
                {"worker_task_id": "task-alice-1", "token_budget": 1000},
                "task-alice-1",
                timestamp=expired_ts,
            )
            return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"]


def test_negative_replayed_proof_is_403(env_state_server):
    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _admit_task(server, ingestor, worker_id="Alice", worker_task_id="task-alice-1")

    # Reuse the exact same proof (same timestamp + nonce) → nonce replay.
    proof = TeamStatusProof.generate(SECRET, "task-alice-1")

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            first = await _task_proof_post(
                client,
                {"worker_task_id": "task-alice-1", "token_budget": 1000},
                "task-alice-1",
                proof=proof,
            )
            second = await _task_proof_post(
                client,
                {"worker_task_id": "task-alice-1", "token_budget": 1000},
                "task-alice-1",
                proof=proof,
            )
            return first, second

    first, second = asyncio.run(_run())
    assert first.status_code == 200, first.text
    assert second.status_code == 403
    assert "replay" in second.json()["detail"]


# ---------------------------------------------------------------------------
# Worker client plumbing: task-bound identity in read_port mode
# ---------------------------------------------------------------------------


def test_worker_client_binds_proof_to_worker_task_id(env_state_server):
    """The SARWorkerStateProvider sends its own server-issued worker_task_id and
    a proof bound to it — not a caller-selected worker id."""
    from Agent.worker_agent.context import (
        ContextConfig,
        WorkerContextManager,
    )
    from sar_orch.worker_state_provider import SARWorkerStateProvider

    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    runtime, dispatch, scope_id = _admit_task(
        server, ingestor, worker_id="Alice", worker_task_id="task-alice-1"
    )
    _seed_alice_embodied(ingestor, scope_id)

    class RealClient:
        async def fetch(self, query_viewer):
            # Mimic the worker's real HTTP fetch against the coordinator.
            async with AsyncClient(
                transport=ASGITransport(app=server._app), base_url="http://test"
            ) as client:
                proof = TeamStatusProof.generate(SECRET, "task-alice-1")
                resp = await client.post(
                    "/environment-state",
                    json={
                        "worker_task_id": "task-alice-1",
                        "proof": proof,
                        "token_budget": 1000,
                    },
                )
                resp.raise_for_status()
                return resp.json()

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id("task-alice-1")
    provider.set_environment_state_client(RealClient())

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=provider,
    )
    import asyncio as _aio

    async def _fetch():
        await provider.fetch_environment_state_async()
        return ctx.assemble("system", [])

    assembled = _aio.run(_fetch())
    assert any(
        isinstance(m.content, str) and "## Environment State" in m.content
        for m in assembled
    )


def test_worker_http_fetch_sends_nonzero_budget_full_canonical_view(
    env_state_server, monkeypatch
):
    """The production HTTP fetch sends a nonzero token_budget derived from the
    context token budget, so the coordinator serves a non-truncated canonical
    section (never the budget=0 truncated-only view)."""
    from Agent.environment_state import TRUNCATED_KEY, Freshness
    from Agent.worker_agent.context import ContextConfig, WorkerContextManager
    from sar_orch.worker_state_provider import SARWorkerStateProvider

    server, store, ingestor, tmp_path = env_state_server
    _register_worker(server, "Alice")
    _admit_task(server, ingestor, worker_id="Alice", worker_task_id="task-alice-1")
    _seed_alice_embodied(ingestor, ingestor.scope_id_for("ctx-Alice", 0))

    import httpx as _httpx
    from httpx import ASGITransport

    _real_async_client = _httpx.AsyncClient

    class _PatchedClient:
        def __init__(self, **kwargs):
            self._real = _real_async_client(
                transport=ASGITransport(app=server._app), base_url="http://test"
            )

        async def __aenter__(self):
            return self._real

        async def __aexit__(self, *exc):
            await self._real.aclose()
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _PatchedClient)

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        environment_state_url="http://test",
        coordinator_secret=SECRET,
        token_limit=80000,
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id("task-alice-1")

    async def _fetch_and_render():
        await provider.fetch_environment_state_async()
        ctx = WorkerContextManager(
            config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
            state_provider=provider,
        )
        return ctx.assemble("system", [])

    assembled = asyncio.run(_fetch_and_render())

    # Provider's HTTP fetch latched nothing and cached the full canonical view.
    assert provider.rollout_rolled_back() is False
    assert provider.rollout_active() is True
    view = provider.query_environment_state(None)
    assert view.freshness == Freshness.FRESH
    sections = view.sections
    assert sections.get(TRUNCATED_KEY) is not True
    assert sections["embodied_state"].get("Alice") is not None

    block = " ".join(
        m.content
        for m in assembled
        if isinstance(m.content, str) and "## Environment State" in m.content
    )
    assert "Embodied State" in block
    assert "Alice" in block


# ---------------------------------------------------------------------------
# Startup dispatch-binding race: first fetch before register_worker_task_id
# ---------------------------------------------------------------------------


def test_worker_first_fetch_before_task_binding_recovers_fresh_without_rollback(
    env_state_server, monkeypatch
):
    """Reproduces the genuine H2 rollout race: the worker's very first
    /environment-state fetch races the coordinator's post-acceptance
    ``register_worker_task_id``.  The dispatch exists but the worker's task id
    is not yet bound, so the coordinator returns the typed
    ``environment_state_unknown_worker_task`` 403.  The worker's admission-aware
    bounded retry absorbs the window and eventually reaches a FRESH read-port
    view with NO read_port→legacy rollback.

    Unauthorized requests after admission still fail closed (server-side 403),
    and the same-request canonical/legacy mixing is never allowed."""
    import asyncio as _aio

    import httpx as _httpx
    from httpx import ASGITransport

    from Agent.environment_state import Freshness
    from sar_orch.worker_state_provider import SARWorkerStateProvider

    server, _store, ingestor, _tmp_path = env_state_server
    worker_id, task_id = "Alice", "task-alice-race"
    _register_worker(server, worker_id)

    manager = server.mission_runtime_manager
    runtime = manager.admit(f"ctx-{worker_id}")
    dispatch = runtime.create_dispatch("logical", worker_id)
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )
    # NOTE: worker_task_id deliberately NOT bound yet — exactly the window where
    # the worker's first fetch races register_worker_task_id.
    scope_id = ingestor.scope_id_for(f"ctx-{worker_id}", 0)
    _seed_alice_embodied(ingestor, scope_id)

    _real_async_client = _httpx.AsyncClient

    class _PatchedClient:
        def __init__(self, **kwargs):
            self._real = _real_async_client(
                transport=ASGITransport(app=server._app), base_url="http://test"
            )

        async def __aenter__(self):
            return self._real

        async def __aexit__(self, *exc):
            await self._real.aclose()
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _PatchedClient)

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        environment_state_url="http://test",
        coordinator_secret=SECRET,
        token_limit=80000,
        admission_retry_limit=10,
        admission_retry_delay=0.1,
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id(task_id)

    async def _race():
        # The coordinator binds the task id shortly after the worker's first
        # fetch attempt has been rejected as unknown_worker_task.
        async def _bind():
            await _aio.sleep(0.15)
            runtime.register_worker_task(dispatch.dispatch_id, task_id)

        bind_task = _aio.create_task(_bind())
        await provider.fetch_environment_state_async()
        await bind_task

    _aio.run(_race())

    # Transient binding-ordering 403 never trips the permanent rollback latch.
    assert provider.rollout_rolled_back() is False
    assert provider.rollout_active() is True
    view = provider.query_environment_state(None)
    assert view.freshness == Freshness.FRESH
    assert view.sections["embodied_state"].get("Alice") is not None
    assert view.sections["embodied_state"]["Alice"]["fields"]["position"]["value"] == [
        3,
        4,
        0,
    ]

    # Server-side ACL still fails closed for a genuinely unknown task id even
    # while the runtime is active (no relaxation of identity checks).
    async def _probe_unknown():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await _task_proof_post(
                client,
                {"worker_task_id": "task-ghost-race", "token_budget": 1000},
                "task-ghost-race",
            )

    resp = asyncio.run(_probe_unknown())
    assert resp.status_code == 403
    assert "environment_state_unknown_worker_task" in resp.json()["detail"]

"""Tests for the deterministic callback-binding dispatch deadlock fix (option A).

Root cause: SDK ``DefaultRequestHandlerV2.on_message_send`` with
``return_immediately=True`` starts the ActiveTask but still subscribes until the
first executor Task event.  The SDK EventConsumer awaits the push-notification
send BEFORE enqueueing that first event to subscribers, while Coordinator push
admission waits for ``worker_task_id`` to be bound from the very HTTP response —
a circular dependency that stalls every dispatch by the full push-retry budget
and can exhaust the sender's 6-attempt backoff.

Fix: ``a2a.worker.a2a_server._ReturnImmediatelyAwareRequestHandler`` answers a
``return_immediately`` request with an initial WORKING Task carrying the
generated worker task id/context id immediately (no subscription), then enqueues
the request for background execution.  These tests pin that behaviour with
mocks/stubs — no real LLM is invoked.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from a2a.helpers import new_text_message
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.tasks import (
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
    TaskUpdater,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
)
from httpx import ASGITransport
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.worker.a2a_server import (
    _assert_return_immediately_sdk_contract,
    _ReturnImmediatelyAwareRequestHandler,
)
from a2a.worker.callback_sender import CallbackSigner, SignedPushNotificationSender

SECRET = b"worker-coordinator-shared-secret-0123456789"
CALLBACK_URL = "http://coordinator/a2a/push-callback"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _OkResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _OkPushClient:
    """Records pushes and admits them all on the first attempt."""

    def __init__(self):
        self.requests: list[tuple[str, bytes, dict]] = []

    async def post(self, url, content=None, headers=None, **kwargs):
        self.requests.append((url, content or b"", headers or {}))
        return _OkResponse()


class _BlockingPushClient:
    """Records calls and then blocks forever.

    If a handler awaited the push send before replying, the enclosing
    ``asyncio.wait_for`` times out and the test fails.
    """

    def __init__(self):
        self.calls = 0

    async def post(self, url, content=None, headers=None, **kwargs):
        self.calls += 1
        await asyncio.Event().wait()


class _GatedExecutor:
    """Emulates AgentAdapter.execute but never produces events until released.

    The first event (initial Task, WORKING, artifact, COMPLETED) is only
    enqueued after :meth:`release` for that worker task id, so the test can
    deterministically order "ack → bind → executor events → pushes".
    """

    def __init__(self):
        self._gates: dict[str, asyncio.Event] = {}
        self.produced_events = 0
        self.started = asyncio.Event()
        self.done = asyncio.Event()
        self.task_ids: list[str] = []

    async def execute(self, context, event_queue):
        self.started.set()
        task_id = context.task_id
        gate = asyncio.Event()
        self._gates[task_id] = gate
        await gate.wait()

        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
            self.produced_events += 1
            self.task_ids.append(task.id)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        await updater.add_artifact(parts=[Part(text="result")], name="result")
        await updater.complete(message=new_text_message("done"))
        self.produced_events += 1
        self.done.set()

    async def cancel(self, context, event_queue):
        return None

    async def release(self, task_id: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            gate = self._gates.get(task_id)
            if gate is not None:
                gate.set()
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"executor gate for {task_id} never reached")


class _ImmediateExecutor:
    """Produces the full task lifecycle immediately (no gate)."""

    def __init__(self):
        self.executed = False

    async def execute(self, context, event_queue):
        self.executed = True
        task = context.current_task
        if task is None:
            from a2a.helpers.proto_helpers import new_task_from_user_message

            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        await updater.add_artifact(parts=[Part(text="result")], name="result")
        await updater.complete(message=new_text_message("done"))

    async def cancel(self, context, event_queue):
        return None


class _CoordinatorAdmission:
    """Simulates the Coordinator's push-callback admission: an unbound worker
    task id is rejected as ``unknown_worker_task`` until ``bind`` lands.

    Mirrors the real server.py callback gate (no HMAC here — the test focuses
    on the worker-side ordering, not on weakening Coordinator admission).
    """

    def __init__(self):
        self.bound: set[str] = set()
        self.rejected: list[str] = []
        self.admitted: list[str] = []

    def bind(self, task_id: str) -> None:
        self.bound.add(task_id)

    def handle(self, body: dict) -> JSONResponse:
        task_id = _callback_task_id(body)
        if task_id not in self.bound:
            self.rejected.append(task_id)
            return JSONResponse(
                {"status": "rejected", "reason": "unknown_worker_task"},
                status_code=401,
            )
        self.admitted.append(task_id)
        return JSONResponse({"status": "accepted"})


def _callback_task_id(body: dict) -> str:
    if "task" in body:
        return str(body["task"].get("id", ""))
    if "statusUpdate" in body:
        return str(body["statusUpdate"].get("taskId", ""))
    if "artifactUpdate" in body:
        return str(body["artifactUpdate"].get("taskId", ""))
    return ""


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _worker_card() -> AgentCard:
    return AgentCard(
        name="TestWorker",
        description="test worker",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=True),
        skills=[],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url="http://worker/api/v1/jsonrpc/",
            )
        ],
    )


def _build_handler(
    executor,
    push_client: Any = None,
    *,
    retry_delay: float = 0.05,
    retry_max_delay: float = 0.2,
) -> _ReturnImmediatelyAwareRequestHandler:
    push_config_store = InMemoryPushNotificationConfigStore()
    sender = SignedPushNotificationSender(
        httpx_client=push_client or _BlockingPushClient(),  # type: ignore[arg-type]
        config_store=push_config_store,
        signer=CallbackSigner("Alice", SECRET),
        callback_retry_attempts=6,
        callback_retry_delay=retry_delay,
        callback_retry_max_delay=retry_max_delay,
    )
    return _ReturnImmediatelyAwareRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=_worker_card(),
        push_config_store=push_config_store,
        push_sender=sender,
    )


def _dispatch_request(context_id: str | None = None, text: str = "do the thing"):
    params = SendMessageRequest(
        message=Message(role=Role.ROLE_USER, parts=[Part(text=text)])
    )
    if context_id:
        params.message.context_id = context_id
    params.configuration.return_immediately = True
    params.configuration.task_push_notification_config.url = CALLBACK_URL
    return params


def _build_worker_app(executor, push_client):
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes

    handler = _build_handler(executor, push_client)
    routes = list(create_agent_card_routes(handler._agent_card))
    routes.extend(create_jsonrpc_routes(handler, rpc_url="/api/v1/jsonrpc/"))
    return handler, Starlette(routes=routes)


def _build_coordinator_app(admission: _CoordinatorAdmission):
    async def callback_endpoint(request):
        body = await request.json()
        return admission.handle(body)

    return Starlette(
        routes=[
            Route("/a2a/push-callback", callback_endpoint, methods=["POST"]),
        ]
    )


async def _make_client(worker_app):
    from a2a.client import ClientConfig, create_client

    http = httpx.AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://worker"
    )
    config = ClientConfig(
        streaming=False,
        httpx_client=http,
        supported_protocol_bindings=["JSONRPC"],
    )
    client = await create_client(_worker_card(), config)
    return client, http


async def _send(client, params: SendMessageRequest) -> Task:
    responses = []
    async for sr in client.send_message(params):
        responses.append(sr)
    assert len(responses) == 1, "non-streaming dispatch yields exactly one response"
    assert responses[0].HasField("task")
    return responses[0].task


async def _wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def _shutdown(handler, wait: float = 1.0) -> None:
    """Drain naturally-pruning tasks before shutting down.

    Finished ActiveTasks prune themselves in ~50ms; shutting them down
    mid-winddown can hit the SDK's ``close(immediate=False)`` race and eat its
    10s timeout.  Stuck tasks (e.g. a never-released gated executor) are
    cancelled by ``shutdown_a2a_active_tasks`` instead."""
    pruned = await _wait_for(
        lambda: not handler._active_task_registry._active_tasks, timeout=wait
    )
    if pruned:
        return
    from a2a.shared.server_lifecycle import shutdown_a2a_active_tasks

    await shutdown_a2a_active_tasks(handler)


# ---------------------------------------------------------------------------
# SDK contract guard
# ---------------------------------------------------------------------------


def test_return_immediately_sdk_contract_is_present():
    """The fast-ack path reuses SDK internals; the guard must not raise and the
    members it depends on must exist on the pinned SDK."""
    _assert_return_immediately_sdk_contract()
    from a2a.server.agent_execution.active_task import ActiveTask

    assert callable(DefaultRequestHandlerV2._setup_active_task)
    assert callable(ActiveTask.enqueue_request)


# ---------------------------------------------------------------------------
# Unit: return_immediately fast-ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_return_immediately_acks_task_id_before_executor_or_push():
    """The ack Task (with the generated worker task id/context id) is returned
    before any executor event and before any push send completes."""
    executor = _GatedExecutor()
    push_client = _BlockingPushClient()
    handler = _build_handler(executor, push_client)
    params = _dispatch_request()

    result = await asyncio.wait_for(
        handler.on_message_send(params, ServerCallContext()), timeout=3.0
    )
    assert isinstance(result, Task)
    assert result.id, "ack must carry the generated worker task id"
    assert result.context_id, "ack must carry the generated context id"
    assert result.status.state == TaskState.TASK_STATE_WORKING

    # The reply was NOT gated behind any executor event or push send.
    assert executor.produced_events == 0
    assert push_client.calls == 0

    # The request WAS enqueued for background execution.
    await asyncio.wait_for(executor.started.wait(), timeout=3.0)

    await _shutdown(handler)


@pytest.mark.asyncio
async def test_return_immediately_worker_task_id_matches_background_task():
    """The id returned in the ack is the id the background executor uses, and the
    coordinator receives the very first push only after binding that id."""
    executor = _GatedExecutor()
    admission = _CoordinatorAdmission()
    worker_handler, worker_app = _build_worker_app(
        executor,
        httpx.AsyncClient(
            transport=ASGITransport(app=_build_coordinator_app(admission))
        ),
    )
    worker_client, worker_http = await _make_client(worker_app)

    try:
        got = await _send(worker_client, _dispatch_request(context_id="ctx-e2e-1"))
        assert got.context_id == "ctx-e2e-1"
        assert admission.rejected == [], "ack must precede any push"

        admission.bind(got.id)
        await executor.release(got.id)

        admitted = await _wait_for(lambda: any(t == got.id for t in admission.admitted))
        assert admitted, "push must be admitted after binding"
        assert got.id in executor.task_ids, "ack id must be the executor's task id"
        assert admission.rejected == [], "no unknown_worker_task burst"

        await asyncio.wait_for(executor.done.wait(), timeout=5.0)
    finally:
        await worker_client.close()
        await worker_http.aclose()
        await _shutdown(worker_handler)


# ---------------------------------------------------------------------------
# Unit: blocking (non-return-immediately) path is preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocking_dispatch_preserves_first_event_behaviour():
    """return_immediately=False delegates to the SDK blocking path: the reply is
    the terminal executor Task (with the artifact), not the synthetic WORKING
    ack."""
    executor = _ImmediateExecutor()
    push_client = _OkPushClient()
    handler = _build_handler(executor, push_client)

    params = _dispatch_request()
    params.configuration.return_immediately = False
    result = await asyncio.wait_for(
        handler.on_message_send(params, ServerCallContext()), timeout=3.0
    )
    assert isinstance(result, Task)
    assert result.id
    assert result.status.state == TaskState.TASK_STATE_COMPLETED
    assert len(result.artifacts) == 1, "blocking reply must carry executor artifacts"
    assert executor.executed is True
    assert push_client.requests, "the executor's events were pushed"

    await _shutdown(handler)


# ---------------------------------------------------------------------------
# E2E: dispatch integration — first push admitted after binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dispatch_first_push_admitted_after_binding():
    """Full round trip (SDK client → worker JSONRPC → gated executor → signed
    push → coordinator admission): the first push is admitted after binding and
    the sender never exhausts its 6-attempt budget."""
    executor = _GatedExecutor()
    admission = _CoordinatorAdmission()
    worker_handler, worker_app = _build_worker_app(
        executor,
        httpx.AsyncClient(
            transport=ASGITransport(app=_build_coordinator_app(admission))
        ),
    )
    worker_client, worker_http = await _make_client(worker_app)

    try:
        start = time.monotonic()
        got = await _send(worker_client, _dispatch_request(context_id="ctx-e2e-2"))
        ack_elapsed = time.monotonic() - start
        assert ack_elapsed < 2.0, f"ack must be immediate, took {ack_elapsed:.2f}s"
        assert got.id
        assert admission.rejected == [], "ack must precede the first push"

        admission.bind(got.id)
        await executor.release(got.id)

        admitted = await _wait_for(lambda: any(t == got.id for t in admission.admitted))
        assert admitted, "first push must be admitted after binding"
        assert admission.rejected == [], "no unknown_worker_task rejection at all"
        await asyncio.wait_for(executor.done.wait(), timeout=5.0)

        # The sender never retried the startup-binding race to exhaustion.
        assert not admission.rejected
    finally:
        await worker_client.close()
        await worker_http.aclose()
        await _shutdown(worker_handler)


# ---------------------------------------------------------------------------
# Regression: multiple sequential dispatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_dispatches_have_no_stall_and_no_unknown_worker_task_burst():
    """Several dispatches through the same worker, each ack → bind → run, must
    not stall (orchestration timeout) and must never produce an
    unknown_worker_task burst."""
    executor = _GatedExecutor()
    admission = _CoordinatorAdmission()
    worker_handler, worker_app = _build_worker_app(
        executor,
        httpx.AsyncClient(
            transport=ASGITransport(app=_build_coordinator_app(admission))
        ),
    )
    worker_client, worker_http = await _make_client(worker_app)

    try:
        num_disp = 3
        start = time.monotonic()
        for i in range(num_disp):
            got = await _send(
                worker_client, _dispatch_request(context_id=f"ctx-seq-{i}")
            )
            assert got.id, "every dispatch must return a worker task id"
            assert admission.rejected == [], f"dispatch {i} ack must precede any push"
            admission.bind(got.id)
            await executor.release(got.id)
            admitted = await _wait_for(
                lambda tid=got.id: any(t == tid for t in admission.admitted)
            )
            assert admitted, f"dispatch {i} pushes must be admitted"
        total = time.monotonic() - start

        await asyncio.wait_for(executor.done.wait(), timeout=5.0)
        assert len(executor.task_ids) == num_disp
        # No orchestration timeout: total stays well below the 600s budget.
        assert total < 10.0, f"sequential dispatches stalled ({total:.2f}s)"
        assert admission.rejected == [], "unknown_worker_task burst across dispatches"
        assert len(admission.admitted) >= num_disp
    finally:
        await worker_client.close()
        await worker_http.aclose()
        await _shutdown(worker_handler)

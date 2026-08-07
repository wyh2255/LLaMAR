"""Tests for Coordinator /a2a/push-callback — help_request 事件路由。"""

import hashlib
import json

import pytest
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a.coordinator.event_store import event_store

SECRET = b"coordinator-callback-secret-0123456789abcdef"


def _extract_worker_data_blocks(text: str) -> list[dict]:
    marker = "[DATA]"
    if marker not in text:
        return []
    blocks = []
    for chunk in text.split(marker)[1:]:
        raw = chunk.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_observation_from_status_text(text: str) -> dict | None:
    for block in _extract_worker_data_blocks(text):
        if (
            block.get("ev") != "tool_result"
            or block.get("tool_name") != "report_observation"
            or not block.get("success")
        ):
            continue
        content = block.get("content") or ""
        try:
            observation = json.loads(content)
        except json.JSONDecodeError:
            return None
        return observation if isinstance(observation, dict) else None
    return None


def _build_push_callback_app():
    """用 coordinator server.py 中相同的 push-callback 逻辑构造独立 ASGI app。"""

    _push_artifact_cache: dict[str, list[str]] = {}

    async def handle_push_notification(request):
        from a2a.types.a2a_pb2 import StreamResponse, TaskState
        from a2a.coordinator.event_store import event_store
        from a2a.coordinator.task_store import resolve_global_future

        body = await request.json()
        sr = StreamResponse()
        ParseDict(body, sr)

        task_id = None
        is_terminal = False

        if sr.HasField("task"):
            t = sr.task
            task_id = t.id
            state_name = TaskState.Name(t.status.state) if t.status.state else "UNKNOWN"
            is_terminal = t.status.state in (
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
            )
            if task_id:
                event_store.append(task_id, "status_update", state=state_name)
        elif sr.HasField("artifact_update"):
            au = sr.artifact_update
            task_id = au.task_id
            if au.HasField("artifact"):
                texts = [p.text for p in au.artifact.parts if p.text]
                if texts and task_id:
                    combined = " ".join(texts)
                    _push_artifact_cache.setdefault(task_id, []).extend(texts)
                    event_store.append(task_id, "artifact_update", text=combined)
        elif sr.HasField("status_update"):
            su = sr.status_update
            task_id = su.task_id
            if su.HasField("status"):
                state_name = (
                    TaskState.Name(su.status.state) if su.status.state else "UNKNOWN"
                )
                is_terminal = su.status.state in (
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                )
                if task_id:
                    event_store.append(task_id, "status_update", state=state_name)

                    if su.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                        question = ""
                        if su.status.HasField("message"):
                            question = " ".join(
                                p.text for p in su.status.message.parts if p.text
                            )
                        event_store.append(task_id, "help_request", text=question)

                    if su.status.HasField("message"):
                        status_text = " ".join(
                            p.text for p in su.status.message.parts if p.text
                        )
                        observation = _extract_observation_from_status_text(status_text)
                        if observation:
                            event_store.append(
                                task_id,
                                "observation_report",
                                text=status_text[:500],
                                observation=observation,
                            )

        if task_id and is_terminal:
            import asyncio

            async def _resolve_with_delay():
                await asyncio.sleep(0.1)
                parts = _push_artifact_cache.pop(task_id, [])
                text = " ".join(parts) if parts else "(no artifact text)"
                resolve_global_future(task_id, text)

            asyncio.create_task(_resolve_with_delay())

        return JSONResponse({"status": "ok"})

    routes = [Route("/a2a/push-callback", handle_push_notification, methods=["POST"])]
    return Starlette(routes=routes)


@pytest.fixture(autouse=True)
def clear_event_store():
    event_store.clear()


@pytest.fixture
def client():
    app = _build_push_callback_app()
    with TestClient(app) as c:
        yield c


def _make_observation_status_payload(task_id: str, data_json: str) -> dict:
    return {
        "statusUpdate": {
            "taskId": task_id,
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [
                        {
                            "text": f"[Result] report_observation: observed\n[DATA]\n{data_json}"
                        }
                    ],
                },
            },
        }
    }


def _make_artifact_payload(task_id: str, text: str) -> dict:
    return {
        "artifactUpdate": {
            "taskId": task_id,
            "artifact": {
                "parts": [{"text": text}],
            },
        },
    }


class TestStatusUpdate:
    def test_task_state_writes_status_update(self, client):
        """task 状态更新写入 status_update 事件。"""
        payload = {
            "task": {
                "id": "task-status-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200

        summary = event_store.get_summary(task_ids={"task-status-1"})
        assert "STATUS" in summary
        assert "COMPLETED" in summary

    def test_status_update_field_writes_status_update(self, client):
        """status_update 顶层字段写入 status_update 事件。"""
        payload = {
            "statusUpdate": {
                "taskId": "task-status-2",
                "status": {"state": "TASK_STATE_FAILED"},
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200

        summary = event_store.get_summary(task_ids={"task-status-2"})
        assert "STATUS" in summary
        assert "FAILED" in summary


class TestInputRequiredRouting:
    def test_input_required_writes_help_request_event(self, client):
        """INPUT_REQUIRED 状态应该写入 help_request 事件。"""
        payload = {
            "statusUpdate": {
                "taskId": "task-input-1",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "Where is the target?"}],
                    },
                },
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200

        summary = event_store.get_summary(task_ids={"task-input-1"})
        assert "HELP" in summary
        assert "Where is the target?" in summary

    def test_input_required_does_not_resolve_future(self, client):
        """INPUT_REQUIRED 不是 terminal，不应该 resolve_global_future。"""
        payload = {
            "statusUpdate": {
                "taskId": "task-input-2",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "Help?"}],
                    },
                },
            },
        }
        resp = client.post("/a2a/push-callback", json=payload)
        assert resp.status_code == 200
        # No way to directly check future wasn't resolved, but verify no terminal event
        summary = event_store.get_summary(task_ids={"task-input-2"})
        assert "COMPLETED" not in summary
        assert "FAILED" not in summary


class TestObservationRouting:
    def test_working_status_tool_result_report_observation_is_recorded(self, client):
        data_json = json.dumps(
            {
                "ev": "tool_result",
                "tool_name": "report_observation",
                "success": True,
                "content": json.dumps(
                    {
                        "reporter": "Alice",
                        "step": 4,
                        "object_type": "fire",
                        "name": "FireA",
                        "position": [1, 1, 0],
                        "attributes": {"status": "active"},
                    }
                ),
            }
        )
        resp = client.post(
            "/a2a/push-callback",
            json=_make_observation_status_payload("task-obs-1", data_json),
        )

        assert resp.status_code == 200
        summary = event_store.get_summary(task_ids={"task-obs-1"})
        observations = event_store.get_recent_observations()
        assert "OBSERVATION" in summary
        assert observations[0]["name"] == "FireA"


# ---------------------------------------------------------------------------
# Phase 2: authenticated Temporal shadow write through the real server
# ---------------------------------------------------------------------------


@pytest.fixture
def secure_server(tmp_path):
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
    )
    assert server.memory_ingestor is not None
    return server, store, ingestor, tmp_path


def _signed_post(client, server, path, payload, worker_id="Alice"):
    body = json.dumps(payload).encode("utf-8")
    body_sha256 = hashlib.sha256(body).hexdigest()
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    proof = CallbackProofV1.generate(SECRET, worker_id, body_sha256)
    return client.post(
        path,
        content=body,
        headers={
            "content-type": "application/json",
            "X-A2A-Worker-Id": worker_id,
            "X-A2A-Callback-Proof": proof,
        },
    )


def test_secure_mode_rejects_unsigned_callback_with_zero_writes(secure_server):
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    from sar_orch.map import SemanticMapStore

    sem = SemanticMapStore()
    server._semantic_map = sem

    # The DISPATCHING internal transition legitimately writes its control
    # lifecycle event through the bridge before the callback is rejected.
    baseline = store.temporal_event_count(scope_id)

    from httpx import ASGITransport, AsyncClient

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/a2a/push-callback",
                json={
                    "statusUpdate": {
                        "taskId": "worker-1",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                },
            )
            return resp

    import asyncio

    resp = asyncio.run(_run())
    assert resp.status_code == 401
    assert resp.json()["status"] == "rejected"
    # Zero domain writes from the rejected callback: the only canonical event is
    # the control-lifecycle receipt for the DISPATCHING transition (baseline).
    assert event_store.get_summary() == ""
    assert sem.snapshot()["recent_observations"] == []
    assert store.temporal_event_count(scope_id) == baseline
    assert dispatch.state.value == "DISPATCHING"


def test_secure_mode_rejects_tampered_body(secure_server):
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    baseline = store.temporal_event_count(scope_id)
    from httpx import ASGITransport, AsyncClient

    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "status": {"state": "TASK_STATE_WORKING"},
        }
    }
    signed = json.dumps(payload).encode("utf-8")
    tampered = json.dumps(
        {
            "statusUpdate": {
                "taskId": "worker-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
            }
        }
    ).encode("utf-8")
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    proof = CallbackProofV1.generate(
        SECRET, "Alice", hashlib.sha256(signed).hexdigest()
    )

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post(
                "/a2a/push-callback",
                content=tampered,
                headers={
                    "content-type": "application/json",
                    "X-A2A-Worker-Id": "Alice",
                    "X-A2A-Callback-Proof": proof,
                },
            )

    import asyncio

    resp = asyncio.run(_run())
    assert resp.status_code == 401
    assert store.temporal_event_count(scope_id) == baseline
    assert dispatch.state.value == "DISPATCHING"


def test_secure_mode_valid_signed_callback_writes_everywhere_and_redacts(
    secure_server,
):
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    from sar_orch.map import SemanticMapStore

    sem = SemanticMapStore(jsonl_path=str(tmp_path / "semantic_map.jsonl"))
    server._semantic_map = sem

    secret_text = "SUPERSECRET_CALLBACK_9f2c1"
    hmac_hex = "c" * 64
    status_text = "[Result] report_observation: obs\n[DATA]\n" + json.dumps(
        {
            "ev": "tool_result",
            "tool_name": "report_observation",
            "success": True,
            "content": json.dumps(
                {
                    "reporter": "Alice",
                    "step": 1,
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [1, 1, 0],
                    "note": f"Authorization: Bearer {secret_text} hmac={hmac_hex}",
                }
            ),
        }
    )
    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": status_text}]},
            },
        }
    }
    from httpx import ASGITransport, AsyncClient
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    body = json.dumps(payload).encode("utf-8")
    proof = CallbackProofV1.generate(SECRET, "Alice", hashlib.sha256(body).hexdigest())

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post(
                "/a2a/push-callback",
                content=body,
                headers={
                    "content-type": "application/json",
                    "X-A2A-Worker-Id": "Alice",
                    "X-A2A-Callback-Proof": proof,
                },
            )

    import asyncio

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Canonical Temporal event exists.
    events = store.temporal_events(scope_id)
    assert events, "canonical event must be written"
    serialized_events = json.dumps(events)
    assert secret_text not in serialized_events
    assert hmac_hex not in serialized_events

    # Legacy EventStore + SemanticMap JSONL have no raw secret either.
    assert secret_text not in json.dumps(event_store.get_summary())
    jsonl_text = (tmp_path / "semantic_map.jsonl").read_text()
    assert secret_text not in jsonl_text
    assert hmac_hex not in jsonl_text
    assert "FireA" in jsonl_text

    # State advanced via MissionRuntime (still the control truth).
    assert dispatch.state.value == "RUNNING"


def test_secure_mode_replayed_nonce_rejected(secure_server):
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    from httpx import ASGITransport, AsyncClient
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {"state": "TASK_STATE_WORKING"},
        }
    }
    body = json.dumps(payload).encode("utf-8")
    proof = CallbackProofV1.generate(SECRET, "Alice", hashlib.sha256(body).hexdigest())
    headers = {
        "content-type": "application/json",
        "X-A2A-Worker-Id": "Alice",
        "X-A2A-Callback-Proof": proof,
    }

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/a2a/push-callback", content=body, headers=headers
            )
            second = await client.post(
                "/a2a/push-callback", content=body, headers=headers
            )
            return first, second

    import asyncio

    baseline = store.temporal_event_count(scope_id)
    first, second = asyncio.run(_run())
    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["status"] == "rejected"
    # First callback wrote its callback event + the bundled control receipt
    # (WORKING advanced state); the replay added nothing.
    assert store.temporal_event_count(scope_id) == baseline + 2


def test_secure_mode_closed_scope_zero_projection(secure_server):
    """An authenticated callback for a closed scope writes no canonical data."""
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    event_store.clear()
    from httpx import ASGITransport, AsyncClient
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {"state": "TASK_STATE_WORKING"},
        }
    }
    body = json.dumps(payload).encode("utf-8")
    proof = CallbackProofV1.generate(SECRET, "Alice", hashlib.sha256(body).hexdigest())
    headers = {
        "content-type": "application/json",
        "X-A2A-Worker-Id": "Alice",
        "X-A2A-Callback-Proof": proof,
    }

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post(
                "/a2a/push-callback", content=body, headers=headers
            )

    import asyncio

    # The canonical scope for ctx-secure is still open -> ok.
    resp = asyncio.run(_run())
    assert resp.status_code == 200

    # Now close it; a fresh callback (new nonce) must not write a projection.
    ingestor.close_scope("ctx-secure", 0)
    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    before = store.temporal_event_count(scope_id)

    async def _run_with_new_nonce():
        fresh_proof = CallbackProofV1.generate(
            SECRET, "Alice", hashlib.sha256(body).hexdigest()
        )
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post(
                "/a2a/push-callback",
                content=body,
                headers={
                    "content-type": "application/json",
                    "X-A2A-Worker-Id": "Alice",
                    "X-A2A-Callback-Proof": fresh_proof,
                },
            )

    resp2 = asyncio.run(_run_with_new_nonce())
    assert resp2.status_code == 200  # auth passes, but scope fence blocks writes
    assert store.temporal_event_count(scope_id) == before


def test_admitted_runtime_auto_activates_expected_scope(secure_server):
    """Phase 2 Blocker: admission must auto-activate the canonical scope from
    trusted control state (project/run/context/epoch) — no test-side/manual
    activate_scope."""
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    scope_id = ingestor.scope_id_for("ctx-autoscope", 0)

    # Scope does not exist before admission.
    assert store.get_scope(scope_id) is None

    runtime = manager.admit("ctx-autoscope")
    assert runtime.context_id == "ctx-autoscope"

    # Admission auto-activated the expected scope.
    row = store.get_scope(scope_id)
    assert row is not None
    assert row["closed_at"] is None
    assert row["project_id"] == "llamar"
    assert row["experiment_id"] == "run-1"
    assert row["context_id"] == "ctx-autoscope"
    assert row["runtime_epoch"] == 0

    # The lock-external receipt seam is attached to the same runtime.
    assert runtime._receipt_sink is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_closed_scope_tuple_fails_closed_on_admission(secure_server):
    """A closed scope tuple must never be reopened: a second admission of the
    same (context, epoch) is rejected with the typed scope_tuple_reuse error and
    writes nothing."""
    from a2a.coordinator.memory.ingestor import MemoryScopeReuseError

    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    scope_id = ingestor.scope_id_for("ctx-reuse", 0)

    first = manager.admit("ctx-reuse")
    assert store.get_scope(scope_id) is not None
    await first.abort("test_complete")

    # System closes the scope (recovery/archive fence).
    assert ingestor.close_scope("ctx-reuse", 0) is True

    with pytest.raises(MemoryScopeReuseError) as exc:
        manager.admit("ctx-reuse")
    assert exc.value.code == "scope_tuple_reuse"

    # No reopen, no writes.
    assert store.get_scope(scope_id)["closed_at"] is not None
    assert store.temporal_event_count(scope_id) == 0


def test_valid_signed_callback_succeeds_without_manual_scope_activation(
    secure_server,
):
    """The full production path: admission auto-activates the scope, so a valid
    signed callback is canonically ingested with zero test-side activation."""
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    assert store.get_scope(scope_id) is not None, "admission must activate scope"

    event_store.clear()
    from httpx import ASGITransport, AsyncClient
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {"state": "TASK_STATE_WORKING"},
        }
    }
    body = json.dumps(payload).encode("utf-8")
    proof = CallbackProofV1.generate(SECRET, "Alice", hashlib.sha256(body).hexdigest())

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post(
                "/a2a/push-callback",
                content=body,
                headers={
                    "content-type": "application/json",
                    "X-A2A-Worker-Id": "Alice",
                    "X-A2A-Callback-Proof": proof,
                },
            )

    import asyncio

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    events = store.temporal_events(scope_id)
    callback_events = [e for e in events if e["event_type"] == "callback.status_update"]
    assert len(callback_events) == 1, "canonical callback event written"


# ---------------------------------------------------------------------------
# Phase 3: production producer path -> canonical projection ingest/reducer
# ---------------------------------------------------------------------------


def _signed_callback_with_observation(server, dispatch, status_text, payload):
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from a2a.coordinator.memory.callback_auth import CallbackProofV1

    body = json.dumps(payload).encode("utf-8")
    proof = CallbackProofV1.generate(SECRET, "Alice", hashlib.sha256(body).hexdigest())
    headers = {
        "content-type": "application/json",
        "X-A2A-Worker-Id": "Alice",
        "X-A2A-Callback-Proof": proof,
    }

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=server._app), base_url="http://test"
        ) as client:
            return await client.post("/a2a/push-callback", content=body, headers=headers)

    return asyncio.run(_run()), body


def test_secure_signed_callback_observation_flows_to_canonical_projection(
    secure_server,
):
    """Production wiring (Phase 3 HIGH finding): a valid signed callback
    carrying a structured Worker observation must invoke the canonical
    projection ingest/reducer — not only the Temporal callback write."""
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    from sar_orch.map import SemanticMapStore

    sem = SemanticMapStore()
    server._semantic_map = sem

    status_text = "[Result] report_observation: obs\n[DATA]\n" + json.dumps(
        {
            "ev": "tool_result",
            "tool_name": "report_observation",
            "success": True,
            "content": json.dumps(
                {
                    "reporter": "Alice",
                    "step": 3,
                    "object_type": "fire",
                    "name": "FireA",
                    "position": [2, 3, 0],
                    "attributes": {"status": "active", "intensity": "High"},
                    "confidence": 0.95,
                }
            ),
        }
    )
    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": status_text}]},
            },
        }
    }

    resp, _ = _signed_callback_with_observation(server, dispatch, status_text, payload)
    assert resp.status_code == 200

    # Canonical Memory: the callback wrote Temporal + the canonical projection.
    field = store.projection_field(scope_id, "spatial", "FireA", "position")
    assert field is not None, "worker evidence must materialize a projection field"
    assert field["value"] == [2, 3, 0]
    assert field["env_step"] == 3
    assert field["provenance"] == "worker_observation"
    assert field["evidence_id"].startswith("cb:worker-1:")

    intensity = store.projection_field(scope_id, "spatial", "FireA", "intensity")
    assert intensity is not None and intensity["value"] == "High"

    # Temporal evidence event carries authenticated dispatch/correlation and the
    # canonical event id joins to the projection field.
    evidence_events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"] == "evidence.projection"
    ]
    assert len(evidence_events) == 1
    ev = evidence_events[0]
    assert ev["event_id"] == field["event_id"]
    assert ev["dispatch_id"] == dispatch.dispatch_id
    assert ev["worker_task_id"] == "worker-1"
    assert ev["correlation_id"] == f"dispatch:{dispatch.dispatch_id}"
    assert ev["causation_id"].startswith("evidence:cb:worker-1:")
    assert store.entity_revision_of(scope_id, "spatial", "FireA") >= 1
    assert store.view_revision_of(scope_id) >= 1

    # Legacy compatibility writes preserved: the observation is still in the
    # semantic map and EventStore.
    obs = sem.get_recent_observations(limit=5)
    assert any(o.get("name") == "FireA" for o in obs)
    store_obs = event_store.get_recent_observations()
    assert any(str(o.get("name")) == "FireA" for o in store_obs)


def test_secure_signed_callback_masked_truth_observation_reaches_no_sink(
    secure_server,
):
    """A valid signed callback whose observation VALUE masks a ground-truth /
    oracle field is stripped at the producer: it reaches neither the canonical
    projection nor the legacy semantic map sink."""
    server, store, ingestor, tmp_path = secure_server
    manager = server.mission_runtime_manager
    runtime = manager.admit("ctx-secure")
    dispatch = runtime.create_dispatch("logical", "Alice")
    runtime.register_worker_task(dispatch.dispatch_id, "worker-1")
    runtime.apply_physical_status(
        dispatch.dispatch_id, "DISPATCHING", source="dispatch"
    )

    scope_id = ingestor.scope_id_for("ctx-secure", 0)
    event_store.clear()
    from sar_orch.map import SemanticMapStore

    sem = SemanticMapStore()
    server._semantic_map = sem

    status_text = "[Result] report_observation: obs\n[DATA]\n" + json.dumps(
        {
            "ev": "tool_result",
            "tool_name": "report_observation",
            "success": True,
            "content": json.dumps(
                {
                    "reporter": "Alice",
                    "step": 3,
                    "object_type": "fire",
                    "name": "MaskedFire",
                    "position": [9, 9, 0],
                    "note": "ground_truth says MaskedFire intensity=High",
                    "attributes": {"intensity": "High"},
                }
            ),
        }
    )
    payload = {
        "statusUpdate": {
            "taskId": "worker-1",
            "contextId": "ctx-secure",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": status_text}]},
            },
        }
    }

    resp, _ = _signed_callback_with_observation(server, dispatch, status_text, payload)
    assert resp.status_code == 200

    # No canonical projection for the masked object.
    assert store.projection_field(scope_id, "spatial", "MaskedFire", "position") is None
    evidence_events = [
        e
        for e in store.temporal_events(scope_id)
        if e["event_type"] == "evidence.projection"
    ]
    assert evidence_events == []
    # The legacy semantic map did not ingest the masked observation either.
    obs = sem.get_recent_observations(limit=10)
    assert all(o.get("name") != "MaskedFire" for o in obs)

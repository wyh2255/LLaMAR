"""Tests for Coordinator /a2a/push-callback — help_request 事件路由。"""

import pytest
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a.coordinator.event_store import event_store


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
                state_name = TaskState.Name(su.status.state) if su.status.state else "UNKNOWN"
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

"""a2a_server.py 单元测试 — 测试 HTTP 路由、SSE 格式和消息格式。"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from integration.sar_workers.a2a_server import A2AWorkerServer


@pytest.fixture
def server():
    react_agent = AsyncMock()
    react_agent.run = AsyncMock(return_value="task done")
    skill = MagicMock()
    skill.name = "firefighting"
    skill.description = "Extinguish fires"
    skill.tool_names = ["use_supply", "navigate_to"]
    # 删除 MagicMock 自动生成的 to_agent_skill_dict，使 hasattr 返回 False，
    # 这样 A2AWorkerServer 会走 fallback 路径生成 skill dict。
    del skill.to_agent_skill_dict
    srv = A2AWorkerServer(
        agent_name="Alice", port=8191,
        coordinator_host="localhost", coordinator_port=8080,
        react_agent=react_agent, skills=[skill],
        model="deepseek-v4-flash",
    )
    return srv


def test_agent_card(server):
    """AgentCard 端点返回正确格式，skills 包含 id/tags 字段（Coordinator 兼容）。"""
    app = server._create_app()
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Alice"
    assert data["version"] == "1.0"
    assert "a2a-jsonrpc" in data["interfaces"]
    # skills: 1 个用户 skill + 2 个特殊 skill (backend/model)
    assert len(data["skills"]) == 3
    # 用户 skill 有 id 和 tags
    user_skill = data["skills"][0]
    assert user_skill["name"] == "firefighting"
    assert "id" in user_skill
    assert "tags" in user_skill
    # backend/model 特殊 skill
    backend_skill = next(s for s in data["skills"] if s["id"] == "backend")
    assert "react_agent" in backend_skill["tags"]
    model_skill = next(s for s in data["skills"] if s["id"] == "model")
    assert "deepseek-v4-flash" in model_skill["tags"]


def test_jsonrpc_send_message_returns_sse(server):
    """JSON-RPC SendMessage 返回 SSE 流（不是普通 JSON）。"""
    app = server._create_app()
    client = TestClient(app)
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "task-001",
                "role": "ROLE_USER",
                "parts": [{"text": "Extinguish the fire"}],
            }
        },
        "id": "req-001",
    }
    resp = client.post("/api/v1/jsonrpc/", json=payload)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # 解析 SSE 事件
    events = []
    for line in resp.text.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))

    # 至少有 working + completed 两个事件
    assert len(events) >= 2
    assert events[0]["result"]["status"]["state"] == "working"
    assert events[-1]["result"]["status"]["state"] == "completed"
    assert "task done" in events[-1]["result"]["artifact"]["parts"][0]["text"]


def test_jsonrpc_unknown_method(server):
    """未知 method 返回 -32601 错误。"""
    app = server._create_app()
    client = TestClient(app)
    payload = {"jsonrpc": "2.0", "method": "UnknownMethod", "id": "req-002"}
    resp = client.post("/api/v1/jsonrpc/", json=payload)
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == -32601


def test_sse_stream_on_task_failure():
    """react_agent.run() 抛异常时 SSE 流返回 failed 状态。"""
    react_agent = AsyncMock()
    react_agent.run = AsyncMock(side_effect=RuntimeError("LLM timeout"))
    srv = A2AWorkerServer(
        agent_name="Bob", port=8192,
        coordinator_host="localhost", coordinator_port=8080,
        react_agent=react_agent, skills=[],
    )
    app = srv._create_app()
    client = TestClient(app)
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {"message": {"messageId": "task-err", "parts": [{"text": "fail task"}]}},
        "id": "req-err",
    }
    resp = client.post("/api/v1/jsonrpc/", json=payload)
    assert resp.status_code == 200
    events = [json.loads(line[6:]) for line in resp.text.split("\n") if line.startswith("data: ")]
    assert len(events) >= 2
    assert events[0]["result"]["status"]["state"] == "working"
    assert events[-1]["result"]["status"]["state"] == "failed"
    assert "LLM timeout" in events[-1]["result"]["artifact"]["parts"][0]["text"]

"""SAR worker startup fail-fast on missing Map Agent MCP tools (t_7c303cb1).

Before this change a worker whose Map Agent MCP handshake failed started
anyway without those tools (``_assemble_tools_async`` swallowed the error into
a warning) or — worse, on the cancel-scope ``CancelledError`` path — its run
thread died silently.  The worker now refuses to start, records the failure on
``SARWorker.thread_error``, and the experiment's liveness gate aborts the run.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager

import pytest

from Agent.worker_agent.tools import mcp_loader
from sar_orch.worker import SARWorker, WorkerStartupError

pytestmark = pytest.mark.unit


class _FakeBarrier:
    """Minimal barrier stub: tool assembly only reads ``_step_counter``."""

    _step_counter = 0


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(mcp_loader._default_timeout_config, "connect_attempts", 2)
    monkeypatch.setattr(mcp_loader._default_timeout_config, "retry_backoff", 0.0)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _StubServer:
    def __init__(self, ready: threading.Event, holder: dict):
        self.ready = ready
        self._holder = holder

    def stop(self) -> None:
        server = self._holder.get("server")
        if server is not None:
            server.should_exit = True


def _serve_stub_map_mcp(port: int) -> _StubServer:
    """Serve a fresh FastMCP app at ``/mcp/map`` (one tool, no LLM needed)."""
    ready = threading.Event()
    holder: dict = {}

    def _run() -> None:
        import uvicorn
        from fastapi import FastAPI
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("map_agent_stub", streamable_http_path="/")

        @mcp.tool(name="map_agent__stub_query")
        def stub_query(text: str = "") -> dict:
            """Echo tool proving the loader/worker wiring."""
            return {"answer": text}

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            async with mcp.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.mount("/mcp/map", mcp.streamable_http_app())
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        holder["server"] = server
        ready.set()
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - thread diagnostics only
            print(f"stub MCP server on port {port} failed: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return _StubServer(ready, holder)


def _worker(tmp_path, coordinator_port: int) -> SARWorker:
    return SARWorker(
        worker_id="worker-1",
        agent_name="Alice",
        agent_idx=0,
        barrier=_FakeBarrier(),
        coordinator_url=f"ws://localhost:{coordinator_port}",
        log_dir=str(tmp_path / "logs"),
        coordinator_secret=bytes(range(32)),
    )


async def test_assemble_tools_loads_map_tools_from_live_endpoint(
    tmp_path, fast_retries
):
    port = _free_port()
    server = _serve_stub_map_mcp(port)
    assert server.ready.wait(timeout=10.0)
    # The stub binds on start; give uvicorn a moment to accept connections.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        probe = socket.socket()
        try:
            probe.connect(("127.0.0.1", port))
            probe.close()
            break
        except OSError:
            probe.close()
            await asyncio.sleep(0.05)

    worker = _worker(tmp_path, port)
    try:
        tools = await worker._assemble_tools_async(
            http_url=f"http://127.0.0.1:{port}", mailbox_store=None
        )
        names = [tool.name for tool in tools]
        assert "map_agent__stub_query" in names
        assert len(worker._mcp_registry) == 1
    finally:
        await mcp_loader.cleanup_mcp_connections(worker._mcp_registry)
        server.stop()


async def test_assemble_tools_fails_fast_when_map_mcp_unreachable(
    tmp_path, fast_retries
):
    dead_port = _free_port()
    worker = _worker(tmp_path, dead_port)

    with pytest.raises(WorkerStartupError) as excinfo:
        await worker._assemble_tools_async(
            http_url=f"http://127.0.0.1:{dead_port}", mailbox_store=None
        )

    message = str(excinfo.value)
    assert "Alice" in message
    assert f"http://127.0.0.1:{dead_port}/mcp/map" in message
    assert "unreachable" in message
    assert worker._mcp_registry == []  # nothing half-connected


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_thread_records_startup_error_instead_of_silent_death(
    tmp_path, fast_retries
):
    """End-to-end: the run thread exits *loudly*, with the cause recorded."""
    dead_port = _free_port()
    worker = _worker(tmp_path, dead_port)

    assert worker.is_alive() is False  # not started yet
    assert worker.thread_error is None

    worker.start()

    deadline = time.time() + 30.0
    while time.time() < deadline and worker.is_alive():
        time.sleep(0.05)

    assert worker.is_alive() is False, "startup failure must end the run thread"
    assert isinstance(worker.thread_error, WorkerStartupError)
    # start() derives the HTTP endpoint from the ws:// coordinator URL.
    assert f"http://localhost:{dead_port}/mcp/map" in str(worker.thread_error)
    # Idempotent teardown: the experiment stops every worker in its finally.
    worker.stop()

"""Worker-startup MCP connect resilience (t_7c303cb1).

Regression coverage for the 2026-09-17 scene3 incident: a worker starting
before the coordinator's ASGI app was serving got the MCP client's
cancel-scope ``CancelledError`` out of ``MCPServerConnection.connect()``
(``except Exception`` never catches a BaseException), the worker thread died
silently and the run burned its whole step budget with an empty team.

The contract these tests pin down:

* handshake failures (refused connection / timeout / aborted handshake) are
  retried within a bounded budget and reported as ``False`` + ``last_error``;
* a cancellation produced by the MCP client's own anyio task group is a
  failed attempt like any other — never an escape;
* a genuine cancellation of the calling task still propagates untouched;
* ``strict`` loaders surface ``MCPConnectError`` instead of silence;
* a real endpoint that becomes reachable late is recovered by the retries.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from builtins import BaseExceptionGroup, ExceptionGroup
from contextlib import asynccontextmanager

import pytest

from Agent.worker_agent.tools import mcp_loader
from Agent.worker_agent.tools.mcp_loader import (
    MCPConnectError,
    MCPServerConnection,
    _is_scope_cancellation,
    _must_propagate,
    load_mcp_tools_async,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def fast_retries(monkeypatch):
    """Keep the retry budget tiny so no test ever sleeps the production backoff."""
    monkeypatch.setattr(mcp_loader._default_timeout_config, "connect_attempts", 2)
    monkeypatch.setattr(mcp_loader._default_timeout_config, "retry_backoff", 0.0)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_blackhole(port: int) -> socket.socket:
    """Bind + listen, accept connections, never answer (ASGI-not-serving race)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def _loop() -> None:
        keep_open = []
        while True:
            try:
                conn, _ = srv.accept()
                keep_open.append(conn)
            except OSError:
                break

    threading.Thread(target=_loop, daemon=True).start()
    return srv


def _connection(url: str, **kwargs) -> MCPServerConnection:
    return MCPServerConnection(
        name="map_agent", connection_type="streamable_http", url=url, **kwargs
    )


def _scripted_connect_once(monkeypatch, connection, outcomes):
    """Replace one connection's handshake with scripted outcomes.

    ``outcomes`` is consumed per call; the last entry repeats.  ``None`` means
    "the attempt succeeded".
    """
    calls: list[int] = []

    async def _fake_once() -> None:
        calls.append(len(calls) + 1)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(connection, "_connect_once", _fake_once)
    return calls


class _ServerHandle:
    def __init__(self, thread: threading.Thread, ready: threading.Event, holder: dict):
        self.thread = thread
        self.ready = ready
        self._holder = holder

    def stop(self) -> None:
        server = self._holder.get("server")
        if server is not None:
            server.should_exit = True
        # Leave the shared module-level FastMCP clean for later tests in the
        # same process: its session manager can only be run() once, so a
        # started instance must not leak into tests that mount it themselves
        # (e.g. tests/test_mcp_injection_points.py asserts _has_started).
        from sar_orch.map_agent import server as map_agent_server

        map_agent_server.mcp._session_manager = None


def _serve_map_mcp_http(port: int, *, delay: float = 0.0) -> _ServerHandle:
    """Serve the real Map Agent MCP mount (same wiring as SARCoordinator).

    The module-level FastMCP instance starts its session manager only once per
    process, so it is reset first to keep this helper reusable for reruns
    (``mcp`` is pinned by uv.lock).
    """
    ready = threading.Event()
    holder: dict = {}

    def _run() -> None:
        time.sleep(delay)
        import uvicorn
        from fastapi import FastAPI

        from sar_orch.map import SemanticMapStore
        from sar_orch.map_agent import server as map_agent_server

        map_agent_server.mcp._session_manager = None  # fresh manager per start

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            # Mirrors SARCoordinator: the parent app owns the MCP session
            # manager lifecycle (sar_orch/coordinator.py).
            async with map_agent_server.mcp.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        map_agent_server.mount_to_fastapi(app, SemanticMapStore())
        ready.set()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        holder["server"] = server
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - thread diagnostics only
            print(f"MCP test server on port {port} failed: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return _ServerHandle(thread, ready, holder)


# --------------------------------------------------------------------------
# cancellation classification (pure helpers)
# --------------------------------------------------------------------------


def test_scope_cancellation_detection_matches_anyio_convention():
    assert _is_scope_cancellation(asyncio.CancelledError("Cancelled via cancel scope 1a2b"))
    assert not _is_scope_cancellation(asyncio.CancelledError())
    assert not _is_scope_cancellation(asyncio.CancelledError("operator shutdown"))

    inner = asyncio.CancelledError("Cancelled via cancel scope 1a2b")
    outer = asyncio.CancelledError("shutdown during connect")
    outer.__context__ = inner
    assert _is_scope_cancellation(outer)


def test_must_propagate_classification():
    assert _must_propagate(asyncio.CancelledError("operator shutdown"))
    assert not _must_propagate(asyncio.CancelledError("Cancelled via cancel scope 1a2b"))
    assert not _must_propagate(TimeoutError())
    assert not _must_propagate(ConnectionError("refused"))
    assert _must_propagate(KeyboardInterrupt())
    assert _must_propagate(SystemExit(1))
    # groups: any member that must propagate decides for the whole group
    assert not _must_propagate(ExceptionGroup("g", [ConnectionError("refused")]))
    assert _must_propagate(
        BaseExceptionGroup("g", [asyncio.CancelledError("operator shutdown")])
    )
    assert not _must_propagate(
        BaseExceptionGroup("g", [asyncio.CancelledError("Cancelled via cancel scope 1a2b")])
    )


# --------------------------------------------------------------------------
# bounded retries + failure reporting
# --------------------------------------------------------------------------


async def test_connect_retries_transient_failure_then_succeeds(monkeypatch, fast_retries):
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    calls = _scripted_connect_once(
        monkeypatch, conn, [ConnectionError("refused"), None]
    )

    assert await conn.connect() is True
    assert len(calls) == 2
    assert conn.last_error is None
    assert conn.last_attempts == 2


async def test_connect_gives_up_after_bounded_attempts(monkeypatch, fast_retries):
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    calls = _scripted_connect_once(monkeypatch, conn, [TimeoutError()])

    assert await conn.connect() is False
    assert len(calls) == 2  # default connect_attempts pinned to 2 by the fixture
    assert isinstance(conn.last_error, TimeoutError)


async def test_scope_cancellation_from_transport_is_a_failed_attempt(
    monkeypatch, fast_retries
):
    """The incident's exception shape: anyio's cancel scope aborts the handshake."""
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    calls = _scripted_connect_once(
        monkeypatch,
        conn,
        [asyncio.CancelledError("Cancelled via cancel scope 4f2a")],
    )

    assert await conn.connect() is False
    assert len(calls) == 2
    assert isinstance(conn.last_error, asyncio.CancelledError)


async def test_scope_cancellation_wrapped_by_transport_is_a_failed_attempt(
    monkeypatch, fast_retries
):
    inner = asyncio.CancelledError("Cancelled via cancel scope 4f2a")
    wrapped = RuntimeError("session init aborted")
    wrapped.__context__ = inner
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    _scripted_connect_once(monkeypatch, conn, [wrapped])

    assert await conn.connect() is False


async def test_external_cancellation_propagates(monkeypatch, fast_retries):
    """A real ``task.cancel()`` must never be swallowed and retried."""
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    started = asyncio.Event()

    async def _hang() -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(conn, "_connect_once", _hang)
    task = asyncio.create_task(conn.connect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_external_cancellation_during_real_handshake_propagates(fast_retries):
    """Same, but through the real streamable-http transport against a black hole."""
    port = _free_port()
    srv = _start_blackhole(port)
    try:
        conn = _connection(
            f"http://127.0.0.1:{port}/mcp/map",
            connect_timeout=30.0,
            connect_attempts=3,
            retry_backoff=0.0,
        )
        task = asyncio.create_task(conn.connect())
        await asyncio.sleep(0.4)  # let the handshake reach the suspended wait
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        srv.close()


async def test_base_exception_group_with_external_cancel_propagates(monkeypatch, fast_retries):
    conn = _connection(f"http://127.0.0.1:{_free_port()}/mcp/map")
    _scripted_connect_once(
        monkeypatch,
        conn,
        [
            BaseExceptionGroup(
                "task group aborted",
                [ConnectionError("refused"), asyncio.CancelledError("operator shutdown")],
            )
        ],
    )

    with pytest.raises(BaseExceptionGroup):
        await conn.connect()


# --------------------------------------------------------------------------
# real transports
# --------------------------------------------------------------------------


async def test_refused_port_returns_false_without_cancelled_escape(fast_retries):
    """The 2026-09-17 incident, hermetically: connection refused.

    Pre-fix this raised ``CancelledError('Cancelled via cancel scope ...')``
    out of ``connect()`` and killed the worker thread.
    """
    dead_port = _free_port()
    conn = _connection(
        f"http://127.0.0.1:{dead_port}/mcp/map",
        connect_timeout=2.0,
        connect_attempts=3,
        retry_backoff=0.0,
    )

    assert await conn.connect() is False
    assert conn.last_error is not None
    assert conn.last_attempts == 3
    # Teardown of the failed transport must not raise either.
    await conn.disconnect()


async def test_timeout_hits_bounded_budget(fast_retries):
    port = _free_port()
    srv = _start_blackhole(port)
    try:
        conn = _connection(
            f"http://127.0.0.1:{port}/mcp/map",
            connect_timeout=0.5,
            connect_attempts=2,
            retry_backoff=0.0,
        )
        assert await conn.connect() is False
        assert isinstance(conn.last_error, TimeoutError)
    finally:
        srv.close()


async def test_real_endpoint_appearing_late_is_recovered(fast_retries):
    """Retry recovers the actual startup race: workers may beat the coordinator."""
    port = _free_port()
    handle = _serve_map_mcp_http(port, delay=1.0)
    assert handle.ready.wait(timeout=10.0)  # app assembled before it starts serving

    conn = _connection(
        f"http://127.0.0.1:{port}/mcp/map",
        connect_timeout=2.0,
        connect_attempts=8,
        retry_backoff=0.4,
    )
    try:
        assert await conn.connect() is True
        assert conn.last_error is None
        tool_names = sorted(tool.name for tool in conn.tools)
        assert tool_names == [
            "map_agent__get_fire_info",
            "map_agent__get_person_info",
            "map_agent__get_reservoir_info",
            "map_agent__get_task_context",
            "map_agent__query_natural",
        ]
    finally:
        await conn.disconnect()
        handle.stop()


# --------------------------------------------------------------------------
# loader-level policy
# --------------------------------------------------------------------------


def _write_config(tmp_path, url: str) -> str:
    import json

    path = tmp_path / "mcp_test.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "map_agent": {
                        "url": url,
                        "type": "streamable_http",
                        "connect_timeout": 2.0,
                        "connect_attempts": 2,
                        "retry_backoff": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return str(path)


async def test_strict_loader_raises_typed_error(tmp_path):
    registry: list = []
    config_path = _write_config(tmp_path, f"http://127.0.0.1:{_free_port()}/mcp/map")

    with pytest.raises(MCPConnectError) as excinfo:
        await load_mcp_tools_async(
            config_path, connection_registry=registry, strict=True
        )

    error = excinfo.value
    assert error.server_name == "map_agent"
    assert "/mcp/map" in error.target
    assert error.attempts == 2
    assert error.last_error is not None
    assert registry == []  # a failed connection is never registered


async def test_lenient_loader_keeps_historical_skip_behaviour(tmp_path):
    registry: list = []
    config_path = _write_config(tmp_path, f"http://127.0.0.1:{_free_port()}/mcp/map")

    tools = await load_mcp_tools_async(config_path, connection_registry=registry)

    assert tools == []
    assert registry == []

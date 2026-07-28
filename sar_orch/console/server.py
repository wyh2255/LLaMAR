"""SAR Console server — one-click experiment launcher + live monitoring backend.

A standalone FastAPI service (default :9000) that:

1. Spawns/stops ``sar_orch/experiment.py`` as a subprocess (same pattern as
   ``sar_orch/benchmark.py``), which brings up the coordinator, the SAR
   simulation, and all workers.
2. Serves live NDJSON log streams (coordinator router trace, per-worker
   traces, events) via SSE, tailing files inside the run's log directory.
3. Forwards mid-run user commands to the coordinator's
   ``POST /api/user-command`` endpoint.
4. Reverse-proxies GET requests to the coordinator server (``/coord/*``) so
   the console page stays same-origin (mission graph, map, dashboard).

Run:
    PYTHONPATH="src:$PYTHONPATH" uv run python -m sar_orch.console.server --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)
from pydantic import BaseModel

logger = logging.getLogger("sar_console")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_ROOT = _PROJECT_ROOT / "sar_orch" / "results"
_INDEX_HTML = Path(__file__).resolve().parent / "index.html"

# Well-known SSE paths on the coordinator that must be proxied as streams.
_SSE_PROXY_PATHS = ("map/state", "dashboard/stream")


class RunStartRequest(BaseModel):
    scene: int = 1
    agents: int = 2
    seed: int = 42
    model: str = "deepseek-v4-flash"
    mode: str = "semantic"
    max_steps: int = 50
    task: str = ""


class CommandRequest(BaseModel):
    text: str


class RunState:
    """Holds the single active experiment run."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.params: dict = {}
        self.log_dir: Path | None = None
        self.started_at: float = 0.0
        self.log_file = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def coordinator_url(self) -> str:
        return f"http://localhost:{self.params.get('coordinator_port', 8080)}"


_STATE = RunState()


def _build_sub_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}:{_PROJECT_ROOT}/src:{env.get('PYTHONPATH', '')}"
    # Keep proxy vars: the LLM API gateway may only be reachable through the
    # proxy, while localhost traffic must bypass it (worker/coordinator/A2A).
    for k in ("no_proxy", "NO_PROXY"):
        existing = env.get(k, "")
        localhost_set = "localhost,0.0.0.0,127.0.0.1,::1"
        env[k] = f"{localhost_set},{existing}" if existing else localhost_set
    return env


def _read_log_tail(path: Path, max_lines: int = 50) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def create_app() -> FastAPI:
    app = FastAPI(title="SAR Console")

    # ── Run control ──────────────────────────────────────────────────────

    @app.post("/api/run/start")
    async def run_start(req: RunStartRequest):
        if _STATE.running:
            raise HTTPException(status_code=409, detail="a run is already active")
        if not 1 <= req.scene <= 5:
            raise HTTPException(status_code=400, detail="scene must be 1-5")
        if not 1 <= req.agents <= 6:
            raise HTTPException(status_code=400, detail="agents must be 1-6")

        ts = time.strftime("%Y%m%d_%H%M%S")
        log_dir = (
            _RESULTS_ROOT / f"console_{ts}_s{req.scene}_seed{req.seed}_a{req.agents}"
        )
        log_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "sar_orch/experiment.py",
            "--scene",
            str(req.scene),
            "--agents",
            str(req.agents),
            "--seed",
            str(req.seed),
            "--model",
            str(req.model),
            "--log-dir",
            str(log_dir),
        ]
        if req.max_steps > 0:
            cmd.extend(["--max-steps", str(req.max_steps)])
        if req.mode != "semantic":
            cmd.extend(["--mode", req.mode])
        if req.task.strip():
            cmd.extend(["--coordinator-prompt", req.task.strip()])

        log_path = log_dir / "console_subprocess.log"
        log_file = open(log_path, "w", buffering=1)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(_PROJECT_ROOT),
            env=_build_sub_env(),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        _STATE.proc = proc
        _STATE.params = req.model_dump()
        _STATE.log_dir = log_dir
        _STATE.started_at = time.time()
        _STATE.log_file = log_file
        logger.info("Started run pid=%d log_dir=%s", proc.pid, log_dir)
        return {
            "started": True,
            "pid": proc.pid,
            "log_dir": str(log_dir.relative_to(_PROJECT_ROOT)),
        }

    @app.post("/api/run/stop")
    async def run_stop():
        proc = _STATE.proc
        if proc is None or proc.returncode is not None:
            return {"stopped": False, "detail": "no active run"}
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if _STATE.log_file is not None:
            _STATE.log_file.close()
            _STATE.log_file = None
        return {"stopped": True, "exit_code": proc.returncode}

    @app.get("/api/run/status")
    async def run_status():
        proc = _STATE.proc
        log_tail: list[str] = []
        if _STATE.log_dir is not None:
            log_tail = _read_log_tail(_STATE.log_dir / "console_subprocess.log")
        return {
            "running": _STATE.running,
            "pid": proc.pid if proc is not None else None,
            "exit_code": proc.returncode if proc is not None else None,
            "params": _STATE.params,
            "log_dir": (
                str(_STATE.log_dir.relative_to(_PROJECT_ROOT))
                if _STATE.log_dir is not None
                else None
            ),
            "started_at": _STATE.started_at or None,
            "uptime_s": (
                round(time.time() - _STATE.started_at, 1)
                if _STATE.running
                else None
            ),
            "log_tail": log_tail,
        }

    # ── NDJSON log discovery + streaming ─────────────────────────────────

    def _require_log_dir() -> Path:
        if _STATE.log_dir is None:
            raise HTTPException(status_code=404, detail="no run yet")
        return _STATE.log_dir

    def _resolve_log_path(rel: str) -> Path:
        log_dir = _require_log_dir()
        path = (log_dir / rel).resolve()
        if not path.is_relative_to(log_dir.resolve()):
            raise HTTPException(status_code=400, detail="path outside log dir")
        if path.suffix != ".ndjson":
            raise HTTPException(status_code=400, detail="only .ndjson supported")
        return path

    def _source_for(path: Path, log_dir: Path) -> str:
        rel = path.relative_to(log_dir)
        if path.name == "events.ndjson":
            return "events"
        if len(rel.parts) > 1:
            return rel.parts[0]  # subdirectory, e.g. "coordinator" / "workers"
        return "coordinator"

    @app.get("/api/logs")
    async def list_logs():
        log_dir = _require_log_dir()
        streams = []
        for path in sorted(log_dir.rglob("*.ndjson")):
            rel = str(path.relative_to(log_dir))
            streams.append(
                {
                    "path": rel,
                    "source": _source_for(path, log_dir),
                    "size": path.stat().st_size,
                    "mtime": path.stat().st_mtime,
                }
            )
        return {"log_dir": str(log_dir), "streams": streams}

    @app.get("/api/logs/stream")
    async def stream_log(path: str, request: Request):
        log_path = _resolve_log_path(path)

        async def event_generator():
            last_size = 0
            # Replay existing content (file may not exist yet — wait for it).
            if log_path.exists():
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            yield f"data: {line}\n\n"
                    last_size = f.tell()
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.3)
                if not log_path.exists():
                    continue
                current_size = log_path.stat().st_size
                if current_size < last_size:
                    last_size = 0  # file recreated
                if current_size > last_size:
                    with open(log_path) as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                    last_size = current_size

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/logs/stream-all")
    async def stream_all_logs(request: Request):
        """Single multiplexed SSE stream for every ``*.ndjson`` in the log dir.

        The page must use this instead of one EventSource per file: browsers
        cap HTTP/1.1 connections per origin at ~6, and a run produces ~20 log
        files — per-file streams starved every other console request (status,
        mission graph, stop) behind permanently-held SSE connections.

        Each event is ``{"path", "source", "event"}`` where ``event`` is the
        parsed NDJSON line. Replays existing content on connect, then follows.
        """
        log_dir = _require_log_dir()

        async def event_generator():
            offsets: dict[Path, int] = {}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    files = sorted(
                        log_dir.rglob("*.ndjson"),
                        key=lambda p: p.stat().st_mtime,
                    )
                except OSError:
                    files = []
                for path in files:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    pos = offsets.get(path, 0)
                    if size < pos:
                        pos = 0  # file recreated/truncated
                    if size <= pos:
                        continue
                    with open(path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    # Only consume up to the last newline so a partially
                    # written final line is re-read on the next scan.
                    last_nl = chunk.rfind(b"\n")
                    if last_nl < 0:
                        continue
                    offsets[path] = pos + last_nl + 1
                    rel = str(path.relative_to(log_dir))
                    source = _source_for(path, log_dir)
                    for raw in chunk[:last_nl].splitlines():
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            json.loads(line)
                        except json.JSONDecodeError:
                            continue  # not a complete JSON record
                        yield (
                            'data: {"path": ' + json.dumps(rel)
                            + ', "source": ' + json.dumps(source)
                            + ', "event": ' + line + "}\n\n"
                        )
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── User command forwarding ──────────────────────────────────────────

    @app.post("/api/command")
    async def forward_command(req: CommandRequest):
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="text required")
        url = f"{_STATE.coordinator_url}/api/user-command"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json={"text": req.text.strip()})
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"coordinator unreachable: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()

    # ── Coordinator reverse proxy (GET only, same-origin for the page) ───

    @app.get("/coord/{path:path}")
    async def coord_proxy(path: str, request: Request):
        if not _STATE.running:
            raise HTTPException(status_code=502, detail="no active run")
        url = f"{_STATE.coordinator_url}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        if path in _SSE_PROXY_PATHS:
            client = httpx.AsyncClient(timeout=None)
            upstream = client.build_request("GET", url)

            async def sse_stream():
                try:
                    resp = await client.send(upstream, stream=True)
                    async for chunk in resp.aiter_raw():
                        yield chunk
                finally:
                    await client.aclose()

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"coordinator unreachable: {exc}"
            ) from exc
        return JSONResponse(
            content=resp.json() if resp.content else None,
            status_code=resp.status_code,
        )

    # ── Console page ─────────────────────────────────────────────────────

    @app.get("/")
    async def index():
        return FileResponse(_INDEX_HTML)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="SAR Console server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

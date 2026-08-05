"""Offline SAR results browser.

Serves the results overview and per-run detail pages, parsing run directories
on demand (deterministic, no LLM).

Usage:
    python sar_orch/results_server.py [--port 8090] [--results-dir sar_orch/results]

Then open http://localhost:8090/results
"""

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from export_run_detail import export_run  # noqa: E402

UI = ROOT / "ui"
_DIR_RE = re.compile(r"_s(\d+)_s(\d+)_a(\d+)")


def _sum_tokens(run_dir: Path) -> int:
    summary = run_dir / "summary.csv"
    if not summary.exists():
        return 0
    import csv

    try:
        with summary.open(encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return 0
        return sum(
            int(v) for k, v in rows[0].items() if k.endswith("TotalTokens") and v and v.isdigit()
        )
    except Exception:
        return 0


def build_index(results_dir: Path) -> dict:
    runs = []
    if results_dir.exists():
        for d in sorted(results_dir.iterdir()):
            if not d.is_dir() or not d.name[:1].isdigit():
                continue
            meta, metrics = {}, {}
            try:
                meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            try:
                metrics = json.loads((d / "run_metrics.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            m = _DIR_RE.search(d.name)
            runs.append({
                "dir": d.name,
                "scene": meta.get("scene") or (int(m.group(1)) if m else None),
                "seed": meta.get("seed") or (int(m.group(2)) if m else None),
                "agents": meta.get("agent_count") or (int(m.group(3)) if m else None),
                "model": meta.get("model", "?"),
                "state_mode": meta.get("state_mode", ""),
                "steps": metrics.get("steps"),
                "coverage": metrics.get("coverage"),
                "transport": metrics.get("transport_rate"),
                "end": metrics.get("end_reason", "?"),
                "finished": metrics.get("finished", False),
                "elapsed": metrics.get("elapsed_seconds"),
                "total_tokens": _sum_tokens(d),
            })
    return {"runs": runs}


class Handler(BaseHTTPRequestHandler):
    results_dir = RESULTS = ROOT / "results"
    _index_cache = {"mtime": 0.0, "data": None}
    _run_cache = {}

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _valid_run(self, name: str) -> Path | None:
        name = unquote(name)
        if "/" in name or ".." in name:
            return None
        p = self.results_dir / name
        return p if p.is_dir() else None

    def _get_index(self):
        try:
            mt = max((p.stat().st_mtime for p in self.results_dir.iterdir()), default=0)
        except FileNotFoundError:
            mt = 0
        if self._index_cache["data"] is None or mt > self._index_cache["mtime"]:
            self._index_cache = {"mtime": mt, "data": build_index(self.results_dir)}
        return self._index_cache["data"]

    def _get_run(self, name: str):
        p = self._valid_run(name)
        if p is None:
            return None
        mt = max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=0)
        cached = self._run_cache.get(name)
        if cached and cached[0] >= mt:
            return cached[1]
        data = export_run(p)
        self._run_cache[name] = (mt, data)
        return data

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/results")
            self.end_headers()
        elif path == "/results":
            self._send((UI / "results.html").read_bytes())
        elif path.startswith("/run/"):
            if self._valid_run(path[5:]) is None:
                self._send(b"run not found", "text/plain; charset=utf-8", 404)
            else:
                self._send((UI / "run_detail.html").read_bytes())
        elif path == "/api/index":
            self._json(self._get_index())
        elif path.startswith("/api/run/"):
            data = self._get_run(path[9:])
            if data is None:
                self._json({"error": "run not found"}, 404)
            else:
                self._json(data)
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--results-dir", default=str(ROOT / "results"))
    args = ap.parse_args()
    Handler.results_dir = Path(args.results_dir).resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SAR results browser: http://{args.host}:{args.port}/results")
    print(f"scanning: {Handler.results_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()

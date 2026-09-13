"""Phase 2 producer matrix — source-level inventory assertion.

Every one of the 14 active callback / EventStore / supervision write points must
declare (next to the code that performs the write) a ``# memory-producer:``
marker stating the canonical event source, the idempotency key and the auth
mode.  Any new writer must add a marker with the same three fields.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MARKER_RE = re.compile(
    r"#\s*memory-producer:\s*([\w-]+);\s*"
    r"canonical_source=([\w.-]+);\s*"
    r"idempotency=([\w.@-]+);\s*"
    r"auth=([\w-]+)"
)

# name -> (relative path, canonical_source, idempotency, auth)
EXPECTED = [
    {
        "file": "src/a2a/builtin_tools/dispatch_task.py",
        "name": "task_created_eventstore",
        "canonical_source": "mission_runtime.dispatch_receipt",
        "idempotency": "control.journal_sha256",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/builtin_tools/dispatch_task.py",
        "name": "dispatch_failure_eventstore",
        "canonical_source": "mission_runtime.failed_receipt",
        "idempotency": "control.journal_sha256",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "observation_report_ingest",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_artifact_branch",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_status_ok",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_task_legacy",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_artifact_legacy",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_status_legacy",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/server.py",
        "name": "callback_input_required",
        "canonical_source": "memory_ingestor.callback_envelope",
        "idempotency": "callback.idempotency_key",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/task_watchdog.py",
        "name": "supervision_worker_unreachable",
        "canonical_source": "supervision_event_adapter",
        "idempotency": "supervision.event_id",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/task_watchdog.py",
        "name": "supervision_task_stale",
        "canonical_source": "supervision_event_adapter",
        "idempotency": "supervision.event_id",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/task_watchdog.py",
        "name": "supervision_deadline_exceeded",
        "canonical_source": "supervision_event_adapter",
        "idempotency": "supervision.event_id",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/task_watchdog.py",
        "name": "supervision_deadline_warning",
        "canonical_source": "supervision_event_adapter",
        "idempotency": "supervision.event_id",
        "auth": "shadow",
    },
    {
        "file": "src/a2a/coordinator/task_watchdog.py",
        "name": "supervision_task_recovered",
        "canonical_source": "supervision_event_adapter",
        "idempotency": "supervision.event_id",
        "auth": "shadow",
    },
]


def _parse_markers(source: str) -> dict[str, dict]:
    markers: dict[str, dict] = {}
    for match in MARKER_RE.finditer(source):
        name, canonical_source, idempotency, auth = match.groups()
        markers[name] = {
            "canonical_source": canonical_source,
            "idempotency": idempotency,
            "auth": auth,
        }
    return markers


def test_all_fourteen_producer_points_declare_canonical_source_and_auth():
    found = 0
    for expected in EXPECTED:
        source = (ROOT / expected["file"]).read_text(encoding="utf-8")
        markers = _parse_markers(source)
        name = expected["name"]
        assert name in markers, (
            f"producer '{name}' missing memory-producer marker in {expected['file']}"
        )
        marker = markers[name]
        assert marker["canonical_source"] == expected["canonical_source"], (
            f"{name}: canonical_source mismatch {marker['canonical_source']}"
        )
        assert marker["idempotency"] == expected["idempotency"], (
            f"{name}: idempotency mismatch {marker['idempotency']}"
        )
        assert marker["auth"] == expected["auth"], (
            f"{name}: auth mode mismatch {marker['auth']}"
        )
        found += 1
    assert found == 14


def test_total_marker_count_is_fourteen():
    total = 0
    for file in {e["file"] for e in EXPECTED}:
        source = (ROOT / file).read_text(encoding="utf-8")
        total += len(_parse_markers(source))
    assert total == 14


def test_every_marker_has_all_three_required_fields():
    """Any new writer must declare canonical_source, idempotency AND auth."""
    for file in {e["file"] for e in EXPECTED}:
        source = (ROOT / file).read_text(encoding="utf-8")
        for match in MARKER_RE.finditer(source):
            name, canonical_source, idempotency, auth = match.groups()
            assert canonical_source, f"{name} missing canonical_source"
            assert idempotency, f"{name} missing idempotency"
            assert auth in {"legacy", "shadow", "read_port"}, (
                f"{name} has invalid auth mode {auth}"
            )

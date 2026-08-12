"""P4→P5 gate: real-model function-call reflection smoke (main plan §5.3).

Runs the **final production adapter** (``run_reflection`` full chain:
claim → ``ReflectionModelPort.complete_with_function_call`` real LLM →
deterministic validator → publish) 5 times consecutively on temporary
absolute long-term roots + fixture source snapshots.  Hard gate: 5/5 runs
must be ``completed`` with every published memory's source refs inside the
input window (redaction/truth scan is enforced by the production validator
inside the chain).

Additionally runs a deterministic fail-closed sanity pass (no model call):
responses with a missing field / forged ref / forbidden truth term must be
rejected with zero long-term entries through the same production chain.

Evidence JSON: ``sar_orch/results/long_term_smoke_<ts>.json``.  The api key
is never printed or recorded (always ``"<set>"``); provider/model come from
``.env`` via ``build_reflection_model_port`` (``reflection_*`` keys with
fallback to generic keys).

Usage (foreground, proxy bypass per AGENTS.md):
    env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
        uv run python sar_orch/scripts/long_term_reflection_smoke.py

Exit code 0 only when the 5/5 gate AND the 3/3 fail-closed sanity pass.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from a2a.coordinator.memory.contracts import POLICY_VERSION
from a2a.coordinator.memory.long_term import LongTermMemoryStore
from a2a.coordinator.memory.reflection import (
    run_reflection,
)
from a2a.coordinator.memory.store import ScopeEventSnapshotV1
from a2a.shared.env_loader import load_env_file
from sar_orch.long_term_reflection import build_reflection_model_port

_PROJECT_ID = "llamar"
_RESULTS_DIR = _REPO_ROOT / "sar_orch" / "results"

# ── fixture source snapshots (P0-test-homomorphic; no truth terms) ─────────

_FIXTURE_EVENTS: dict[str, list[dict[str, Any]]] = {
    "variant_a": [
        {
            "event_id": "a1",
            "scope_id": "smoke-a",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "contain warehouse fire at sector 7", "unit": "alpha"},
        },
        {
            "event_id": "a2",
            "scope_id": "smoke-a",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "water pressure low at hydrant 3"},
        },
        {
            "event_id": "a3",
            "scope_id": "smoke-a",
            "sequence": 3,
            "event_type": "evidence.projection",
            "payload": {"observed": "smoke plume drifting north east"},
        },
        {
            "event_id": "a4",
            "scope_id": "smoke-a",
            "sequence": 4,
            "event_type": "supervision.review",
            "payload": {"note": "keep crew clear of collapsing roof"},
        },
    ],
    "variant_b": [
        {
            "event_id": "b1",
            "scope_id": "smoke-b",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "evacuate east wing before flood rise", "unit": "bravo"},
        },
        {
            "event_id": "b2",
            "scope_id": "smoke-b",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "stairs blocked on floor two"},
        },
        {
            "event_id": "b3",
            "scope_id": "smoke-b",
            "sequence": 3,
            "event_type": "evidence.projection",
            "payload": {"observed": "water level rising one meter per hour"},
        },
        {
            "event_id": "b4",
            "scope_id": "smoke-b",
            "sequence": 4,
            "event_type": "supervision.review",
            "payload": {"note": "shelter group at north entrance"},
        },
    ],
    "variant_c": [
        {
            "event_id": "c1",
            "scope_id": "smoke-c",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "rescue crew from collapsed tunnel", "unit": "charlie"},
        },
        {
            "event_id": "c2",
            "scope_id": "smoke-c",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "comms intermittent near tunnel mouth"},
        },
        {
            "event_id": "c3",
            "scope_id": "smoke-c",
            "sequence": 3,
            "event_type": "evidence.projection",
            "payload": {"observed": "debris blocking secondary access"},
        },
        {
            "event_id": "c4",
            "scope_id": "smoke-c",
            "sequence": 4,
            "event_type": "supervision.review",
            "payload": {"note": "pair workers during cable clearing"},
        },
    ],
}


def _make_snapshot(variant: str, revision: int) -> ScopeEventSnapshotV1:
    events = _FIXTURE_EVENTS[variant]
    digest = (
        f"smoke-{variant}-rev{revision}-"
        + "d" * 48
    )[:64]
    return ScopeEventSnapshotV1(
        scope_id=events[0]["scope_id"],
        memory_revision=revision,
        events=tuple(events),
        snapshot_digest=digest,
        status="ok",
    )


def _window_refs(snapshot: ScopeEventSnapshotV1) -> set[tuple[str, str]]:
    return {
        (event["scope_id"], event["event_id"])
        for event in snapshot.events
        if isinstance(event.get("scope_id"), str) and isinstance(event.get("event_id"), str)
    }


def _scrub(text: str, secret: str) -> str:
    """Remove the api key value from any diagnostic text (defense in depth)."""
    if secret and secret in text:
        text = text.replace(secret, "<set>")
    return text


def _usage_tokens(store: LongTermMemoryStore) -> int | None:
    for entry in store.list_audit_entries():
        if entry.get("kind") == "reflection_usage":
            match = re.search(r"tokens=(\d+)", entry.get("reason", ""))
            if match:
                return int(match.group(1))
    return None


def _published_refs(
    store: LongTermMemoryStore, scope_id: str
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Published memory rows + all their support refs (production store data)."""
    rows = [
        row
        for row in store.list_memories(project_id=_PROJECT_ID, scope_id=scope_id)
        if row.get("status") == "published"
    ]
    refs: list[tuple[str, str]] = []
    for row in rows:
        refs.extend(
            store.list_support_refs(
                project_id=_PROJECT_ID, scope_id=scope_id, memory_key=row["memory_key"]
            )
        )
    return rows, refs


# ── deterministic fail-closed sanity (production chain, no model) ──────────


class _StaticPort:
    """Fake port returning a fixed response — deterministic validator checks."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls = 0

    def complete_with_function_call(self, *, system_prompt, user_prompt, tools):
        self.calls += 1
        return self._response


def _bad_missing_field(snapshot: ScopeEventSnapshotV1) -> dict[str, Any]:
    return {
        "function_call": [
            {
                "memory_key": "k-bad",
                "kind": "lesson",
                "statement": "coordinate at fires",
                # confidence deliberately missing
                "source_refs": [[snapshot.scope_id, snapshot.events[0]["event_id"]]],
            }
        ]
    }


def _bad_forged_ref(snapshot: ScopeEventSnapshotV1) -> dict[str, Any]:
    return {
        "function_call": [
            {
                "memory_key": "k-bad",
                "kind": "lesson",
                "statement": "coordinate at fires",
                "confidence": 0.9,
                "source_refs": [[snapshot.scope_id, "never-seen-event"]],
            }
        ]
    }


def _bad_truth_term(snapshot: ScopeEventSnapshotV1) -> dict[str, Any]:
    return {
        "function_call": [
            {
                "memory_key": "k-bad",
                "kind": "lesson",
                "statement": "compare with the oracle values",
                "confidence": 0.9,
                "source_refs": [[snapshot.scope_id, snapshot.events[0]["event_id"]]],
            }
        ]
    }


def _run_fail_closed_sanity() -> list[dict[str, Any]]:
    """3× production-chain rejection checks with zero model calls."""
    results: list[dict[str, Any]] = []
    cases = [
        ("missing_candidate_field", _bad_missing_field, "missing_candidate_field"),
        ("forged_source_ref", _bad_forged_ref, "forged_source_ref"),
        ("forbidden_truth_term", _bad_truth_term, "forbidden_truth_term"),
    ]
    for label, builder, expected_reason in cases:
        snapshot = _make_snapshot("variant_a", revision=1)
        root = Path(tempfile.mkdtemp(prefix=f"lt-smoke-failclosed-{label}-"))
        try:
            store = LongTermMemoryStore(root / "long_term.db").open()
            result = run_reflection(
                store,
                snapshot,
                POLICY_VERSION,
                project_id=_PROJECT_ID,
                model_port=_StaticPort(builder(snapshot)),
            )
            rows, _ = _published_refs(store, snapshot.scope_id)
            passed = (
                result.status == "rejected"
                and result.long_term_memory_written == 0
                and not rows
                and (result.reason or "").startswith(expected_reason)
            )
            results.append(
                {
                    "case": label,
                    "status": result.status,
                    "reason": result.reason,
                    "long_term_memory_written": result.long_term_memory_written,
                    "published_rows": len(rows),
                    "passed": passed,
                }
            )
            print(
                f"  [fail-closed] {label}: status={result.status} "
                f"reason={result.reason} written={result.long_term_memory_written} "
                f"rows={len(rows)} -> {'PASS' if passed else 'FAIL'}"
            )
        finally:
            store.close()
            shutil.rmtree(root, ignore_errors=True)
    return results


# ── main gate ──────────────────────────────────────────────────────────────


def main() -> int:
    env = load_env_file(_REPO_ROOT / ".env")
    port = build_reflection_model_port(env)
    if port is None:
        print(
            "FATAL: reflection model port unconfigured "
            "(need .env provider/model/api_key/api_base); gate cannot run.",
            file=sys.stderr,
        )
        return 1

    secret = env.get("api_key") or env.get("reflection_api_key") or ""
    api_base = port.api_base or ""
    host = urlparse(api_base).netloc or api_base
    evidence: dict[str, Any] = {
        "model": port.model,
        "provider": port.provider,
        "api_base_host": host,
        "api_key": "<set>",
        "config_source": "load_env_file(.env) -> build_reflection_model_port "
        "(reflection_* fallback to generic keys)",
        "runs": [],
        "fail_closed_sanity": [],
        "success_count": 0,
        "summary": {},
    }
    print(
        f"smoke model={port.model} provider={port.provider} api_base_host={host} "
        f"api_key=<set>"
    )
    print("running fail-closed sanity (deterministic, no model call)...")
    evidence["fail_closed_sanity"] = _run_fail_closed_sanity()
    sanity_ok = all(item["passed"] for item in evidence["fail_closed_sanity"])

    variants = ["variant_a", "variant_b", "variant_c"]
    success_count = 0
    for index in range(1, 6):
        variant = variants[(index - 1) % len(variants)]
        snapshot = _make_snapshot(variant, revision=index)
        root = Path(tempfile.mkdtemp(prefix=f"lt-smoke-{index}-"))
        run_record: dict[str, Any] = {
            "index": index,
            "variant": variant,
            "scope_id": snapshot.scope_id,
            "status": "failed",
            "reason": None,
            "candidates": 0,
            "published_rows": 0,
            "refs_in_window": False,
            "tokens": None,
            "latency_sec": None,
            "port_error": None,
        }
        store = LongTermMemoryStore(root / "long_term.db").open()
        started = time.monotonic()
        try:
            result = run_reflection(
                store,
                snapshot,
                POLICY_VERSION,
                project_id=_PROJECT_ID,
                model_port=port,
            )
            latency = round(time.monotonic() - started, 2)
            run_record["status"] = result.status
            run_record["reason"] = result.reason
            run_record["latency_sec"] = latency
            run_record["port_error"] = (
                _scrub(port.last_error or "", secret) if port.last_error else None
            )
            if result.status == "completed":
                rows, refs = _published_refs(store, snapshot.scope_id)
                window = _window_refs(snapshot)
                refs_in_window = all(ref in window for ref in refs)
                run_record["candidates"] = len(rows)
                run_record["published_rows"] = len(rows)
                run_record["refs_in_window"] = refs_in_window
                if refs_in_window:
                    success_count += 1
            run_record["tokens"] = _usage_tokens(store)
            evidence["runs"].append(run_record)
            print(
                f"  [run {index}] variant={variant} status={result.status} "
                f"reason={result.reason} candidates={run_record['candidates']} "
                f"refs_in_window={run_record['refs_in_window']} "
                f"tokens={run_record['tokens']} latency={latency}s"
                + (f" port_error={_scrub(port.last_error or '', secret)}" if port.last_error else "")
            )
        finally:
            store.close()
            shutil.rmtree(root, ignore_errors=True)

    evidence["success_count"] = success_count
    evidence["summary"] = {
        "gate": "5/5 completed + refs_in_window",
        "success_count": success_count,
        "target": 5,
        "gate_passed": success_count == 5,
        "fail_closed_sanity_passed": sanity_ok,
        "statuses": [r["status"] for r in evidence["runs"]],
        "rejection_categories": [
            r["reason"] for r in evidence["runs"] if r["status"] == "rejected"
        ],
        "tokens_total": sum(r["tokens"] or 0 for r in evidence["runs"]),
        "latency_total_sec": round(
            sum(r["latency_sec"] or 0 for r in evidence["runs"]), 2
        ),
    }

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    evidence_path = _RESULTS_DIR / f"long_term_smoke_{ts}.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"evidence: {evidence_path}")
    print(
        f"summary: success={success_count}/5 gate_passed={success_count == 5} "
        f"fail_closed_sanity={sanity_ok}/3"
    )
    return 0 if (success_count == 5 and sanity_ok) else 1


if __name__ == "__main__":
    sys.exit(main())

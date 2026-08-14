"""R2 gate material: real-model diagnosis loop smoke (main plan §8, R2).

Runs the production ``DiagnosisLoop`` (four read-only tools + bounded
agentic rounds + ``validate_diagnosis_response`` + short-lived
``DiagnosisMemoryStore`` + ``diagnosis.audit`` temporal event) 3 times
independently on temporary absolute memory roots + fixture source
evidence.  Hard gate: 3/3 runs must be ``ok`` with every candidate's
source refs inside the evidence window (validator enforced) and zero
truth terms.

Additionally measures the real round distribution (how many rounds until
``record_diagnosis`` vs ``rounds_exhausted``/``rejected``/``timeout``)
and reports per-run latency — the R2 review material for the human
judge (finding quality, suggestion direction, hallucination check).

Evidence JSON: ``sar_orch/results/diagnosis_smoke_<ts>.json``.  The api
key is never printed or recorded (always ``"<set>"``); provider/model
come from ``.env`` via ``build_reflection_model_port`` (``reflection_*``
keys with fallback to generic keys).

Usage (foreground, proxy bypass per AGENTS.md):
    env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \\
        uv run python sar_orch/scripts/diagnosis_smoke.py

Exit code 0 only when the 3/3 gate passes.
"""

from __future__ import annotations

import json
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

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.diagnosis import DiagnosisMemoryStore
from a2a.coordinator.memory.store import MemoryStore
from a2a.shared.env_loader import load_env_file
from sar_orch.diagnosis_loop import DiagnosisLoop
from sar_orch.long_term_reflection import build_reflection_model_port

_RESULTS_DIR = _REPO_ROOT / "sar_orch" / "results"


# ── fixture evidence (four online families + decisions; no truth terms) ────

_FIXTURE_EVENTS: dict[str, list[dict[str, Any]]] = {
    "variant_a": [
        {
            "event_id": "evt_ctl_1",
            "scope_id": "smoke-a",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "contain warehouse fire at sector 7"},
        },
        {
            "event_id": "evt_cb_1",
            "scope_id": "smoke-a",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "water pressure low at hydrant 3"},
        },
        {
            "event_id": "evt_dec_1",
            "scope_id": "smoke-a",
            "sequence": 3,
            "event_type": "coordinator_decision.assign_task",
            "payload": {"content": "dispatch alpha to sector 7", "who": "alpha"},
        },
        {
            "event_id": "evt_sup_1",
            "scope_id": "smoke-a",
            "sequence": 4,
            "event_type": "supervision.TASK_STALE",
            "payload": {"task_id": "dispatch-1"},
        },
    ],
    "variant_b": [
        {
            "event_id": "evt_ctl_1",
            "scope_id": "smoke-b",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "rescue civilians from collapsed building"},
        },
        {
            "event_id": "evt_cb_1",
            "scope_id": "smoke-b",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "alpha blocked by debris at entrance"},
        },
        {
            "event_id": "evt_dec_1",
            "scope_id": "smoke-b",
            "sequence": 3,
            "event_type": "coordinator_decision.reply_to_help",
            "payload": {"related_task_id": "dispatch-1", "response_preview": "reroute via east"},
        },
        {
            "event_id": "evt_sup_1",
            "scope_id": "smoke-b",
            "sequence": 4,
            "event_type": "supervision.WORKER_UNREACHABLE",
            "payload": {"worker_id": "alpha"},
        },
    ],
    "variant_c": [
        {
            "event_id": "evt_ctl_1",
            "scope_id": "smoke-c",
            "sequence": 1,
            "event_type": "control.dispatch.RUNNING",
            "payload": {"task": "extinguish chemical spill at depot"},
        },
        {
            "event_id": "evt_cb_1",
            "scope_id": "smoke-c",
            "sequence": 2,
            "event_type": "callback.status_update",
            "payload": {"status": "bob needs sand not water"},
        },
        {
            "event_id": "evt_ev_1",
            "scope_id": "smoke-c",
            "sequence": 3,
            "event_type": "evidence.projection",
            "payload": {"observed": "chemical type non-chemical at depot"},
        },
        {
            "event_id": "evt_dec_1",
            "scope_id": "smoke-c",
            "sequence": 4,
            "event_type": "coordinator_decision.update_plan",
            "payload": {"plan_nodes": [{"logical_id": "n1", "objective": "extinguish"}]},
        },
    ],
}


def _seed_store(root: Path, variant: str) -> tuple[MemoryStore, str]:
    """Fresh canonical MemoryStore with a scope + fixture temporal events."""
    from a2a.coordinator.memory.contracts import MemoryScopeV1

    cfg = MemoryConfig(experiment_id="run-1", memory_root=root).validate()
    store = MemoryStore(cfg.db_path)
    scope = MemoryScopeV1(
        project_id=cfg.project_id,
        experiment_id=cfg.experiment_id,
        context_id="ctx-1",
        runtime_epoch=0,
    )
    result = store.activate_scope(scope)
    assert result.scope_id is not None
    scope_id = result.scope_id
    for evt in _FIXTURE_EVENTS[variant]:
        store.append_temporal_event(
            event_id=evt["event_id"],
            scope_id=scope_id,
            sequence=evt["sequence"],
            event_type=evt["event_type"],
            occurred_at="2026-08-13T00:00:00+00:00",
            ingested_at="2026-08-13T00:00:00+00:00",
            actor_id="Coordinator",
            logical_task_id=None,
            dispatch_id=None,
            worker_task_id=None,
            tool_call_id=None,
            success=None,
            error=None,
            payload=json.dumps(evt["payload"], ensure_ascii=False),
            causation_id=None,
            correlation_id=None,
            idempotency_key=None,
        )
    return store, scope_id


def _scrub(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


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
        "gate": "3/3 runs status=ok, candidates>=1, refs verifiable (validator-enforced), zero truth terms",
        "runs": [],
        "round_distribution": {},
        "success_count": 0,
        "summary": {},
    }
    print(
        f"diagnosis smoke model={port.model} provider={port.provider} "
        f"api_base_host={host} api_key=<set>"
    )

    variants = ["variant_a", "variant_b", "variant_c"]
    success_count = 0
    round_dist: dict[str, int] = {}
    for index in range(1, 4):
        variant = variants[index - 1]
        root = Path(tempfile.mkdtemp(prefix=f"diag-smoke-{index}-"))
        run_record: dict[str, Any] = {
            "index": index,
            "variant": variant,
            "status": "failed",
            "reason": None,
            "rounds": None,
            "candidates": 0,
            "latency_sec": None,
            "port_error": None,
            "samples": [],
        }
        try:
            store: MemoryStore | None = None
            store, scope_id = _seed_store(root, variant)
            diag_root = root / "diagnosis"
            diag_store = DiagnosisMemoryStore(diag_root / "diagnosis.sqlite3")
            from a2a.coordinator.memory.contracts import DiagnosisConfig

            diag_cfg = DiagnosisConfig(
                experiment_id="run-1", memory_root=root, max_rounds=3, diagnosis_sec=90
            )
            loop = DiagnosisLoop(
                model_port=port,
                store=store,
                scope_id=scope_id,
                diagnosis_store=diag_store,
                config=diag_cfg,
            )
            started = time.monotonic()
            result = loop.run()
            latency = round(time.monotonic() - started, 2)
            run_record["status"] = result.status
            run_record["reason"] = result.reason
            run_record["rounds"] = result.rounds
            run_record["latency_sec"] = latency
            run_record["port_error"] = (
                _scrub(port.last_error or "", secret) if port.last_error else None
            )
            round_dist[result.status] = round_dist.get(result.status, 0) + 1
            if result.status == "ok" and result.validation is not None:
                run_record["candidates"] = len(result.validation.candidates)
                for candidate in result.validation.candidates:
                    run_record["samples"].append(
                        {
                            "target": candidate.target,
                            "finding": candidate.finding,
                            "suggestion": candidate.suggestion,
                            "confidence": candidate.confidence,
                            "source_refs": list(candidate.source_refs),
                            "diagnosis_key": candidate.diagnosis_key,
                            "policy_version": candidate.policy_version,
                        }
                    )
                if run_record["candidates"] >= 1:
                    success_count += 1
            evidence["runs"].append(run_record)
            print(
                f"  [run {index}] variant={variant} status={result.status} "
                f"rounds={result.rounds} candidates={run_record['candidates']} "
                f"latency={latency}s"
            )
            for sample in run_record["samples"]:
                print(
                    f"    target={sample['target']} conf={sample['confidence']} "
                    f"finding={sample['finding'][:80]!r}"
                )
        finally:
            if store is not None:
                store.close()
            shutil.rmtree(root, ignore_errors=True)

    evidence["round_distribution"] = round_dist
    evidence["success_count"] = success_count
    evidence["summary"] = {
        "gate_passed": success_count == 3,
        "total_runs": len(evidence["runs"]),
        "ok_runs": sum(1 for r in evidence["runs"] if r["status"] == "ok"),
        "rejected_runs": sum(1 for r in evidence["runs"] if r["status"] == "rejected"),
        "timeout_runs": sum(1 for r in evidence["runs"] if r["status"] == "timeout"),
        "rounds_exhausted_runs": sum(
            1 for r in evidence["runs"] if r["status"] == "rounds_exhausted"
        ),
        "avg_latency_sec": round(
            sum(r["latency_sec"] or 0 for r in evidence["runs"]) / 3, 2
        ),
    }
    print(f"summary: {json.dumps(evidence['summary'], ensure_ascii=False)}")

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RESULTS_DIR / f"diagnosis_smoke_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"evidence: {out_path}")
    return 0 if success_count == 3 else 1


if __name__ == "__main__":
    sys.exit(main())

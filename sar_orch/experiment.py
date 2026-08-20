#!/usr/bin/env python3
"""SAR 端到端实验运行器（End-to-end SAR experiment runner using my_a2a framework）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from a2a.shared.env_loader import load_env_file
from Agent.sandbox import SandboxPolicy
from sar_orch.barrier import SARBarrier
from sar_orch.coordinator import SARCoordinator
from sar_orch.logger import ExperimentLogger
from sar_orch.worker import SARWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("sar_experiment")

# Port assignments for up to 6 agents
AGENT_PORTS: dict[str, int] = {
    "Alice": 8191,
    "Bob": 8192,
    "Charlie": 8193,
    "David": 8194,
    "Emma": 8195,
    "Finn": 8196,
}

COORDINATOR_PORT = 8080

#: LLaMAR 论文 §5 的规划视界上限 L。论文原文："Average steps (L): The number
#: of high-level actions taken by the team to complete the task, capped at
#: L = 30 in our experiments. If the task is not completed within L steps, the
#: episode is deemed a failure."
#: 与论文表格对照时必须用这个值，否则 SR/TR/C/L 全都不可比。
PAPER_MAX_STEPS = 30

# Paths
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Default prompt/skills roots. --prompt-dir / --skills-dir override these;
# each is expected to contain coordinator/ and worker/ subdirectories, mirroring
# the historical sar_orch/prompts/{coordinator,worker} and
# sar_orch/skills/{coordinator,worker} layout.
_DEFAULT_PROMPT_DIR = os.path.join(_PROJECT_ROOT, "sar_orch", "prompts")
_DEFAULT_SKILLS_DIR = os.path.join(_PROJECT_ROOT, "sar_orch", "skills")

# Default results root (all experiment outputs go under sar_orch/results/)
_RESULTS_ROOT = os.path.join(_PROJECT_ROOT, "sar_orch", "results")


def _is_git_dirty() -> bool:
    """True when `git status --porcelain` reports any uncommitted changes.

    A dirty tree means the code that actually executed is not equal to
    HEAD's content -- recording just HEAD's SHA in that case would silently
    misattribute the run to a commit that isn't what ran.
    """
    try:
        import subprocess

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
    except Exception:
        pass
    return False


def _get_git_commit(dirty: bool | None = None) -> str:
    """Best-effort read of the current git short SHA.

    Appends `-dirty` when the working tree has uncommitted changes: the bare
    SHA would otherwise claim the run used HEAD's exact content, which is
    false whenever anything is uncommitted (see `_is_git_dirty`).
    """
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        sha = result.stdout.strip()
    except Exception:
        return ""
    if dirty is None:
        dirty = _is_git_dirty()
    return f"{sha}-dirty" if dirty else sha


def _prompt_hash_sources(
    prompt_dir: str | Path, skills_dir: str | Path
) -> list[tuple[str, Path]]:
    """Enumerate every .md file under prompt_dir and skills_dir.

    Each entry is paired with a stable sort key: the tagged path relative to
    its own root (e.g. "prompts/coordinator/system.md",
    "skills/worker/navigation/SKILL.md"). The key is what gets sorted, not
    filesystem enumeration order -- `Path.rglob` order is unspecified and can
    differ across machines/filesystems for byte-identical content, which
    would otherwise make the same prompt set hash differently depending on
    where it was checked out.
    """
    prompt_dir = Path(prompt_dir)
    skills_dir = Path(skills_dir)
    pairs: list[tuple[str, Path]] = []
    for root, tag in ((prompt_dir, "prompts"), (skills_dir, "skills")):
        if not root.is_dir():
            continue
        for f in root.rglob("*.md"):
            if f.is_file():
                key = f"{tag}/{f.relative_to(root).as_posix()}"
                pairs.append((key, f))
    return pairs


def compute_prompt_hash(prompt_dir: str | Path, skills_dir: str | Path) -> str:
    """SHA-256 (first 12 hex chars) over all prompt/skill .md content.

    Files are hashed in an explicit sort over relative-path strings (see
    `_prompt_hash_sources`), so the result is independent of directory
    traversal order while still changing whenever any file's content
    changes. This replaces the previous hardcoded `prompt_version` string,
    which never reflected the actual prompt/skill content of a run.
    """
    import hashlib

    pairs = sorted(_prompt_hash_sources(prompt_dir, skills_dir), key=lambda p: p[0])
    hasher = hashlib.sha256()
    for key, path in pairs:
        hasher.update(key.encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()[:12]


def snapshot_prompts(exp_dir: str | Path, prompt_dir: str | Path, skills_dir: str | Path) -> None:
    """Copy the exact prompt/skill files used by this run into
    <exp_dir>/prompt_snapshot/, preserving relative directory structure.

    This lets any historical run answer "what prompt did it actually use"
    without depending on sar_orch/prompts or sar_orch/skills staying
    unchanged after the run -- previously nothing recorded the actual prompt
    content, only a hardcoded label.
    """
    import shutil

    snapshot_root = Path(exp_dir) / "prompt_snapshot"
    for key, path in _prompt_hash_sources(prompt_dir, skills_dir):
        dest = snapshot_root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


async def probe_seed_support(
    *, api_key: str, api_base: str, model: str, seed: int
) -> bool | None:
    """Ask the gateway the same question twice with the same seed.

    Returns True if both replies match (seed appears honoured), False if they
    differ (accepted but ignored), None if the probe itself could not run.

    Why a probe rather than checking for a 4xx: measured against
    packyapi/deepseek-v4-flash, a valid `seed` yields 200 and is ignored, and an
    out-of-range one yields 400 `expected u64`. So the field is parsed and
    type-validated while never affecting sampling -- an error-code rule reports
    "supported" forever.

    The prompt is chosen to have real entropy: with a deterministic prompt both
    replies would match regardless of the seed, and the probe would report
    success from an artefact. Temperature is pinned high for the same reason.

    None (not False) on failure: "could not determine" and "determined to be
    unsupported" are different states, and collapsing them would let a network
    blip masquerade as a finding.
    """
    if not api_key:
        logger.warning("seed probe skipped: no API key available")
        return None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        prompt = "Invent one surprising eight-word sentence. Output only the sentence."
        replies = []
        for _ in range(2):
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=1.0,
                seed=seed,
            )
            replies.append((resp.choices[0].message.content or "").strip())
        return replies[0] == replies[1]
    except Exception as exc:  # noqa: BLE001 - probe must never abort the run
        logger.warning("seed probe failed (%s: %s)", type(exc).__name__, exc)
        return None


def build_run_metadata(
    *,
    run_id: str,
    scene: int,
    num_agents: int,
    seed: int,
    model: str,
    provider: str,
    api_base: str,
    max_steps: int,
    wall_clock_limit: float,
    sandbox_profile: str,
    coordinator_prompts: str,
    worker_prompts: str,
    agent_names: list[str] | None = None,
    code_commit: str = "",
    git_dirty: bool | None = None,
    prompt_hash: str = "",
    prompt_tag: str = "unlabeled",
) -> dict:
    if git_dirty is None:
        git_dirty = _is_git_dirty()
    return {
        "run_id": run_id,
        "env_name": "SAR",
        "scenario_id": f"scene_{scene}",
        "scene": scene,
        "seed": seed,
        "agent_count": num_agents,
        "agent_names": agent_names or ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents],
        "model": model,
        "provider": provider,
        "api_base": api_base,
        "max_steps": max_steps,
        "wall_clock_timeout": wall_clock_limit,
        "sandbox_profile": sandbox_profile,
        "task_objective": "Extinguish all fires and rescue all persons",
        "success_criteria": "SAR checker subtasks complete",
        "coordinator_prompts": coordinator_prompts,
        "worker_prompts": worker_prompts,
        # Human label, settable via --prompt-tag; NOT a content identity.
        # Content identity is prompt_hash below -- prompt_version can be
        # forgotten to update or copy-pasted between runs, prompt_hash can't.
        "prompt_version": prompt_tag,
        "prompt_hash": prompt_hash,
        "code_commit": code_commit or _get_git_commit(dirty=git_dirty),
        "git_dirty": git_dirty,
    }


def classify_end_reason(
    *,
    finished: bool,
    steps: int,
    max_steps: int,
    elapsed_seconds: float,
    wall_clock_limit: float,
    a2a_done: bool,
    a2a_error: bool,
    coordinator_error: bool,
) -> str:
    if finished:
        return "success"
    if coordinator_error or a2a_error:
        return "framework_error"
    if elapsed_seconds >= wall_clock_limit:
        return "wall_clock_timeout"
    if steps >= max_steps:
        return "max_steps_reached"
    if a2a_done:
        return "coordinator_finished_early"
    return "stopped_before_success"


def _finalize_truth_recorder(
    truth_recorder, coordinator, terminal_status: str
) -> dict | None:
    """Resolve the canonical scope (read-only), freeze the evaluator-private
    manifest, and report whether a manifest was produced.

    In legacy memory mode (or when no canonical scope can be resolved) the
    recorder skips cleanly and ``None`` is returned so the caller neither wires
    a truth manifest into the terminal evaluator nor fails the run.
    """
    scope_id = None
    server = getattr(coordinator, "_server", None)
    resolver = (
        getattr(server, "_resolve_export_scope_id", None)
        if server is not None
        else None
    )
    if callable(resolver):
        try:
            scope_id = resolver()
        except Exception:
            logger.exception("truth recorder scope resolution failed")
    truth_recorder.set_scope_id(scope_id)
    manifest = truth_recorder.finalize(terminal_status)
    if manifest is None:
        logger.warning(
            "truth recorder: no canonical scope resolved; manifest skipped "
            "(legacy memory mode)"
        )
        return None
    logger.info(
        "truth manifest frozen: %s (scope %s...)",
        truth_recorder.manifest_path,
        str(scope_id or "")[:12],
    )
    return {
        "manifest": str(truth_recorder.manifest_path),
        "trace": str(truth_recorder.trace_path),
        "scope_id": scope_id,
        "terminal_status": terminal_status,
    }


def _invoke_run_terminal_memory_eval(
    *,
    coordinator,
    exp_dir: Path,
    memory_read_mode: str,
    truth_manifest: str | None,
    truth_trace: str | None,
) -> dict:
    """Phase 5 run-terminal wiring: materialize + acceptance/projection eval.

    Runs only when canonical Memory is configured (``shadow``/``read_port``);
    ``legacy`` runs keep their existing artifacts untouched (legacy retirement
    is NOT enabled here).  The exporter deterministically rebuilds the
    compatibility artifacts (``semantic_map.jsonl`` + export manifest) from the
    committed canonical set; then the acceptance evaluator writes
    ``memory_acceptance.json`` and, when a truth manifest is provided, the
    projection-quality evaluator writes ``memory_projection_quality.json``.

    The acceptance gate is logged but never crashes the experiment: a real run
    may legitimately contain framework errors (worker_busy / task routing), and
    H3 retirement has not been authorised.  Returns a small summary dict.
    """
    result: dict = {"materialized": False}
    if memory_read_mode not in ("shadow", "read_port"):
        return result
    server = getattr(coordinator, "_server", None)
    if server is None:
        logger.info("run-terminal memory eval skipped: coordinator server unavailable")
        return result
    try:
        materialized = server.materialize_compatibility_artifacts(str(exp_dir))
        result["materialized"] = materialized is not None
        if materialized is not None:
            result["scope_id"] = materialized.get("scope_id")
    except Exception:
        logger.exception("run-terminal compatibility materialization failed")

    try:
        from sar_orch.eval.memory_acceptance import (
            evaluate as acceptance_evaluate,
        )
        from sar_orch.eval.memory_acceptance import (
            gate as acceptance_gate,
        )
        from sar_orch.eval.memory_acceptance import (
            write_artifact as acceptance_write_artifact,
        )

        acceptance_result, unknown = acceptance_evaluate(str(exp_dir))
        acceptance_write_artifact(exp_dir, acceptance_result)
        result["acceptance"] = {
            "failed_tool_rows": int(acceptance_result["failed_tool_rows"]),
            "missing_error_code_rows": int(
                acceptance_result["missing_error_code_rows"]
            ),
            "framework_error_counts": acceptance_result["framework_error_counts"],
        }
        try:
            acceptance_gate(acceptance_result, unknown)
            result["acceptance_gate"] = "pass"
        except Exception as exc:  # noqa: BLE001 - gate is logged, not fatal
            logger.warning(
                "memory_acceptance gate not satisfied (logged, run continues): %s", exc
            )
            result["acceptance_gate"] = "fail"
    except Exception:
        logger.exception("memory_acceptance evaluation failed")

    if truth_manifest:
        try:
            from sar_orch.eval.memory_projection_quality import (
                evaluate as pq_evaluate,
            )
            from sar_orch.eval.memory_projection_quality import (
                write_artifact as pq_write_artifact,
            )

            pq_artifact = pq_evaluate(str(exp_dir), truth_manifest, truth_trace)
            pq_write_artifact(exp_dir, pq_artifact)
            result["projection_quality"] = pq_artifact.get("metric_status")
        except Exception:
            logger.exception("memory_projection_quality evaluation failed")
    return result


def _invoke_run_terminal_long_term_reflection(
    *,
    coordinator,
    exp_dir: Path,
    long_term_mode: str,
    lt_config,
    truth_manifest: str | None,
    env: dict,
) -> dict:
    """Phase 4 run-terminal long-term wiring: drain → snapshot → reflection.

    Order (main plan §3.4.1): first drain the in-flight rolling reflection
    (join with ``[timeout] reflection_sec``, default 60s), then take the
    terminal committed snapshot and run the terminal reflection.  A join
    timeout records a typed timeout status and never blocks run exit.
    Phase 4 never invokes a real model: an unconfigured model port skips the
    reflection (``skipped_model_unconfigured``) instead of failing the run —
    except in ``read`` mode, where a missing ``reflection_api_key`` raises a
    typed D8 error (explicit rejection, never a silent downgrade to
    shadow/off).
    With ``quality_enabled`` the read-only ``long_term_memory_quality``
    evaluator writes ``long_term_memory_quality.json`` into the results dir
    (mode=ro; source/run/project DBs untouched).
    """
    from a2a.coordinator.memory.contracts import MemoryContractError

    result: dict = {"status": "off"}
    if long_term_mode == "off" or coordinator is None:
        return result
    store = getattr(coordinator, "long_term_store", None)
    if store is None:
        result["status"] = "no_store"
        return result
    try:
        from sar_orch.long_term_reflection import (
            build_reflection_model_port,
            drain_inflight_reflection,
            reflection_run,
        )

        # 1. drain the in-flight rolling reflection (join with timeout)
        drain = drain_inflight_reflection(
            timeout_sec=getattr(lt_config, "reflection_sec", 60)
        )
        result["drain"] = drain.status
        # Phase 4 (P4): surface the second-channel diagnosis outcome the
        # in-flight worker recorded (typed ok/rejected/timeout/skip — D8).
        # There is deliberately NO terminal diagnosis run: the rolling
        # trigger is the only diagnosis channel and terminal = drop
        # (diagnosis is non-essential and must never block run exit).
        result["diagnosis"] = (drain.result or {}).get("diagnosis")
        if drain.status == "timeout":
            try:
                store.record_audit(
                    "reflection_timeout",
                    "terminal drain timed out; in-flight rolling reflection "
                    "left running (typed timeout, run not blocked)",
                )
            except Exception:
                logger.exception("failed to record reflection timeout audit")

        # 2. terminal committed snapshot (active / last scope)
        snapshot = coordinator.long_term_snapshot()
        if snapshot is None:
            result["status"] = "no_snapshot"
            return result
        if getattr(snapshot, "status", "ok") != "ok":
            result["status"] = f"snapshot_{snapshot.status}"
            return result

        # 3. terminal reflection (offline in P4: no real model call)
        model_port = build_reflection_model_port(env)
        if model_port is None:
            if long_term_mode == "read":
                # D8（M2）：read 模式缺 reflection_api_key → 显式 typed 拒绝，
                # 不静默降级；shadow/off 保持跳过语义。
                raise MemoryContractError(
                    "read_mode_requires_api_key",
                    "read mode requires reflection_api_key (D8); refusing to "
                    "silently downgrade to shadow/off",
                )
            result["status"] = "skipped_model_unconfigured"
            return result
        outcome = reflection_run(
            store=store,
            snapshot=snapshot,
            project_id="llamar",
            model_port=model_port,
        )
        result["status"] = outcome.status
        result["run_id"] = outcome.run_id
        result["long_term_memory_written"] = outcome.long_term_memory_written
        result["reason"] = outcome.reason

        # 4. read-only quality evaluator (terminal-only)
        if getattr(lt_config, "quality_enabled", True):
            try:
                from sar_orch.eval.long_term_memory_quality import (
                    evaluate_long_term_memory_quality,
                )

                artifact = evaluate_long_term_memory_quality(
                    exp_dir, truth_manifest=truth_manifest
                )
                result["quality"] = artifact.get("metrics")
            except Exception:
                logger.exception("long_term_memory_quality evaluation failed")
                result["quality"] = "error"
    except MemoryContractError as exc:
        if exc.code == "read_mode_requires_api_key":
            # D8（M2）：read 模式缺 key 的 typed 拒绝必须向上传播（显式拒绝，
            # 不静默降级）；其余 typed 存储错误仍按“不阻塞 run 退出”转换。
            raise
        logger.exception("run-terminal long-term reflection failed")
        result["status"] = "failed"
    except Exception:
        logger.exception("run-terminal long-term reflection failed")
        result["status"] = "failed"
    return result


async def run_experiment(
    scene: int = 1,
    num_agents: int = 2,
    seed: int = 42,
    model: str = "deepseek-v4-flash",
    provider: str = "openai",
    api_base: str = "https://api.deepseek.com",
    api_key_env: str = "OPENAI_API_KEY",
    max_steps: int | None = None,
    coordinator_port: int = 8080,
    agent_base_port: int = 8191,
    log_dir: str | None = None,
    sandbox_profile: str = "workspace",
    state_mode: str = "semantic",
    coordinator_prompt: str | None = None,
    enable_peer_mail: bool = False,
    wall_clock_limit: float = 3600.0,
    temperature: float = 0.7,
    llm_seed: int | None = None,
    prompt_dir: str | None = None,
    skills_dir: str | None = None,
    prompt_tag: str = "unlabeled",
    memory_read_mode: str = "read_port",
    truth_manifest: str | None = None,
    truth_trace: str | None = None,
    truth_output_dir: str | None = None,
    # Phase 4: run-local long-term memory mode (off|shadow|read).  ``off``
    # performs zero long-term DB I/O; shadow/read persist published
    # reflection products (atomic committed snapshots) but never inject
    # them into Context (P5).  Phase 4 never invokes a real model.
    long_term_mode: str = "off",
) -> dict:
    """Run one full SAR experiment.

    Args:
        max_steps: Maximum environment steps (step-based, like original LLaMAR).
            Defaults to scene's task_timeout value.
        coordinator_port: Port for the coordinator HTTP server.
        agent_base_port: Base port for agent A2A servers (each agent gets base + index).
        log_dir: Explicit log directory. If None, auto-generated timestamp dir.
        coordinator_prompt: Optional override for the initial task sent to the coordinator.
        prompt_dir: Root directory containing coordinator/ and worker/ system
            prompt subdirectories. Defaults to sar_orch/prompts. This is the
            phase-5 self-evolution hook: pointing it at a candidate prompt
            set is how a candidate actually reaches an experiment run.
        skills_dir: Root directory containing coordinator/ and worker/ skill
            subdirectories. Defaults to sar_orch/skills. Same self-evolution
            role as prompt_dir, for skills specifically.
        prompt_tag: Human-readable label recorded as metadata["prompt_version"].
            Purely cosmetic -- prompt_hash is the content identity used for
            reproducibility and candidate verification.
        truth_manifest: Evaluator-private truth manifest (JSON) frozen after run
            terminal; when provided the terminal-only memory_projection_quality
            evaluator runs (canonical Memory mode only).
        truth_trace: Optional override of the truth trace path in the manifest.
        truth_output_dir: Optional evaluator-private directory for the Phase 5
            truth recorder (per-step truth_trace.jsonl + terminal
            truth_manifest.json). When provided and no explicit
            ``truth_manifest`` is given, the generated manifest is wired into
            the terminal memory_projection_quality evaluator. The directory
            must not be inside the run results dir (H1 evaluator-private
            boundary: the recorder never writes anywhere agents can read).
    """
    prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
    skills_dir = skills_dir or _DEFAULT_SKILLS_DIR
    coordinator_prompts_path = str(Path(prompt_dir) / "coordinator")
    worker_prompts_path = str(Path(prompt_dir) / "worker")
    coordinator_skills_path = str(Path(skills_dir) / "coordinator")
    worker_skills_path = str(Path(skills_dir) / "worker")
    agent_names = ["Alice", "Bob", "Charlie", "David", "Emma", "Finn"][:num_agents]

    logger.info("=" * 60)
    logger.info("SAR Experiment: scene=%d, agents=%d, seed=%d", scene, num_agents, seed)
    logger.info("Model: %s (provider=%s)", model, provider)
    logger.info("=" * 60)

    # 1. Create SARBarrier
    barrier = SARBarrier(num_agents=num_agents, scene=scene, seed=seed)
    # 论文 §5 把规划视界固定为 L=30（"capped at L=30 in our experiments.
    # If the task is not completed within L steps, the episode is deemed a
    # failure."）。场景自带的 task_timeout 并不统一（scene_1 是 1200，
    # scene_2–5 是 35），直接用它会让不同场景跑在不可比的预算下，且与论文
    # 报告的 L 口径不一致。因此默认取 PAPER_MAX_STEPS，显式传 --max-steps
    # 才覆盖。
    max_steps = max_steps or PAPER_MAX_STEPS
    logger.info("SARBarrier initialized -- max_steps=%d", max_steps)

    # 2. Create experiment log directory with unified naming convention
    if log_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        experiment_dir_name = f"{timestamp}_s{scene}_s{seed}_a{num_agents}"
        log_dir = str(Path(_RESULTS_ROOT) / experiment_dir_name)

    exp_dir = Path(log_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Create internal subdirectory structure
    coord_dir = exp_dir / "coordinator"
    workers_dir = exp_dir / "workers"
    supervision_dir = exp_dir / "supervision"
    coord_dir.mkdir(exist_ok=True)
    supervision_dir.mkdir(exist_ok=True)

    # Create per-worker subdirectories
    worker_log_dirs: dict[str, str] = {}
    for name in agent_names:
        wdir = workers_dir / name
        wdir.mkdir(parents=True, exist_ok=True)
        worker_log_dirs[name] = str(wdir)

    logger.info("Experiment logs unified under: %s", exp_dir)

    # 3. Create experiment logger
    exp_logger = ExperimentLogger(
        experiment_name="sar_experiment", log_dir=str(exp_dir)
    )

    run_id = f"sar-scene{scene}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"
    if wall_clock_limit <= 0:
        wall_clock_limit = float("inf")  # 0 表示无墙钟时间上限
    git_dirty = _is_git_dirty()
    code_commit = _get_git_commit(dirty=git_dirty)
    prompt_hash = compute_prompt_hash(prompt_dir, skills_dir)
    exp_logger.set_run_context(run_id=run_id, model=model, prompt_version=prompt_tag)

    # Phase 5: opt-in evaluator-private truth recorder.  Must be created before
    # the poll loop so every executed step is captured, and its output dir is
    # kept outside the run results dir (H1 boundary: agents/workers must never
    # be able to read the raw truth trace during the run).
    truth_recorder = None
    if truth_output_dir is not None:
        truth_out = Path(truth_output_dir)
        try:
            if str(truth_out.resolve()).startswith(str(exp_dir.resolve())):
                raise ValueError(
                    f"--truth-output-dir {truth_out} must not be inside the run "
                    f"results dir {exp_dir} (evaluator-private boundary)"
                )
        except OSError:
            pass  # resolution edge cases fall through to recorder init
        from sar_orch.eval.truth_recorder import TruthRecorder

        truth_recorder = TruthRecorder(
            barrier,
            truth_out,
            run_id=run_id,
            scene=scene,
            num_agents=num_agents,
            seed=seed,
        )
        logger.info("Truth recorder enabled; output dir: %s", truth_out)
    code_commit = _get_git_commit()
    metadata = build_run_metadata(
        run_id=run_id,
        code_commit=code_commit,
        git_dirty=git_dirty,
        prompt_hash=prompt_hash,
        prompt_tag=prompt_tag,
        scene=scene,
        num_agents=num_agents,
        seed=seed,
        model=model,
        provider=provider,
        api_base=api_base,
        max_steps=max_steps,
        wall_clock_limit=wall_clock_limit if wall_clock_limit != float("inf") else None,
        sandbox_profile=sandbox_profile,
        coordinator_prompts=coordinator_prompts_path,
        worker_prompts=worker_prompts_path,
    )
    metadata["state_mode"] = state_mode
    metadata["enable_peer_mail"] = enable_peer_mail
    metadata["temperature"] = temperature
    # Named llm_seed, never `seed`: `seed` above is the scene/env seed. Two
    # different knobs, and pooling them would make the variance baseline
    # uninterpretable.
    metadata["llm_seed"] = llm_seed
    # Whether the gateway honours `seed` must be PROBED, not inferred from the
    # absence of an error. Measured on packyapi/deepseek-v4-flash: a valid seed
    # returns 200 and is silently ignored (same seed twice -> different output),
    # while an out-of-range seed returns 400 `expected u64` -- i.e. the field is
    # parsed and type-checked but never reaches sampling. Any rule keyed on 4xx
    # would therefore record `true` forever: a parameter accepted, written to
    # metadata, and never in effect. That is the exact defect class this project
    # keeps rediscovering, so it is settled by behaviour or left unknown.
    if llm_seed is None:
        metadata["llm_seed_supported"] = None  # not applicable, distinct from False
    else:
        metadata["llm_seed_supported"] = await probe_seed_support(
            api_key=os.environ.get(api_key_env, ""),
            api_base=api_base,
            model=model,
            seed=llm_seed,
        )
        if metadata["llm_seed_supported"] is False:
            logger.warning(
                "--llm-seed=%s was requested but this gateway ignores it "
                "(probed: identical seed produced differing output). Runs in "
                "this batch are NOT deterministically reproducible; treat the "
                "variance baseline as pure sampling repetition.",
                llm_seed,
            )
        elif metadata["llm_seed_supported"] is None:
            logger.warning(
                "--llm-seed=%s requested but seed support could not be probed; "
                "recording llm_seed_supported=null rather than assuming.",
                llm_seed,
            )
    exp_logger.write_metadata(metadata)
    # Copy the exact prompt/skill files this run loaded into the run
    # directory. Without this, a historical run's prompt_hash identifies
    # *that* content changed but not *what* it was -- sar_orch/prompts and
    # sar_orch/skills keep evolving, so the source files a hash was computed
    # from may no longer exist by the time someone investigates the run.
    snapshot_prompts(exp_dir, prompt_dir, skills_dir)

    # Create sandbox policy based on profile
    _project_root = Path(_PROJECT_ROOT)
    if sandbox_profile == "off":
        sandbox_policy = SandboxPolicy.off()
        logger.info("Sandbox: disabled (profile=off)")
    elif sandbox_profile == "workspace":
        _result_dir = Path(exp_logger.get_log_dir())
        sandbox_policy = SandboxPolicy.workspace(
            project_root=_project_root,
            workspace_dir="./workspace",
            write_roots=[_result_dir],
        )
        logger.info("Sandbox: workspace profile (write_roots=[%s])", _result_dir)
    else:
        raise ValueError(
            f"Invalid sandbox profile '{sandbox_profile}'. Must be 'off' or 'workspace'."
        )

    # Phase 4: generate per-run coordinator secret for peer mail
    if enable_peer_mail:
        import secrets

        coordinator_secret = secrets.token_bytes(32)
        logger.info("Peer mail enabled — coordinator secret generated")
    else:
        coordinator_secret = None

    # Phase 2: secure memory mode requires a protected per-run callback secret.
    if memory_read_mode in ("shadow", "read_port"):
        import secrets

        coordinator_secret = coordinator_secret or secrets.token_bytes(32)
        logger.info(
            "Memory mode %s — coordinator callback secret generated",
            memory_read_mode,
        )

    workers: dict[str, SARWorker] = {}
    coordinator: SARCoordinator | None = None
    final_metrics: dict = {
        "finished": False,
        "steps": 0,
        "coverage": 0.0,
        "transport_rate": 0.0,
        "elapsed_seconds": 0.0,
    }

    try:
        # 2.5 Load .env and set API key env var (coordinator RouterAgent reads from env)
        _env = load_env_file(str(Path(__file__).parent.parent / ".env"))
        if "api_key" in _env:
            os.environ[api_key_env] = _env["api_key"]

        # Phase 4: long_term.config tunables (D9).  An unparseable/invalid
        # file forces long_term_mode=off with a typed error logged — never a
        # silent fallback into a running trigger.
        lt_config = None
        if long_term_mode != "off":
            try:
                from sar_orch.long_term_reflection import load_long_term_config

                lt_config = load_long_term_config(
                    str(Path(__file__).parent.parent / "long_term.config")
                )
            except Exception as exc:  # noqa: BLE001 - LongTermConfigError -> off
                logger.warning(
                    "long_term.config invalid — forcing long_term_mode=off: %s", exc
                )
                long_term_mode = "off"
                lt_config = None

        # Phase 4 (P4): [diagnosis] tunables (P3 loader).  An unparseable /
        # invalid section disables ONLY the diagnosis channel (fail-closed,
        # typed error logged) — long-term mode itself is unaffected because
        # the diagnosis channel is non-essential (D8).
        diag_runtime = None
        if long_term_mode != "off":
            try:
                from sar_orch.long_term_reflection import load_diagnosis_config

                diag_runtime = load_diagnosis_config(
                    str(Path(__file__).parent.parent / "long_term.config")
                )
            except Exception as exc:  # noqa: BLE001 - DiagnosisConfigError -> off
                logger.warning(
                    "long_term.config [diagnosis] invalid — diagnosis channel "
                    "disabled: %s",
                    exc,
                )
                diag_runtime = None

        # 3. Create and start coordinator FIRST so workers can connect immediately
        coordinator = SARCoordinator(
            host="0.0.0.0",
            port=coordinator_port,
            a2a_port=coordinator_port + 1,
            barrier=barrier,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key_env=api_key_env,
            prompts_dir=coordinator_prompts_path,
            skills_dir=coordinator_skills_path,
            log_dir=str(coord_dir),
            supervision_dir=str(supervision_dir),
            orchestration_mode="agentic",
            exp_logger=exp_logger,
            sandbox_policy=sandbox_policy,
            state_mode=state_mode,
            max_steps=max_steps,
            map_summary_path=str(exp_dir / "map_summary.jsonl"),
            enable_peer_mail=enable_peer_mail,
            coordinator_secret=coordinator_secret,
            temperature=temperature,
            llm_seed=llm_seed,
            memory_read_mode=memory_read_mode,
            run_id=run_id,
            long_term_mode=long_term_mode,
            diagnosis_tunables=diag_runtime,
        )

        logger.info("SARCoordinator starting on port %d", coordinator_port)
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for coordinator to bind ports
        await asyncio.sleep(3.0)

        # Phase 4: wire the rolling long-term reflection runtime (shadow/read;
        # off performs zero long-term DB I/O).  The model port is built from
        # .env reflection_* keys; when unconfigured the rolling trigger skips
        # (never invokes a model, never blocks the run).
        if long_term_mode != "off" and coordinator.long_term_store is not None:
            from sar_orch.long_term_reflection import (
                build_reflection_model_port,
                configure_long_term_runtime,
            )

            model_port = build_reflection_model_port(_env)
            configure_long_term_runtime(
                store=coordinator.long_term_store,
                snapshot_provider=coordinator.long_term_snapshot,
                project_id="llamar",
                model_port=model_port,
                config=lt_config,
            )
            logger.info(
                "long-term rolling reflection wired (mode=%s, model_port=%s)",
                long_term_mode,
                "configured" if model_port is not None else "unconfigured(skip)",
            )
            # Phase 4 (P4): second channel — agentic diagnosis loop on the
            # same rolling trigger (并存不替代, main plan §3.2).  Fail-closed:
            # a missing diagnosis store merely skips the channel; it never
            # affects the long-term reflection or the run.
            diag_config = coordinator.diagnosis_config
            if coordinator.diagnosis_store is not None and diag_config is not None:
                from sar_orch.long_term_reflection import (
                    configure_diagnosis_runtime,
                )

                diagnosis_model_port = build_reflection_model_port(
                    _env, timeout_sec=diag_config.diagnosis_sec
                )
                configure_diagnosis_runtime(
                    canonical_store=coordinator.memory_store,
                    diagnosis_store=coordinator.diagnosis_store,
                    diagnosis_config=diag_config,
                    model_port=diagnosis_model_port,
                )
                logger.info(
                    "diagnosis channel wired (inject_enabled=%s, min_confidence=%s, "
                    "model_port=%s, timeout_sec=%s)",
                    diag_config.inject_enabled,
                    diag_config.min_confidence,
                    "configured"
                    if diagnosis_model_port is not None
                    else "unconfigured(skip)",
                    diag_config.diagnosis_sec,
                )
            else:
                logger.warning(
                    "diagnosis channel skipped — no diagnosis store "
                    "(long-term mode %s)",
                    long_term_mode,
                )

        # Redirect semantic_map.jsonl to the top-level experiment directory
        if coordinator._semantic_map is not None:
            coordinator._semantic_map.set_jsonl_path(
                str(exp_dir / "semantic_map.jsonl")
            )
            logger.info(
                "semantic_map.jsonl path set to: %s", exp_dir / "semantic_map.jsonl"
            )

        # 4. Create and start workers (they immediately connect to coordinator's WS)
        for i, name in enumerate(agent_names):
            port = agent_base_port + i
            worker = SARWorker(
                worker_id=name,
                agent_name=name,
                agent_idx=i,
                barrier=barrier,
                a2a_port=port,
                coordinator_url=f"ws://localhost:{coordinator_port}",
                model=model,
                provider=provider,
                api_base=api_base,
                api_key_env=api_key_env,
                prompts_dir=worker_prompts_path,
                skills_dir=worker_skills_path,
                log_dir=worker_log_dirs[name],
                exp_logger=exp_logger,
                sandbox_policy=sandbox_policy,
                enable_peer_mail=enable_peer_mail,
                coordinator_secret=coordinator_secret,
                temperature=temperature,
                llm_seed=llm_seed,
                memory_read_mode=memory_read_mode,
            )
            workers[name] = worker
            worker.start()
            logger.info("Worker %s started on port %d", name, port)

        # Give workers time to register with coordinator
        # Phase 2+3: Increased from 2.0 to 5.0 to ensure workers are fully registered
        # before coordinator starts dispatching (prevents task_not_routable_yet errors)
        await asyncio.sleep(5.0)

        # 5. Submit initial task (fire-and-forget — poll barrier in parallel)
        task_description = coordinator_prompt or (
            "Extinguish all fires and rescue all persons"
        )
        logger.info("Submitting initial task: %s", task_description)
        a2a_task = asyncio.create_task(coordinator.submit_task(task_description))
        await asyncio.sleep(0.5)  # Let A2A start processing

        # 6. Poll barrier for completion (runs immediately, not blocked by A2A)
        # Uses STEP-BASED timeout (max_steps) like original LLaMAR, not wall-clock.
        # Wall-clock guard prevents indefinite stall from barrier timeouts.
        start_time = time.time()
        poll_interval = 2.0
        _last_step_logged = -1
        _last_supervision_count = 0

        a2a_done = False
        a2a_error = False
        coordinator_error = False

        while not barrier.is_finished() and barrier.get_metrics()["steps"] < max_steps:
            await asyncio.sleep(poll_interval)

            if coord_task.done():
                exc = coord_task.exception()
                if exc:
                    logger.error("Coordinator failed: %s", exc)
                    coordinator_error = True
                    break
            if a2a_task.done() and not a2a_task.cancelled():
                exc = a2a_task.exception()
                if exc:
                    logger.error("A2A orchestration failed: %s", exc)
                    a2a_error = True
                else:
                    result = a2a_task.result()
                    if isinstance(result, str) and result.startswith("Error:"):
                        logger.error("A2A orchestration returned an error: %s", result)
                        a2a_error = True
                    else:
                        logger.info("A2A orchestration completed; exiting poll loop")
                        a2a_done = True
                break

            elapsed = time.time() - start_time
            if elapsed > wall_clock_limit:
                logger.warning(
                    "Wall-clock limit %.0fs reached, stopping (step %d/%d)",
                    wall_clock_limit,
                    barrier.get_metrics()["steps"],
                    max_steps,
                )
                break

            metrics = barrier.get_metrics()
            logger.info(
                "Step %d/%d | Coverage: %.2f | Transport: %.2f | Finished: %s | %.0fs",
                metrics["steps"],
                max_steps,
                metrics["coverage"],
                metrics["transport_rate"],
                metrics["finished"],
                elapsed,
            )

            # Log every step that completed since the last poll (not just the
            # latest) — drain_step_logs() buffers all of them, so a poll
            # interval slower than step throughput can't silently drop rows.
            drained_logs = (
                barrier.drain_step_logs() if hasattr(barrier, "drain_step_logs") else []
            )
            for step_log in drained_logs:
                step_num = step_log.get("step", metrics["steps"])
                if step_num <= _last_step_logged:
                    continue
                exp_logger.log_step(
                    step_num=step_num,
                    actions=step_log.get("actions", []),
                    successes=step_log.get("successes", []),
                    observations=step_log.get("observations", []),
                    coverage=step_log.get("coverage", metrics["coverage"]),
                    transport_rate=step_log.get(
                        "transport_rate", metrics["transport_rate"]
                    ),
                    finished=step_log.get("finished", metrics["finished"]),
                    timeout_agents=step_log.get("timeout_agents", []),
                    map_recall=(
                        coordinator._semantic_map.map_recall()
                        if coordinator is not None
                        and coordinator._semantic_map is not None
                        else 0.0
                    ),
                    freshness=(
                        coordinator._semantic_map.freshness()
                        if coordinator is not None
                        and coordinator._semantic_map is not None
                        else 0.0
                    ),
                    run_id=run_id,
                    max_steps=max_steps,
                    remaining_steps=max(0, max_steps - step_num),
                    wall_time_since_start=elapsed,
                    step_duration_ms=step_log.get("step_duration_ms", ""),
                    error_types=step_log.get("error_types", []),
                    completed_subtasks_delta=step_log.get(
                        "completed_subtasks_delta", []
                    ),
                    end_reason="",
                )
                # Incremental summary write — survives shell timeout kills
                exp_logger.flush_summary()
                if coordinator is not None and coordinator._semantic_map is not None:
                    coordinator._semantic_map.update_step_budget(
                        current_step=step_num,
                        max_steps=max_steps,
                    )
                if truth_recorder is not None:
                    try:
                        truth_recorder.record_step(step_num)
                    except Exception:
                        logger.exception(
                            "truth recorder record_step failed (step %d)", step_num
                        )
                _last_step_logged = step_num

            # Phase 4: rolling long-term reflection trigger points (main plan
            # §3.4.1): task complete / supervision event / every N env_step.
            # The min-interval throttle and the in-flight coalesce live inside
            # maybe_trigger_rolling_reflection (async thread, never blocks the
            # poll loop).
            if long_term_mode != "off" and coordinator.long_term_store is not None:
                from sar_orch.long_term_reflection import (
                    maybe_trigger_rolling_reflection,
                )

                for step_log in drained_logs:
                    step_num = step_log.get("step", metrics["steps"])
                    if (
                        lt_config.task_complete
                        and step_log.get("finished")
                    ):
                        maybe_trigger_rolling_reflection()
                    if (
                        lt_config.every_env_step
                        and step_num > 0
                        and step_num % lt_config.every_env_step == 0
                    ):
                        maybe_trigger_rolling_reflection()
                if lt_config.supervision_event:
                    current_supervision = coordinator.long_term_supervision_count()
                    if current_supervision != _last_supervision_count:
                        _last_supervision_count = current_supervision
                        maybe_trigger_rolling_reflection()

        # Run reached terminal (finished / max_steps / a2a done / error).
        # Freeze the outcome CSVs before teardown so any in-flight coordinator
        # round that keeps executing tool calls during shutdown cannot append
        # post-terminal rows to router_interactions.csv / agent_interactions.csv
        # (the Phase 5 acceptance aggregator reads those CSVs as terminal
        # evidence).
        if hasattr(exp_logger, "freeze_terminal"):
            exp_logger.freeze_terminal()

        elapsed_total = time.time() - start_time
        # The step loop may exit on the environment boundary before observing
        # a just-finished A2A request.  Inspect the task once more so a failed
        # controller/transport request cannot be misclassified as early finish.
        if a2a_task.done() and not a2a_task.cancelled():
            a2a_exception = a2a_task.exception()
            if a2a_exception is not None:
                a2a_error = True
            else:
                result = a2a_task.result()
                if isinstance(result, str) and result.startswith("Error:"):
                    a2a_error = True
                else:
                    a2a_done = True
        if coord_task.done() and not coord_task.cancelled():
            coordinator_exception = coord_task.exception()
            if coordinator_exception is not None:
                coordinator_error = True
        final_metrics = barrier.get_metrics()
        final_metrics["elapsed_seconds"] = elapsed_total

        end_reason = classify_end_reason(
            finished=final_metrics["finished"],
            steps=final_metrics["steps"],
            max_steps=max_steps,
            elapsed_seconds=elapsed_total,
            wall_clock_limit=wall_clock_limit,
            a2a_done=a2a_done,
            a2a_error=a2a_error,
            coordinator_error=coordinator_error,
        )
        final_metrics["end_reason"] = end_reason
        exp_logger.set_end_reason(end_reason)
        final_metrics["run_id"] = run_id
        final_metrics["max_steps"] = max_steps

        # Phase 5: truth recorder — capture any step that completed on the
        # environment boundary after the last poll (drain is idempotent), then
        # freeze the evaluator-private manifest at run terminal.
        truth_recorder_result = None
        if truth_recorder is not None:
            try:
                pending_logs = (
                    barrier.drain_step_logs()
                    if hasattr(barrier, "drain_step_logs")
                    else []
                )
                for step_log in pending_logs:
                    truth_recorder.record_step(int(step_log.get("step", 0)))
                if final_metrics["steps"] >= 1:
                    truth_recorder.record_step(final_metrics["steps"])
                truth_recorder_result = _finalize_truth_recorder(
                    truth_recorder, coordinator, end_reason
                )
                if truth_recorder_result is not None and truth_manifest is None:
                    # Auto-wire the generated evaluator-private manifest into the
                    # terminal projection-quality evaluator below.
                    truth_manifest = str(truth_recorder.manifest_path)
                    truth_trace = None
            except Exception:
                logger.exception("truth recorder terminal finalize failed")

        if barrier.is_finished():
            logger.info(
                "TASK COMPLETED in %.1f seconds, %d/%d steps",
                elapsed_total,
                final_metrics["steps"],
                max_steps,
            )
        else:
            logger.warning(
                "TASK TIMEOUT after %d steps (limit %d), %.1f seconds",
                final_metrics["steps"],
                max_steps,
                elapsed_total,
            )

        # Cancel A2A orphan task if barrier finished before it
        if not a2a_task.done():
            a2a_task.cancel()
            try:
                await a2a_task
            except asyncio.CancelledError:
                pass

        # Phase 5: run-terminal exporter/evaluator wiring.  Only active when
        # canonical Memory is configured; legacy mode stays untouched (no
        # legacy retirement here).  This materializes the compatibility
        # artifacts and writes memory_acceptance.json / (optionally)
        # memory_projection_quality.json into the results dir.
        # ``run_metrics.json`` is written first so the acceptance evaluator can
        # read the non-null coverage/transport_rate (main() rewrites it after).
        metrics_file = exp_dir / "run_metrics.json"
        try:
            metrics_file.write_text(
                json.dumps(final_metrics, indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass
        terminal_memory = _invoke_run_terminal_memory_eval(
            coordinator=coordinator,
            exp_dir=exp_dir,
            memory_read_mode=memory_read_mode,
            truth_manifest=truth_manifest,
            truth_trace=truth_trace,
        )
        # Phase 4: terminal long-term reflection — drain in-flight rolling
        # reflection (join with timeout), terminal committed snapshot,
        # reflection, read-only quality artifact.  Never blocks run exit.
        terminal_long_term = _invoke_run_terminal_long_term_reflection(
            coordinator=coordinator,
            exp_dir=exp_dir,
            long_term_mode=long_term_mode,
            lt_config=lt_config,
            truth_manifest=truth_manifest,
            env=_env,
        )
        if truth_recorder_result is not None:
            terminal_memory["truth_recorder"] = truth_recorder_result
        final_metrics["memory_terminal"] = terminal_memory
        final_metrics["long_term_reflection"] = terminal_long_term

        return final_metrics

    finally:
        # Stop the environment first so any worker blocked in a barrier tool wakes up.
        # Each teardown step is guarded: shutdown must always complete (final
        # run_metrics / summary.csv writes) even if one step raises.
        logger.info("Shutting down barrier...")
        try:
            barrier.stop()
        except Exception:
            logger.exception("barrier.stop() failed during teardown")

        logger.info("Shutting down workers...")
        for worker in workers.values():
            try:
                worker.stop()
            except Exception:
                logger.exception("worker.stop() failed during teardown")
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            try:
                await coordinator.stop()
            except Exception:
                logger.exception("coordinator.stop() failed during teardown")

        try:
            logger.info("Clearing agent sessions...")
            for worker in workers.values():
                worker.clear_sessions()
            if coordinator is not None:
                coordinator.clear_sessions()
        except Exception:
            logger.exception("agent session clearing failed during teardown")

        try:
            exp_logger.close()
        except Exception:
            logger.exception("exp_logger.close() failed during teardown")
        try:
            final_metrics["log_dir"] = exp_logger.get_log_dir()
            logger.info("Experiment logs saved to: %s", exp_logger.get_log_dir())
        except Exception:
            logger.exception("final metrics log_dir backfill failed during teardown")
        logger.info("Cleanup complete")


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument("--agents", type=int, default=2, help="Number of agents (1-6)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")

    # Load .env FIRST so its values become CLI defaults (CLI args still take precedence)
    _env = load_env_file(str(Path(__file__).parent.parent / ".env"))
    parser.add_argument(
        "--model",
        type=str,
        default=_env.get("model", "deepseek-v4-flash"),
        help="LLM model",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=_env.get("provider", "openai"),
        help="LLM provider",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=_env.get("api_base", "https://api.deepseek.com"),
        help="API base URL",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Max environment steps (default: {PAPER_MAX_STEPS}, the paper's L cap)",
    )
    parser.add_argument(
        "--coordinator-port",
        type=int,
        default=8080,
        help="Coordinator server port (default: 8080)",
    )
    parser.add_argument(
        "--agent-base-port",
        type=int,
        default=8191,
        help="Base port for agent A2A servers (default: 8191)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Explicit log directory (default: auto-generated timestamp dir)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="semantic",
        choices=["semantic"],
        help="Coordinator state source mode (default: semantic)",
    )
    parser.add_argument(
        "--sandbox-profile",
        type=str,
        default="workspace",
        choices=["off", "workspace"],
        help="Sandbox profile: off|workspace (default: workspace)",
    )
    parser.add_argument(
        "--coordinator-prompt",
        type=str,
        default=None,
        help="Override the initial task prompt sent to the coordinator (for targeted testing)",
    )
    parser.add_argument(
        "--enable-peer-mail",
        action="store_true",
        default=False,
        help="Enable signed envelope peer messaging (Phase 4)",
    )
    parser.add_argument(
        "--wall-clock-limit",
        type=float,
        default=3600.0,
        help="Wall-clock time limit in seconds (default: 3600; 0 = unlimited)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="LLM sampling temperature (default: 0.7, unchanged from the "
        "previously hardcoded value)",
    )
    parser.add_argument(
        "--llm-seed",
        type=int,
        default=None,
        help="LLM sampling seed for reproducible runs. Distinct from --seed, "
        "which seeds the scene/environment. Only sent to providers that "
        "support it (the Anthropic Messages API rejects it).",
    )
    parser.add_argument(
        "--prompt-dir",
        type=str,
        default=_DEFAULT_PROMPT_DIR,
        help="Root dir containing coordinator/ and worker/ system prompt "
        f"subdirectories (default: {_DEFAULT_PROMPT_DIR}). This is the "
        "phase-5 self-evolution hook for injecting a candidate prompt set.",
    )
    parser.add_argument(
        "--skills-dir",
        type=str,
        default=_DEFAULT_SKILLS_DIR,
        help="Root dir containing coordinator/ and worker/ skill "
        f"subdirectories (default: {_DEFAULT_SKILLS_DIR}). Independent of "
        "--prompt-dir -- previously this was always derived from "
        "prompts_dir and could not be set separately.",
    )
    parser.add_argument(
        "--prompt-tag",
        type=str,
        default="unlabeled",
        help="Human-readable label recorded as metadata['prompt_version'] "
        "(default: 'unlabeled'). Cosmetic only -- metadata['prompt_hash'] "
        "is the content identity used for reproducibility.",
    )
    parser.add_argument(
        "--memory-read-mode",
        type=str,
        default="read_port",
        choices=["legacy", "shadow", "read_port"],
        help="Memory mode: legacy|shadow|read_port (default read_port since "
        "H3 retirement; legacy retained as rollback target)",
    )
    parser.add_argument(
        "--long-term-mode",
        type=str,
        default="off",
        choices=["off", "shadow", "read"],
        help="Run-local long-term memory mode (default off). shadow/read "
        "persist published reflection products from committed snapshots but "
        "never inject them into Context (P5); Phase 4 performs no real model "
        "calls (reflection_* keys in .env gate the model port)",
    )
    parser.add_argument(
        "--truth-manifest",
        type=str,
        default=None,
        help="Evaluator-private truth manifest path; when provided, the "
        "terminal-only memory_projection_quality evaluator runs after the run "
        "(requires canonical Memory mode shadow/read_port)",
    )
    parser.add_argument(
        "--truth-trace",
        type=str,
        default=None,
        help="Optional override of the truth trace path declared in "
        "--truth-manifest",
    )
    parser.add_argument(
        "--truth-output-dir",
        type=str,
        default=None,
        help="Evaluator-private directory for the Phase 5 truth recorder "
        "(per-step truth_trace.jsonl + terminal truth_manifest.json). When "
        "set, the generated manifest is wired into the terminal "
        "memory_projection_quality evaluator unless --truth-manifest is "
        "given. Must be outside the run results dir (evaluator-private "
        "boundary).",
    )
    args = parser.parse_args()

    metrics = asyncio.run(
        run_experiment(
            scene=args.scene,
            num_agents=args.agents,
            seed=args.seed,
            model=args.model,
            provider=args.provider,
            api_base=args.api_base,
            max_steps=args.max_steps,
            coordinator_port=args.coordinator_port,
            agent_base_port=args.agent_base_port,
            log_dir=args.log_dir,
            sandbox_profile=args.sandbox_profile,
            state_mode=args.mode,
            coordinator_prompt=args.coordinator_prompt,
            enable_peer_mail=args.enable_peer_mail,
            wall_clock_limit=args.wall_clock_limit,
            temperature=args.temperature,
            llm_seed=args.llm_seed,
            prompt_dir=args.prompt_dir,
            skills_dir=args.skills_dir,
            prompt_tag=args.prompt_tag,
            memory_read_mode=args.memory_read_mode,
            long_term_mode=args.long_term_mode,
            truth_manifest=args.truth_manifest,
            truth_trace=args.truth_trace,
            truth_output_dir=args.truth_output_dir,
        )
    )

    # Write metrics to JSON for subprocess caller
    log_dir = metrics.get("log_dir", "")
    if log_dir:
        metrics_file = os.path.join(log_dir, "run_metrics.json")
        with open(metrics_file, "w") as f:
            json.dump(metrics, f, indent=2, default=str)

    print(json.dumps(metrics, default=str))


if __name__ == "__main__":
    main()

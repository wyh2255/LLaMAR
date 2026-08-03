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

from Agent.sandbox import SandboxPolicy
from a2a.shared.env_loader import load_env_file
from sar_orch.barrier import SARBarrier
from sar_orch.logger import ExperimentLogger
from sar_orch.worker import SARWorker
from sar_orch.coordinator import SARCoordinator

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

    logger.info(
        "Experiment logs unified under: %s", exp_dir
    )

    # 3. Create experiment logger
    exp_logger = ExperimentLogger(experiment_name="sar_experiment", log_dir=str(exp_dir))

    run_id = f"sar-scene{scene}-agents{num_agents}-seed{seed}-{uuid.uuid4().hex[:8]}"
    if wall_clock_limit <= 0:
        wall_clock_limit = float("inf")  # 0 表示无墙钟时间上限
    git_dirty = _is_git_dirty()
    code_commit = _get_git_commit(dirty=git_dirty)
    prompt_hash = compute_prompt_hash(prompt_dir, skills_dir)
    exp_logger.set_run_context(run_id=run_id, model=model, prompt_version=prompt_tag)
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
        )

        logger.info("SARCoordinator starting on port %d", coordinator_port)
        coord_task = asyncio.create_task(coordinator.start())

        # Wait for coordinator to bind ports
        await asyncio.sleep(3.0)

        # Redirect semantic_map.jsonl to the top-level experiment directory
        if coordinator._semantic_map is not None:
            coordinator._semantic_map.set_jsonl_path(str(exp_dir / "semantic_map.jsonl"))
            logger.info("semantic_map.jsonl path set to: %s", exp_dir / "semantic_map.jsonl")

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
                barrier.drain_step_logs()
                if hasattr(barrier, "drain_step_logs")
                else []
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
                        if coordinator is not None and coordinator._semantic_map is not None
                        else 0.0
                    ),
                    freshness=(
                        coordinator._semantic_map.freshness()
                        if coordinator is not None and coordinator._semantic_map is not None
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
                _last_step_logged = step_num

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

        return final_metrics

    finally:
        # Stop the environment first so any worker blocked in a barrier tool wakes up.
        logger.info("Shutting down barrier...")
        barrier.stop()

        logger.info("Shutting down workers...")
        for worker in workers.values():
            worker.stop()
        if coordinator is not None:
            logger.info("Shutting down coordinator...")
            await coordinator.stop()

        logger.info("Clearing agent sessions...")
        for worker in workers.values():
            worker.clear_sessions()
        if coordinator is not None:
            coordinator.clear_sessions()

        exp_logger.close()
        final_metrics["log_dir"] = exp_logger.get_log_dir()
        logger.info("Experiment logs saved to: %s", exp_logger.get_log_dir())
        logger.info("Cleanup complete")


def main():
    parser = argparse.ArgumentParser(description="SAR Experiment")
    parser.add_argument("--scene", type=int, default=1, help="SAR scene number (1-5)")
    parser.add_argument("--agents", type=int, default=2, help="Number of agents (1-6)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")

    # Load .env FIRST so its values become CLI defaults (CLI args still take precedence)
    _env = load_env_file(str(Path(__file__).parent.parent / ".env"))
    parser.add_argument(
        "--model", type=str, default=_env.get("model", "deepseek-v4-flash"), help="LLM model"
    )
    parser.add_argument(
        "--provider", type=str, default=_env.get("provider", "openai"), help="LLM provider"
    )
    parser.add_argument(
        "--api-base", type=str, default=_env.get("api_base", "https://api.deepseek.com"), help="API base URL"
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

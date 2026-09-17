"""The ``reef-sar`` runner: one SAR simulation as one reef gate episode.

Reef launches ``reef-sar <task JSON>`` per episode with the rendered tree root
in ``REEF_SAR_TREE`` (descriptor.yaml's ``argv``/``env``). The chain, in order:

    materialize    ``git archive $LLAMAR_REF | tar -x`` into ``workspace/repo``
                   - a private checkout of one frozen ref, so a candidate and
                   the current tree differ only in the rendered overlay
    overlay        copy ``overlay/prompts/<domain>/<name>.md`` over the
                   checkout's ``sar_orch/prompts/<domain>/<name>.md`` and append
                   ``overlay/rules.md`` (behind a marker naming its source) to
                   the coordinator's ``system.semantic.md`` - unless it is the
                   baseline placeholder, which appends nothing (see
                   ``RULES_PLACEHOLDER``)
    config         read ``overlay/sar_config.json``: the model binding reef
                   rendered in, plus the whitelisted run parameters
    ports          pick a free coordinator/agent-base port block (concurrent
                   episodes must not collide)
    run            ``uv run --extra sar python sar_orch/experiment.py`` with
                   cwd ``workspace/repo``
    collect        run the sar-metrics collector, land the run's gradeable
                   artifacts in ``sar/out`` (the trajectory directory reef
                   copies into the step record), and print one compact metrics
                   JSON object as the last stdout line

Contracts this runner owns (implementation workflow §A.2):

- **The task JSON is authoritative.** ``scene``/``agents``/``seed`` come from
  argv; so does ``max_steps`` when the task carries one, which every task the
  recipe serves does. ``sar_config.json`` contributes only the behavior keys
  the runner whitelists and explicitly translates (``CONFIG_RUN_KEYS``), and a
  key it pins or does not know is warned about on stderr and ignored. A config
  mutation can therefore never change which task the candidate is graded on,
  nor buy an episode more steps than its paired baseline episode gets.
- **Truth stays inside the episode root.** ``--truth-output-dir`` is
  ``{tree}/workspace/truth``, and ``sar/out`` never holds a truth file - the
  collect step refuses to finish if one appears there.
- **Everything lands under ``workspace/`` or the whitelisted ``sar/``**, so the
  episode's residue audit stays clean; the HOME-scoped caches a run writes
  (matplotlib, XDG) are pinned into ``workspace/cache`` for the same reason.
- **The binding key never reaches an artifact or a stream.** The episode's env
  carries it (that is how the framework authenticates); the config snapshot
  kept under ``sar/out`` holds it redacted, and this module writes it to
  neither stdout nor stderr (the dry run reports ``api_key_present`` instead).

The episode's own stdout is kept in ``workspace/logs/experiment.out.log`` (and
stderr beside it) instead of being passed through, because stdout's last line
is reserved for the metrics object the step record reads.

Exit codes: 0 the experiment exited 0; 1 ``--dry-run`` found the rendered tree
incomplete; 2 usage, task, or environment error (``USAGE_EXIT``); 3 the episode
chain failed before the experiment could run (``CHAIN_FAILURE_EXIT``, nothing
gradeable was produced). The experiment's own exit code is forwarded as-is when
it ran - reef records it alongside the score (``backend.py:155-159``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

__all__ = [
    "CHAIN_FAILURE_EXIT",
    "INCOMPLETE_TREE_EXIT",
    "RULES_PLACEHOLDER",
    "USAGE_EXIT",
    "ChainError",
    "Episode",
    "allocate_ports",
    "apply_overlay",
    "build_plan",
    "collect_artifacts",
    "harness_base_url",
    "main",
    "materialize",
    "metrics_line",
    "parse_task",
    "preflight",
    "translate_config",
]

#: The plan is printable but the rendered tree is incomplete (dry run).
INCOMPLETE_TREE_EXIT = 1
#: Usage, task, or environment error.
USAGE_EXIT = 2
#: The chain failed before the experiment could run.
CHAIN_FAILURE_EXIT = 3

#: Environment the descriptor guarantees (descriptor.yaml's env section).
REQUIRED_ENV = ("REEF_SAR_TREE", "LLAMAR_REPO")

#: Task fields every episode carries.
REQUIRED_TASK_KEYS = ("scene", "agents", "seed")
#: Optional task fields.
OPTIONAL_TASK_KEYS = ("max_steps",)

#: Overlay files a complete render leaves for the runner (single-file kinds;
#: the two prompt directories are globbed).
OVERLAY_FILES = {
    "config": "sar_config.json",
    "rules": "rules.md",
}
OVERLAY_PROMPT_DIRS = {
    "coordinator_prompts": "prompts/coordinator",
    "worker_prompts": "prompts/worker",
}

#: Where the two rendered prompt directories land in the checkout, and the file
#: the rules text is appended to.
PROMPT_TARGETS = {
    "coordinator": "sar_orch/prompts/coordinator",
    "worker": "sar_orch/prompts/worker",
}
RULES_TARGET = "sar_orch/prompts/coordinator/system.semantic.md"
RULES_HEADER = "\n\n<!-- reef-sar: appended from the rendered tree's overlay/rules.md -->\n\n"

#: The text the seed gives the ``base-rules`` entry, and the one rules body that
#: is not appended. reef's tree refuses a rules node whose text is empty
#: (``harness/tree/nodes.py`` ``rules_node`` -> ``_require_text``), so the seed
#: cannot carry nothing; carrying this placeholder instead keeps the baseline
#: byte-identical to the checkout - no rules file, no marker, no prompt drift -
#: while the entry stays a real mutation anchor. The first proposal that writes
#: actual rules replaces it, and its text is appended like any other.
RULES_PLACEHOLDER = "<!-- reef-sar: baseline placeholder; no rules text is appended -->"

#: The rendered config file name inside the overlay, and the keys the runner
#: reads out of it. Everything the runner does not translate or pin is warned
#: about and ignored: a config mutation reaches the run only through this table.
CONFIG_FILE = "sar_config.json"
CONFIG_RUN_KEYS: Mapping[str, str] = {
    "max_steps": "--max-steps",
    "prune_policy": "--prune-policy",
}
PRUNE_POLICIES = ("count_window", "prefix_stable")
PINNED_CONFIG_KEYS: Mapping[str, str] = {
    "scene": "the task JSON decides the scene",
    "agents": "the task JSON decides the agent count",
    "seed": "the task JSON decides the seed",
    "mode": "the gate always runs in semantic mode",
    "model": "the model under test is the reef model binding (llm.model)",
    "provider": "the runner pins the openai dialect",
    "sandbox_profile": "the runner pins the workspace sandbox profile",
    "memory_read_mode": "the runner pins the run's memory measurement setup",
    "long_term_mode": "the runner pins the run's long-term measurement setup",
}

#: The experiment invocation. ``--extra sar`` is not decoration: the base
#: dependency set does not carry matplotlib, which ``SAR/utils.py`` imports, so
#: a bare ``uv run`` cannot even start the experiment (probe: ModuleNotFoundError).
UV_EXTRA = "sar"
MODE = "semantic"
PROVIDER = "openai"
EXPERIMENT_SCRIPT = "sar_orch/experiment.py"
EVAL_SCRIPT = "sar_orch/eval/run_eval_metrics.py"

#: Episode-local paths, relative to the rendered tree root.
REPO_DIR = "workspace/repo"
RUN_DIR = "workspace/run"
TRUTH_DIR = "workspace/truth"
LOGS_DIR = "workspace/logs"
CACHE_DIR = "workspace/cache"
TRAJECTORY_DIR = "sar/out"

#: Files the collect step lands in the trajectory directory. ``run_meta.json``
#: (the episode identity the trajectory reader reads) and ``task.json`` (the
#: task as reef sent it) ride along so the artifact is self-describing.
COLLECT_FILES = ("run_metrics.json", "eval_metrics.json", "summary.csv")
RUN_META_FILE = "run_meta.json"
TASK_FILE = "task.json"
CONFIG_SNAPSHOT = "sar_config.json"
#: Evaluator-private truth artifacts: they live under ``workspace/truth`` and
#: must never appear in the trajectory directory.
TRUTH_ARTIFACTS = ("truth_trace.jsonl", "truth_manifest.json")

#: Port scan window and stride, the benchmark's layout (``benchmark.py:46-48``).
PORT_SCAN_START = 50000
PORT_SCAN_END = 59999
PORT_STRIDE = 10
PORT_HOST = "localhost"

#: tarfile's extraction filter where the interpreter has one (Python 3.12+).
_EXTRACT_FILTER = "data" if sys.version_info >= (3, 12) else None


class ChainError(RuntimeError):
    """Something the episode needs is missing or unusable.

    ``code`` is the exit code ``main`` returns for it: ``USAGE_EXIT`` for a
    broken task or environment, ``CHAIN_FAILURE_EXIT`` for a chain step that
    could not be carried out.
    """

    def __init__(self, message: str, *, code: int = CHAIN_FAILURE_EXIT) -> None:
        super().__init__(message)
        self.code = code


def _say(message: str) -> None:
    """A note on stderr: stdout carries the metrics object and nothing else."""
    print(f"reef-sar: {message}", file=sys.stderr)


# -- task and plan ------------------------------------------------------------


def parse_task(text: str) -> dict[str, Any]:
    """The episode's task JSON: an object with integer scene/agents/seed and optional max_steps.

    Raises ValueError with the reason; ``main`` turns that into USAGE_EXIT.
    Extra keys are kept: the plan echoes the whole task.
    """
    try:
        task = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"task is not valid JSON: {exc}") from exc
    if not isinstance(task, dict):
        # The task arrives as text: a wrong shape is invalid input, and main
        # maps ValueError to the usage exit code.
        raise ValueError(  # noqa: TRY004
            f"task must be a JSON object, got {type(task).__name__}"
        )
    for key in REQUIRED_TASK_KEYS:
        if key not in task:
            raise ValueError(f"task is missing the required field {key!r}")
        if isinstance(task[key], bool) or not isinstance(task[key], int) or task[key] < 0:
            raise ValueError(f"task field {key!r} must be a non-negative integer, got {task[key]!r}")
    for key in OPTIONAL_TASK_KEYS:
        value = task.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"task field {key!r} must be a positive integer, got {value!r}")
    return task


@dataclass(frozen=True)
class Translation:
    """What the rendered harness config contributes to one episode.

    ``flags`` are the CLI flags the whitelist produced, ``binding`` the model
    endpoint reef injected (``base_url``/``api_key``/``model``), and
    ``warnings`` the lines a person reads to learn why a config key changed
    nothing about the run.
    """

    flags: dict[str, Any] = field(default_factory=dict)
    binding: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def read_config(overlay: Path) -> dict[str, Any]:
    """The rendered harness config; ``{}`` when the tree carries none.

    A present file that cannot be read or decoded raises ``ChainError``: an
    unreadable config is not an absent one.
    """
    path = overlay / CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ChainError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChainError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ChainError(f"{path} must hold a JSON object, got {type(data).__name__}")
    return data


def translate_config(data: Mapping[str, Any], task: Mapping[str, Any]) -> Translation:
    """Turn the rendered config into run flags, warning about every key it drops.

    Only the whitelist in :data:`CONFIG_RUN_KEYS` is translated. The step budget
    is the task JSON's when the task carries one (every task the recipe serves
    does), so a config ``max_steps`` can never buy an episode more steps than
    the paired baseline episode gets; the config value counts only for a task
    that carries none. Keys the runner pins (:data:`PINNED_CONFIG_KEYS`) and
    keys it does not know are reported and ignored - never silently applied.
    """
    flags: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in data.items():
        if key == "llm":
            continue  # the model binding; read separately
        if key == "max_steps":
            if task.get("max_steps") is not None:
                warnings.append(
                    f"config max_steps={value!r} ignored: the task JSON sets max_steps="
                    f"{task['max_steps']!r} and owns the episode's step budget"
                )
            elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
                flags["max_steps"] = value
            else:
                warnings.append(f"config max_steps={value!r} is not a positive integer; ignored")
        elif key == "prune_policy":
            if value in PRUNE_POLICIES:
                flags["prune_policy"] = value
            else:
                warnings.append(f"config prune_policy={value!r} is not one of {list(PRUNE_POLICIES)}; ignored")
        elif key in PINNED_CONFIG_KEYS:
            warnings.append(f"config key {key!r} ignored: {PINNED_CONFIG_KEYS[key]}")
        else:
            warnings.append(
                f"unknown config key {key!r}; ignored (the runner translates only {sorted(CONFIG_RUN_KEYS)})"
            )
    binding, binding_warnings = _binding(data)
    return Translation(flags=flags, binding=binding, warnings=(*warnings, *binding_warnings))


def _binding(data: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """The model endpoint reef rendered into the config, and what is missing from it."""
    llm = data.get("llm")
    binding: dict[str, str] = {}
    if isinstance(llm, Mapping):
        for key in ("base_url", "api_key", "model"):
            value = llm.get(key)
            if isinstance(value, str) and value.strip():
                binding[key] = value.strip()
    missing = [key for key in ("base_url", "api_key", "model") if key not in binding]
    if missing:
        return binding, [
            "the rendered config carries no usable llm binding ({} missing); a real run cannot "
            "start without it".format(", ".join(missing))
        ]
    return binding, []


def harness_base_url(base_url: str) -> str:
    """The OpenAI-SDK-style base the experiment needs, restored from reef's dialect.

    Reef's binding contract carries no ``/v1`` (``model_binding.py:61``: "``base_url``
    carries no ``/v1`` suffix; the request paths add it") because reef appends
    ``/v1/chat/completions`` itself. The experiment hands ``--api-base`` to
    ``AsyncOpenAI(base_url=...)``, and the SDK appends only ``/chat/completions``,
    so an endpoint that serves the OpenAI paths under ``/v1`` (cf.api.fan, a vLLM
    server) answers ``404 not found`` without the prefix. The runner restores the
    prefix reef's dialect drops - once, and only when it is missing, so a rendered
    config that already carries it (the LLaMAR ``.env`` convention) stays untouched.
    """
    stripped = base_url.strip().rstrip("/")
    if not stripped:
        return ""
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def resolve_ref(repo: str, environ: Mapping[str, str]) -> tuple[str, str]:
    """``(ref, source)``: ``LLAMAR_REF``, else the checkout's HEAD with a warning.

    The ref pins the code baseline: candidate and current episodes of one
    campaign must materialize the same commit, so the frozen variable is what a
    campaign sets and the HEAD fallback only serves a manual run.
    """
    ref = (environ.get("LLAMAR_REF") or "").strip()
    if ref:
        source = "LLAMAR_REF"
    else:
        ref = _git_head(repo)
        source = "HEAD (LLAMAR_REF unset)"
        _say(
            "LLAMAR_REF is unset; falling back to "
            f"{repo}'s HEAD ({ref[:12]}). A campaign must freeze one ref for every episode - "
            "candidate and current are only comparable on the same code baseline."
        )
    _verify_ref(repo, ref)
    return ref, source


def _git_head(repo: str) -> str:
    """The checkout's HEAD commit, or ChainError (an environment error)."""
    result = _git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise ChainError(
            f"cannot read HEAD from {repo}: {result.stderr.strip() or 'git rev-parse failed'}",
            code=USAGE_EXIT,
        )
    return result.stdout.strip()


def _verify_ref(repo: str, ref: str) -> None:
    """Fail closed when ``ref`` does not resolve in ``repo`` (an environment error)."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        raise ChainError(f"LLAMAR_REF={ref!r} does not resolve to a commit in {repo}", code=USAGE_EXIT)


def _git(repo: str, *argument: str) -> subprocess.CompletedProcess[str]:
    """One git call in ``repo``; the caller decides what a failure means."""
    try:
        return subprocess.run(
            ["git", "-C", repo, *argument],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ChainError("git is not on PATH; the runner materializes its checkout with git", code=USAGE_EXIT) from exc
    except subprocess.TimeoutExpired as exc:
        raise ChainError(f"git {' '.join(argument)} timed out after 60s") from exc


@dataclass(frozen=True)
class Episode:
    """One episode's resolved inputs, paths, and the commands that will run."""

    task: dict[str, Any]
    tree: Path
    overlay: Path
    repo: str
    ref: str
    ref_source: str
    repo_dir: Path
    run_dir: Path
    truth_dir: Path
    out_dir: Path
    logs_dir: Path
    max_steps: int | None
    max_steps_source: str
    flags: dict[str, Any]
    binding: dict[str, str]
    warnings: tuple[str, ...]
    overlay_found: tuple[str, ...]
    overlay_missing: tuple[str, ...]
    coordinator_port: int
    agent_base_port: int

    def experiment_command(self) -> list[str]:
        """The experiment invocation (ports and flags as planned)."""
        command = [
            "uv",
            "run",
            "--extra",
            UV_EXTRA,
            "python",
            EXPERIMENT_SCRIPT,
            "--mode",
            MODE,
            "--provider",
            PROVIDER,
            "--scene",
            str(self.task["scene"]),
            "--agents",
            str(self.task["agents"]),
            "--seed",
            str(self.task["seed"]),
            "--coordinator-port",
            str(self.coordinator_port),
            "--agent-base-port",
            str(self.agent_base_port),
            "--log-dir",
            str(self.run_dir),
            "--truth-output-dir",
            str(self.truth_dir),
            "--api-base",
            harness_base_url(self.binding.get("base_url", "")),
            "--model",
            self.binding.get("model", ""),
        ]
        if self.max_steps is not None:
            command += ["--max-steps", str(self.max_steps)]
        if "prune_policy" in self.flags:
            command += ["--prune-policy", str(self.flags["prune_policy"])]
        return command

    def eval_command(self) -> list[str]:
        """The sar-metrics collector invocation over the finished run."""
        return [
            "uv",
            "run",
            "--extra",
            UV_EXTRA,
            "python",
            EVAL_SCRIPT,
            "--results-dir",
            str(self.run_dir),
        ]

    def plan(self) -> dict[str, Any]:
        """The episode plan as printable data: paths, decisions, commands."""
        return {
            "runner": f"reef-sar {__version__}",
            "task": dict(self.task),
            "max_steps": self.max_steps,
            "max_steps_source": self.max_steps_source,
            "tree": str(self.tree),
            "overlay": str(self.overlay),
            "llamar_repo": self.repo,
            "llamar_ref": self.ref,
            "llamar_ref_source": self.ref_source,
            "paths": {
                "repo_dir": str(self.repo_dir),
                "run_dir": str(self.run_dir),
                "truth_dir": str(self.truth_dir),
                "trajectory_dir": str(self.out_dir),
                "logs_dir": str(self.logs_dir),
            },
            "overlay_files": {"found": list(self.overlay_found), "missing": list(self.overlay_missing)},
            "config": {
                "flags": dict(self.flags),
                "binding": {
                    "base_url": self.binding.get("base_url", ""),
                    "experiment_api_base": harness_base_url(self.binding.get("base_url", "")),
                    "model": self.binding.get("model", ""),
                    "api_key_present": "api_key" in self.binding,
                },
                "warnings": list(self.warnings),
            },
            "ports": {"coordinator_port": self.coordinator_port, "agent_base_port": self.agent_base_port},
            "commands": {"experiment": self.experiment_command(), "eval": self.eval_command()},
            "artifacts": {
                "collected": [
                    *COLLECT_FILES,
                    RUN_META_FILE,
                    TASK_FILE,
                    CONFIG_SNAPSHOT,
                ],
                "truth_dir": str(self.truth_dir),
            },
        }


def build_plan(task: Mapping[str, Any], environ: Mapping[str, str]) -> Episode:
    """Resolve the episode's inputs: ref, paths, overlay inventory, config, ports.

    Raises ``ChainError`` (usage exit) for a missing environment variable or an
    unusable ref. The ports are allocated here for the plan and allocated again
    right before the launch, so the window between "free" and "bound" stays the
    few milliseconds of the ``subprocess`` call.
    """
    absent = [name for name in REQUIRED_ENV if not environ.get(name)]
    if absent:
        raise ChainError(f"missing required environment: {', '.join(absent)}", code=USAGE_EXIT)
    tree = Path(environ["REEF_SAR_TREE"])
    overlay = Path(environ.get("REEF_SAR_OVERLAY") or tree / "overlay")
    repo = environ["LLAMAR_REPO"]
    ref, ref_source = resolve_ref(repo, environ)
    found = sorted(label for label, relative in OVERLAY_FILES.items() if (overlay / relative).is_file())
    missing = sorted(label for label in OVERLAY_FILES if label not in found)
    for label, relative in OVERLAY_PROMPT_DIRS.items():
        (found if list(overlay.glob(f"{relative}/*.md")) else missing).append(label)
    found.sort()
    missing.sort()
    translation = translate_config(read_config(overlay), task)
    task_max_steps = task.get("max_steps")
    if task_max_steps is not None:
        max_steps, max_steps_source = task_max_steps, "task JSON"
    elif "max_steps" in translation.flags:
        max_steps, max_steps_source = translation.flags["max_steps"], "config"
    else:
        max_steps, max_steps_source = None, "scene default"
    coordinator_port, agent_base_port = allocate_ports(int(task["agents"]), spread_seed=str(tree))
    return Episode(
        task=dict(task),
        tree=tree,
        overlay=overlay,
        repo=repo,
        ref=ref,
        ref_source=ref_source,
        repo_dir=tree / REPO_DIR,
        run_dir=tree / RUN_DIR,
        truth_dir=tree / TRUTH_DIR,
        out_dir=tree / TRAJECTORY_DIR,
        logs_dir=tree / LOGS_DIR,
        max_steps=max_steps,
        max_steps_source=max_steps_source,
        flags=dict(translation.flags),
        binding=dict(translation.binding),
        warnings=translation.warnings,
        overlay_found=tuple(found),
        overlay_missing=tuple(missing),
        coordinator_port=coordinator_port,
        agent_base_port=agent_base_port,
    )


# -- chain steps --------------------------------------------------------------


def materialize(repo: str, ref: str, destination: Path) -> int:
    """``git archive ref | tar -x`` into ``destination``; returns the file count.

    The archive is streamed straight into the extractor, so nothing lands in a
    temporary file, and every member is checked to stay inside ``destination``.
    A pre-existing destination is cleared first: the checkout must be exactly
    ``ref``, not ``ref`` extracted over a previous episode's leftovers. A
    failure leaves no half-extracted checkout behind either, and the error
    carries git's own stderr.
    """
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["git", "-C", repo, "archive", "--format=tar", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None  # PIPE
    count = 0
    failure: Exception | None = None
    try:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                count = _extract(archive, destination)
        except (tarfile.TarError, OSError) as exc:
            failure = exc
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    code = process.wait()
    if code != 0 or failure is not None:
        shutil.rmtree(destination, ignore_errors=True)
        detail = stderr.strip()
        if not detail and failure is not None:
            detail = f"archive unreadable ({failure})"
        raise ChainError(f"git archive {ref!r} failed in {repo} (exit {code}): {detail}")
    return count


def _extract(archive: tarfile.TarFile, destination: Path) -> int:
    """Extract every regular member of a streamed archive; returns the file count."""
    count = 0
    for member in archive:
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ChainError(f"archive member {member.name!r} escapes the checkout")
        if member.isdir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
            continue
        if _EXTRACT_FILTER is not None:
            archive.extract(member, destination, filter=_EXTRACT_FILTER)
        else:  # pragma: no cover - Python < 3.12
            # The member's path was checked above; the older interpreter has no filter argument.
            archive.extract(member, destination)
        count += 1
    return count


def apply_overlay(overlay: Path, repo_dir: Path) -> tuple[list[str], list[str]]:
    """Apply the rendered composition to the checkout; returns (written, notes).

    The render produces exactly three kinds of file, and each lands its own way:

    - ``sar_config.json`` is the runner's input, not checkout content;
    - ``rules.md`` is appended to the coordinator's system prompt behind a
      marker naming its source (an empty rules file, or the seed's baseline
      placeholder :data:`RULES_PLACEHOLDER`, appends nothing: the seed keeps
      the harness behavior-identical);
    - ``prompts/<domain>/<name>.md`` overwrites
      ``sar_orch/prompts/<domain>/<name>.md``.

    Anything else is a note: the harness has no target for it, and a file
    written to a guessed path would be a silent no-op or a stray edit. Entry
    kinds the checkout does not carry stay as committed.
    """
    written: list[str] = []
    notes: list[str] = []
    for source in sorted(overlay.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(overlay).as_posix()
        if relative == CONFIG_FILE:
            continue
        if relative == "rules.md":
            text = source.read_text(encoding="utf-8")
            if not text.strip():
                notes.append("rules: overlay/rules.md is empty; the coordinator prompt keeps the checkout's text")
            elif text.strip() == RULES_PLACEHOLDER:
                notes.append(
                    "rules: overlay/rules.md carries the baseline placeholder; the coordinator prompt keeps "
                    "the checkout's text"
                )
            else:
                target = repo_dir / RULES_TARGET
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(RULES_HEADER + text.strip() + "\n")
                notes.append(f"rules: appended overlay/rules.md to {RULES_TARGET}")
                if RULES_TARGET not in written:
                    written.append(RULES_TARGET)
            continue
        target = _overlay_target(relative)
        if target is None:
            notes.append(f"overlay entry {relative!r} has no target in the harness; ignored")
            continue
        destination = repo_dir / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if target not in written:
            written.append(target)
    return written, notes


def _overlay_target(relative: str) -> str | None:
    """The checkout path an overlay file overwrites, or None when it has none."""
    parts = PurePosixPath(relative).parts
    if len(parts) == 3 and parts[0] == "prompts" and parts[1] in PROMPT_TARGETS and parts[2].endswith(".md"):
        return f"{PROMPT_TARGETS[parts[1]]}/{parts[2]}"
    return None


def preflight() -> dict[str, Any]:
    """The tooling the chain needs, checked before its first expensive step.

    ``git`` materializes the checkout; ``uv`` starts the experiment inside it,
    and the descriptor pins ``UV_CACHE_DIR`` because episode/run.py relocates
    HOME into the episode root - with a warm cache a fresh per-episode venv is
    seconds, with a cold one every dependency is re-downloaded per episode
    (8-40 times per gate step). The dry run prints this section so the operator
    sees the state of both; a real run stops here when either is missing.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise ChainError(
            "uv is not on PATH; the runner starts the experiment with `uv run` (install uv or put it "
            "on the episode's PATH)",
            code=USAGE_EXIT,
        )
    git = shutil.which("git")
    if git is None:
        raise ChainError("git is not on PATH; the runner materializes its checkout with git", code=USAGE_EXIT)
    cache = os.environ.get("UV_CACHE_DIR", "")
    version = ""
    try:
        version = subprocess.run(
            [uv, "--version"], capture_output=True, text=True, check=False, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):  # a broken uv reports itself as unusable below
        version = ""
    return {
        "uv": uv,
        "uv_version": version,
        "uv_cache_dir": cache,
        "uv_cache_warm": bool(cache) and Path(cache).is_dir(),
        "git": git,
    }


def _scan_slots(spread_seed: str) -> list[int]:
    """The block bases to try, in order: from the start, or rotated by ``spread_seed``.

    Every episode used to scan from :data:`PORT_SCAN_START`, and the probe holds
    nothing - ``uv run`` takes seconds between the probe and the experiment's
    bind - so two episodes launched at the same moment both took block 50000 and
    the loser died on the bind (steps=0, `framework_error`, in ~2 s). A
    per-episode seed rotates the starting slot, so concurrent episodes land in
    different corners of the window; an empty seed keeps the plain scan for
    manual runs and tests.
    """
    bases = list(range(PORT_SCAN_START, PORT_SCAN_END - PORT_STRIDE + 1, PORT_STRIDE))
    if spread_seed:
        digest = hashlib.sha256(spread_seed.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:8], "big") % len(bases)
        bases = bases[slot:] + bases[:slot]
    return bases


def allocate_ports(agents: int, *, spread_seed: str = "") -> tuple[int, int]:
    """A free ``(coordinator_port, agent_base_port)`` block for one episode.

    One run binds ``coordinator_port``, ``coordinator_port + 1`` (the
    coordinator's own A2A port, ``experiment.py:857``) and one port per agent
    from the base up, so the probe checks the whole block. The scan mirrors the
    benchmark's layout and stride (``benchmark.py:46-48,131-156``); the ports are
    only held for the instant of the probe, so the caller re-allocates right
    before the launch rather than trusting a stale block - and passes a
    per-episode ``spread_seed`` (the episode root) so two launches at the same
    moment do not probe the same corner of the window (see :func:`_scan_slots`).
    """
    for base in _scan_slots(spread_seed):
        block = [base, base + 1] + [base + 2 + index for index in range(agents)]
        if all(_port_free(port) for port in block):
            return base, base + 2
    raise ChainError(f"no free {2 + agents}-port block in {PORT_SCAN_START}-{PORT_SCAN_END}")


def _port_free(port: int) -> bool:
    """True when the port can be bound right now (the benchmark's probe)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((PORT_HOST, port))
    except OSError:
        return False
    return True


def experiment_env(episode: Episode) -> dict[str, str]:
    """The run's environment: the descriptor's env plus what the run itself needs.

    ``UV_CACHE_DIR`` comes from the descriptor (episode/run.py relocates HOME
    into the episode root, so an unpinned cache would rebuild the venv and
    re-download every dependency per episode); ``PYTHONPATH=src`` and the
    ``no_proxy`` pair are the documented LLaMAR conventions, ``OPENAI_API_KEY``
    is the binding's key, and the HOME-scoped caches a run writes are pinned
    under ``workspace/`` so nothing lands beside the whitelist. The ``REEF_SAR_*``
    variables describe the harness to the runner, not to the run: they are
    dropped so the graded process cannot come to depend on them.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("REEF_SAR_")}
    env["PYTHONPATH"] = "src"
    env["OPENAI_API_KEY"] = episode.binding.get("api_key", "")
    env["no_proxy"] = env["NO_PROXY"] = "localhost,0.0.0.0,127.0.0.1"
    env["MPLCONFIGDIR"] = str(episode.tree / CACHE_DIR / "matplotlib")
    env["XDG_CACHE_HOME"] = str(episode.tree / CACHE_DIR)
    env["XDG_CONFIG_HOME"] = str(episode.tree / CACHE_DIR / "config")
    return env


def run_logged(command: Sequence[str], *, cwd: Path, env: Mapping[str, str], out: Path, err: Path) -> int:
    """Run ``command`` with its output in ``out``/``err``; returns its exit code.

    The output is captured, never passed through: stdout's last line is the
    metrics object the step record reads. The tail of stderr is echoed on
    failure so the episode record says what went wrong.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out, "wb") as stdout, open(err, "wb") as stderr:
            completed = subprocess.run(command, cwd=cwd, env=dict(env), stdout=stdout, stderr=stderr, check=False)
    except FileNotFoundError as exc:
        raise ChainError(f"cannot run {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        tail = _tail(err)
        if tail:
            _say(f"{command[0]} exited {completed.returncode}; last output:\n{tail}")
    return completed.returncode


def _tail(path: Path, *, lines: int = 10) -> str:
    """The last ``lines`` lines of a log file, or "" when it is unreadable/empty."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _diagnose_framework_error(episode: Episode) -> None:
    """Echo the experiment's log tails when a run died of a framework error.

    The experiment exits 0 on a framework error (it reports the reason in the
    metrics instead of the exit code), so ``run_logged`` stays silent and the
    step record would otherwise carry no hint of the cause. The tails are
    redacted with the binding's key: the logs live inside the episode root, but
    this module's contract is that the key never reaches a stream.
    """
    key = episode.binding.get("api_key", "")
    for name in ("experiment.err.log", "experiment.out.log"):
        tail = _tail(episode.logs_dir / name, lines=12)
        if not tail:
            continue
        if key:
            tail = tail.replace(key, "<redacted>")
        _say(f"framework_error; tail of {name}:\n{tail}")


# -- collect ------------------------------------------------------------------


def collect_artifacts(
    episode: Episode,
    *,
    task_text: str,
    run_meta: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Copy the run's gradeable artifacts into ``sar/out``; returns (copied, missing).

    ``run_meta.json`` (the episode identity the trajectory reader reads),
    ``task.json`` (the task exactly as reef sent it) and the redacted config
    snapshot are always written; the run's own files are copied when the run
    produced them, and a missing one is reported rather than faked. The truth
    guard runs last: a truth artifact under ``sar/out`` would leak
    evaluator-private data into the step record.
    """
    out_dir = episode.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    for name in COLLECT_FILES:
        source = episode.run_dir / name
        if source.is_file():
            shutil.copyfile(source, out_dir / name)
            copied.append(name)
        else:
            missing.append(name)
    files = {"collected": copied, "missing": missing}
    (out_dir / RUN_META_FILE).write_text(
        json.dumps({**dict(run_meta), "files": files}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / TASK_FILE).write_text(task_text if task_text.endswith("\n") else task_text + "\n", encoding="utf-8")
    snapshot = redact_config(read_config(episode.overlay))
    (out_dir / CONFIG_SNAPSHOT).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _assert_no_truth(out_dir)
    return copied, missing


def redact_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """The config as an artifact: the binding's key is replaced by a marker.

    The snapshot outlives the episode (it is copied into the step record), so
    the credential must not travel with it. The key is the only value treated
    this way: every other value is what the runner read.
    """
    snapshot = json.loads(json.dumps(dict(data), default=str))
    llm = snapshot.get("llm")
    if isinstance(llm, dict) and "api_key" in llm:
        llm["api_key"] = "<redacted>"
    return snapshot


def _assert_no_truth(out_dir: Path) -> None:
    """Refuse to finish when a truth artifact reached the trajectory directory."""
    leaked = [name for name in TRUTH_ARTIFACTS if (out_dir / name).exists()]
    leaked += [
        path.name for path in out_dir.rglob("*") if path.is_file() and "truth" in path.name and path.name not in leaked
    ]
    if leaked:
        raise ChainError(f"evaluator-private truth leaked into {out_dir}: {', '.join(sorted(set(leaked)))}")


def metrics_line(
    episode: Episode, *, run_metrics: Mapping[str, Any], eval_metrics: Mapping[str, Any], exit_code: int
) -> dict[str, Any]:
    """The one stdout line: the numbers a step record reads without opening a file."""
    return {
        "scene": episode.task["scene"],
        "agents": episode.task["agents"],
        "seed": episode.task["seed"],
        "transport_rate": run_metrics.get("transport_rate"),
        "load_balance_b": _nested(eval_metrics, "l2_planning", "load_balance_b"),
        "effective_billed_tokens": _nested(eval_metrics, "l4_cost", "effective_billed_tokens"),
        "steps": run_metrics.get("steps"),
        "end_reason": run_metrics.get("end_reason"),
        "exit_code": exit_code,
    }


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    """``mapping`` walked along ``path``; None at the first missing or non-object step."""
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    """A JSON object from ``path``; ``{}`` when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run_meta(episode: Episode, *, task_text: str, exit_code: int | None = None) -> dict[str, Any]:
    """The episode identity file: which task ran, from which code baseline.

    The materialized checkout carries no ``.git`` (``git archive`` output), so the
    experiment's own ``metadata.json`` cannot record a commit; the frozen ref is
    recorded here instead, beside the task it was graded on.
    """
    meta: dict[str, Any] = {
        "runner": f"reef-sar {__version__}",
        "scene": episode.task["scene"],
        "agents": episode.task["agents"],
        "seed": episode.task["seed"],
        "max_steps": episode.max_steps,
        "max_steps_source": episode.max_steps_source,
        "mode": MODE,
        "task": task_text.strip(),
        "llamar_ref": episode.ref,
        "llamar_ref_source": episode.ref_source,
        "run_dir": str(episode.run_dir),
        "truth_dir": str(episode.truth_dir),
    }
    if exit_code is not None:
        meta["exit_code"] = exit_code
    return meta


# -- the chain ----------------------------------------------------------------


def run_episode(episode: Episode, *, task_text: str) -> tuple[int, dict[str, Any]]:
    """Run the whole chain; returns (experiment exit code, the metrics line).

    The identity file lands in ``sar/out`` before any work starts, so an episode
    the chain cannot finish still tells the step record which task and which
    frozen ref it was (a chain failure leaves no metrics, and the reader turns
    that into the floor score rather than a verdict). ``preflight`` runs first:
    the tooling check is seconds, the materialization is not.
    """
    for warning in episode.warnings:
        _say(warning)
    tooling = preflight()
    if not tooling["uv_cache_warm"]:
        _say(
            f"UV_CACHE_DIR={tooling['uv_cache_dir']!r} is unset or absent; every episode will rebuild its "
            "venv and re-download dependencies from the network"
        )
    episode.out_dir.mkdir(parents=True, exist_ok=True)
    (episode.out_dir / RUN_META_FILE).write_text(
        json.dumps(run_meta(episode, task_text=task_text), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _say(f"materializing {episode.ref} from {episode.repo}")
    count = materialize(episode.repo, episode.ref, episode.repo_dir)
    written, notes = apply_overlay(episode.overlay, episode.repo_dir)
    _say(f"materialized {count} files; overlay wrote {len(written)}: {', '.join(written) or '(none)'}")
    for note in notes:
        _say(note)
    if not episode.binding:
        raise ChainError(
            "the rendered config carries no model binding; a gate episode cannot reach a model without "
            f"{CONFIG_FILE}'s llm block (reef renders it at evaluation time)"
        )
    # Allocate the block as late as possible: the probe only frees the ports the
    # instant it returns, so the launch must follow immediately (the plan's ports
    # were a preview).
    coordinator_port, agent_base_port = allocate_ports(int(episode.task["agents"]), spread_seed=str(episode.tree))
    episode = replace(episode, coordinator_port=coordinator_port, agent_base_port=agent_base_port)
    env = experiment_env(episode)
    started = time.time()
    _say("running: " + " ".join(episode.experiment_command()))
    exit_code = run_logged(
        episode.experiment_command(),
        cwd=episode.repo_dir,
        env=env,
        out=episode.logs_dir / "experiment.out.log",
        err=episode.logs_dir / "experiment.err.log",
    )
    if exit_code != 0:
        _say(f"experiment exited {exit_code}; collecting whatever it produced")
    _say("collecting: " + " ".join(episode.eval_command()))
    eval_code = run_logged(
        episode.eval_command(),
        cwd=episode.repo_dir,
        env=env,
        out=episode.logs_dir / "eval.out.log",
        err=episode.logs_dir / "eval.err.log",
    )
    if eval_code != 0:
        _say(f"the metrics collector exited {eval_code}; the run keeps what it produced")
    run_metrics = _read_json_object(episode.run_dir / "run_metrics.json")
    eval_metrics = _read_json_object(episode.run_dir / "eval_metrics.json")
    if run_metrics.get("end_reason") == "framework_error":
        _diagnose_framework_error(episode)
    copied, missing = collect_artifacts(
        episode,
        task_text=task_text,
        run_meta={
            **run_meta(episode, task_text=task_text, exit_code=exit_code),
            "eval_collector_exit_code": eval_code,
            "duration_s": round(time.time() - started, 1),
        },
    )
    _say(f"collected {len(copied)} file(s) into {episode.out_dir} (missing: {', '.join(missing) or 'none'})")
    return exit_code, metrics_line(episode, run_metrics=run_metrics, eval_metrics=eval_metrics, exit_code=exit_code)


def run_dry(episode: Episode) -> int:
    """Materialize and render the tree, print the plan, run nothing.

    A dry run is a rehearsal: it proves the ref materializes, the overlay lands
    where the harness reads it, and the config translates - then prints the
    commands the real run would execute and the artifacts it would leave.
    """
    if episode.overlay_missing:
        report = episode.plan()
        report["dry_run"] = {"materialized": None, "skipped": "the rendered tree is incomplete"}
        print(json.dumps(report, indent=2, sort_keys=True))
        _say(f"--dry-run: the rendered tree is incomplete, missing {', '.join(episode.overlay_missing)}")
        return INCOMPLETE_TREE_EXIT
    for warning in episode.warnings:
        _say(warning)
    tooling = preflight()
    count = materialize(episode.repo, episode.ref, episode.repo_dir)
    written, notes = apply_overlay(episode.overlay, episode.repo_dir)
    for note in notes:
        _say(note)
    report = episode.plan()
    report["tooling"] = tooling
    report["dry_run"] = {
        "materialized": {"ref": episode.ref, "files": count, "into": str(episode.repo_dir)},
        "overlay_written": written,
        "notes": notes,
        "nothing_executed": True,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    _say(f"--dry-run: {count} files materialized, {len(written)} overlay file(s) written, nothing executed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """``reef-sar`` entry point; see the module docstring for the exit codes."""
    parser = argparse.ArgumentParser(
        prog="reef-sar",
        description="Run one SAR simulation as one reef harness episode.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help='the episode task JSON, e.g. \'{"scene":3,"agents":2,"seed":0,"max_steps":35}\'',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="materialize the checkout, apply the overlay, and print the episode plan without running the experiment",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.task is None:
        parser.error("the episode task JSON is required (reef passes it as the first argument)")

    try:
        task = parse_task(args.task)
        episode = build_plan(task, os.environ)
    except ValueError as exc:
        _say(str(exc))
        return USAGE_EXIT
    except ChainError as exc:
        _say(str(exc))
        return exc.code

    if args.dry_run:
        try:
            return run_dry(episode)
        except ChainError as exc:
            _say(str(exc))
            return exc.code

    try:
        exit_code, line = run_episode(episode, task_text=args.task)
    except ChainError as exc:
        _say(str(exc))
        return exc.code
    print(json.dumps(line, sort_keys=True))
    return exit_code


if __name__ == "__main__":  # pragma: no cover - module invocation
    raise SystemExit(main())

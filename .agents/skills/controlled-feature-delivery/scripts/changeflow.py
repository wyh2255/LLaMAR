#!/usr/bin/env python3
"""Fail-closed, standard-library ledger for controlled feature delivery."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
APPROVALS = {"APPROVE", "REJECT"}
TERMINAL = {"COMMITTED"}
SMOKE_SKILL = "controlled-feature-delivery"
SMOKE_RUNNERS = {"opencode"}
RECOVERY_OUTCOMES = {"clean", "adopt", "repair"}
STATE_FIELDS = {
    "schema_version",
    "change_id",
    "state",
    "revision",
    "active_phase",
    "resume_state",
    "owner",
    "updated_at",
    "history_tail_hash",
}


class GateError(Exception):
    """A rejected state-machine gate."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resolve_repo_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        capture_output=True,
        check=True,
    )
    return Path(result.stdout.strip()).resolve()


def validate_relative_repo_path(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        raise GateError("path must be a repository-relative path")
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise GateError("path resolves outside repository")
    return resolved


def relative_path(root: Path, raw: str) -> str:
    return str(validate_relative_repo_path(root, raw).relative_to(root))


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise GateError(f"JSON object required: {path}")
    return value


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, encoding="utf-8", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def manifest_digest(manifest: dict[str, Any]) -> str:
    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return sha256_bytes(canonical_bytes(value))


def git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=root, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def workspace_fingerprint(root: Path) -> str:
    entries = [
        line
        for line in git(
            root, "status", "--porcelain=v1", "--untracked-files=all"
        ).splitlines()
        if ".hermes/" not in line
    ]
    return sha256_bytes(
        canonical_bytes({"head": git(root, "rev-parse", "HEAD"), "status": entries})
    )


def _file_record(path: Path) -> tuple[str, str]:
    info = path.lstat() if path.exists() or path.is_symlink() else None
    if info is None:
        return "missing", ""
    if stat.S_ISLNK(info.st_mode):
        return "120000", sha256_bytes(os.readlink(path).encode())
    if stat.S_ISREG(info.st_mode):
        mode = "100755" if info.st_mode & 0o111 else "100644"
        return mode, sha256_bytes(path.read_bytes())
    raise GateError(f"unsupported managed file type: {path}")


def managed_snapshot(root: Path) -> dict[str, str]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        text=False,
        capture_output=True,
        check=True,
    ).stdout
    paths = {item.decode() for item in output.split(b"\0") if item}
    result = {}
    for name in paths:
        if name.startswith(".hermes/"):
            continue
        mode, digest = _file_record(root / name)
        if mode != "missing":
            result[name] = f"{mode}:{digest}"
    return result


def managed_tree_digest(root: Path) -> str:
    return sha256_bytes(canonical_bytes(managed_snapshot(root)))


def skill_digest(root: Path) -> str:
    skill_dir = root / ".agents/skills" / SMOKE_SKILL
    if not skill_dir.is_dir():
        raise GateError("skill directory is missing")
    files = {}
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[str(path.relative_to(skill_dir))] = sha256_bytes(path.read_bytes())
    return sha256_bytes(canonical_bytes(files))


def run_dir(root: Path, change_id: str) -> Path:
    if (
        not change_id
        or len(change_id) > 64
        or change_id[0] in "_-"
        or "." in change_id
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in change_id)
    ):
        raise GateError("invalid change id")
    return root / ".hermes" / "runs" / change_id


def state_digest(state: dict[str, Any]) -> str:
    value = dict(state)
    value.pop("integrity_sha256", None)
    return sha256_bytes(canonical_bytes(value))


def load_state(directory: Path) -> dict[str, Any]:
    state = load_json(directory / "state.json")
    if (
        not STATE_FIELDS <= state.keys()
        or state.get("schema_version") != SCHEMA_VERSION
        or not isinstance(state.get("change_id"), str)
        or not isinstance(state.get("state"), str)
        or not isinstance(state.get("revision"), int)
        or state.get("revision", 0) < 1
        or not isinstance(state.get("owner"), str)
        or not state.get("owner")
        or (
            state.get("resume_state") is not None
            and not isinstance(state.get("resume_state"), str)
        )
        or not isinstance(state.get("history_tail_hash"), str)
    ):
        raise GateError("unknown state schema")
    if state.get("integrity_sha256") != state_digest(state):
        raise GateError("state integrity failure")
    history = directory / "history.jsonl"
    previous = ""
    revisions = []
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("previous_hash", "") != previous or record.get(
                "record_hash"
            ) != record_digest(record):
                raise GateError("history integrity failure")
            previous = record["record_hash"]
            revisions.append(record["revision"])
    if revisions and (
        revisions[-1] != state["revision"]
        or revisions != list(range(1, state["revision"] + 1))
    ):
        raise GateError("history does not match state")
    if state["history_tail_hash"] != (previous if revisions else ""):
        raise GateError("state history tail mismatch")
    return state


def record_digest(record: dict[str, Any]) -> str:
    value = dict(record)
    value.pop("record_hash", None)
    return sha256_bytes(canonical_bytes(value))


@contextmanager
def run_lock(directory: Path):
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / ".lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def save_transition(
    directory: Path,
    state: dict[str, Any],
    event: str,
    expected: int,
    details: dict[str, Any] | None = None,
) -> None:
    if state["revision"] != expected:
        raise GateError("revision conflict")
    state["revision"] += 1
    state["updated_at"] = now()
    history_path = directory / "history.jsonl"
    lines = (
        history_path.read_text(encoding="utf-8").splitlines()
        if history_path.exists()
        else []
    )
    previous = json.loads(lines[-1]).get("record_hash", "") if lines else ""
    record = {
        "at": state["updated_at"],
        "event": event,
        "revision": state["revision"],
        "state": state["state"],
        "previous_hash": previous,
        "details": details or {},
    }
    record["record_hash"] = record_digest(record)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    state["history_tail_hash"] = record["record_hash"]
    state["integrity_sha256"] = state_digest(state)
    atomic_write_json(directory / "state.json", state)
    with (directory / "progress.md").open("a", encoding="utf-8") as handle:
        handle.write(f"- {record['at']} | {event} | `{state['state']}`\n")


def phase(manifest: dict[str, Any], phase_id: str) -> dict[str, Any]:
    for item in manifest.get("phases", []):
        if item.get("id") == phase_id:
            return item
    raise GateError("unknown phase")


def require_approval(artifact: dict[str, Any]) -> None:
    if artifact.get("verdict") not in APPROVALS:
        raise GateError("invalid review verdict")
    blockers = artifact.get("blockers")
    if artifact["verdict"] == "REJECT" and (
        not isinstance(blockers, list) or not blockers
    ):
        raise GateError("reject must include a blocker")
    if artifact["verdict"] != "APPROVE" or blockers:
        raise GateError("review is not approved")
    for major in artifact.get("majors", []):
        if (
            not isinstance(major, dict)
            or not major.get("target_phase")
            or not major.get("acceptance")
        ):
            raise GateError("major lacks target_phase or acceptance")


def require_provenance(
    artifact: dict[str, Any], expected: dict[str, Any], label: str
) -> None:
    scope = artifact.get("scope")
    if scope != expected:
        raise GateError(f"{label} provenance mismatch")


def require_fresh_workspace(root: Path, state: dict[str, Any], prefix: str) -> None:
    expected = state.get(f"{prefix}_fingerprint")
    if expected and workspace_fingerprint(root) != expected:
        raise GateError(f"{prefix} workspace is stale")


SECRET_PATTERN = re.compile(
    r"(?i)(?P<key>api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)"
    r"(?P<separator>\s*[:=]\s*|\s+)(?P<quote>['\"]?)(?P<value>[^\s'\",}]+)(?P=quote)"
)
BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
INLINE_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:sk|gh[opsr]|xox[baprs])-[A-Za-z0-9_-]{8,}\b"
)


def redact_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<REDACTED>")
    redacted = SECRET_PATTERN.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}<REDACTED>",
        redacted,
    )
    redacted = BEARER_PATTERN.sub(r"\1<REDACTED>", redacted)
    return INLINE_SECRET_PATTERN.sub("<REDACTED>", redacted)


def verification_environment(env: dict[str, str], cwd: Path) -> dict[str, Any]:
    return {
        "cwd": str(cwd),
        "executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "env_keys": sorted(env),
    }


def validate_verification(
    root: Path,
    record: dict[str, Any],
    specs: list[dict[str, Any]],
    scope_paths: set[str],
    scope: str,
) -> None:
    expected_names = {item.get("name") for item in specs}
    checks = record.get("checks")
    scope_paths_value = record.get("scope_paths")
    if (
        record.get("verdict") != "PASS"
        or record.get("scope") != scope
        or not isinstance(checks, list)
        or not isinstance(scope_paths_value, list)
        or not all(isinstance(path, str) for path in scope_paths_value)
        or len(checks) != len(expected_names)
        or not all(isinstance(item, dict) for item in checks)
        or {item.get("name") for item in checks} != expected_names
        or sorted(scope_paths_value) != sorted(scope_paths)
        or record.get("managed_tree_digest") != managed_tree_digest(root)
        or record.get("workspace_fingerprint") != workspace_fingerprint(root)
    ):
        raise GateError(
            "verification evidence is missing, malformed, stale, or out of scope"
        )
    for check in checks:
        if (
            not isinstance(check, dict)
            or check.get("exit_code") != 0
            or check.get("timed_out") is not False
            or not isinstance(check.get("log_path"), str)
            or not validate_relative_repo_path(root, check["log_path"]).is_file()
        ):
            raise GateError("verification check evidence is malformed or failed")


def index_tree(root: Path) -> str:
    return git(root, "write-tree")


def validate_manifest(root: Path, manifest: dict[str, Any]) -> None:
    required = (
        "schema_version",
        "change_id",
        "goal",
        "base_commit",
        "plan_path",
        "plan_sha256",
        "manifest_sha256",
        "phases",
        "integration_checks",
        "canonical_doc_targets",
    )
    if (
        any(key not in manifest for key in required)
        or manifest["schema_version"] != SCHEMA_VERSION
    ):
        raise GateError("incomplete manifest")
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise GateError("manifest digest mismatch")
    if manifest.get("smoke_runner", "opencode") not in SMOKE_RUNNERS:
        raise GateError("unsupported smoke runner")
    validate_relative_repo_path(root, manifest["plan_path"])
    for target in manifest["canonical_doc_targets"]:
        validate_relative_repo_path(root, target)
    for item in manifest["phases"]:
        if (
            not item.get("id")
            or not isinstance(item.get("allowed_paths"), list)
            or not isinstance(item.get("forbidden_paths"), list)
        ):
            raise GateError("invalid phase path policy")
        for path in item["allowed_paths"] + item["forbidden_paths"]:
            if path.startswith("/") or ".." in Path(path).parts:
                raise GateError("invalid manifest path policy")
        validate_checks(item.get("required_checks", []), root)
    validate_checks(manifest["integration_checks"], root)


def validate_checks(checks: list[dict[str, Any]], root: Path) -> None:
    for spec in checks:
        if (
            not isinstance(spec.get("argv"), list)
            or not spec["argv"]
            or not all(isinstance(x, str) for x in spec["argv"])
        ):
            raise GateError("invalid check argv")
        validate_relative_repo_path(root, spec.get("cwd", "."))
        if (
            not isinstance(spec.get("timeout_seconds", 0), (int, float))
            or spec["timeout_seconds"] <= 0
        ):
            raise GateError("invalid check timeout")


def changed_paths(
    root: Path, base: str, baseline: dict[str, str] | None = None
) -> set[str]:
    current = managed_snapshot(root)
    if baseline is not None:
        return {
            name
            for name in set(current) | set(baseline)
            if current.get(name) != baseline.get(name)
        }
    names = set()
    for line in git(root, "diff", "--name-status", base).splitlines():
        parts = line.split("\t")
        names.update(parts[1:] if parts[0].startswith(("R", "C")) else parts[1:2])
    names.update(
        current.keys()
        - set(git(root, "ls-tree", "-r", "--name-only", base).splitlines())
    )
    return {name for name in names if not name.startswith(".hermes/")}


def enforce_scope(
    root: Path,
    manifest: dict[str, Any],
    phase_id: str | None = None,
    baseline: dict[str, str] | None = None,
) -> None:
    policy = (
        phase(manifest, phase_id)
        if phase_id
        else {"allowed_paths": [], "forbidden_paths": []}
    )
    paths = changed_paths(root, manifest["base_commit"], baseline)
    forbidden = [
        p
        for p in paths
        if any(fnmatch.fnmatch(p, pattern) for pattern in policy["forbidden_paths"])
    ]
    if forbidden:
        raise GateError(f"forbidden changed paths: {forbidden}")
    if any(
        not any(fnmatch.fnmatch(p, pattern) for pattern in policy["allowed_paths"])
        for p in paths
    ):
        raise GateError("changed path is outside phase allowlist")


def command_init(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = validate_relative_repo_path(root, args.manifest)
    manifest = load_json(manifest_path)
    validate_manifest(root, manifest)
    plan_path = validate_relative_repo_path(root, manifest["plan_path"])
    if not plan_path.is_file() or manifest["plan_sha256"] != sha256_bytes(
        plan_path.read_bytes()
    ):
        raise GateError("plan does not exist or digest mismatches")
    if manifest["base_commit"] != git(root, "rev-parse", "HEAD"):
        raise GateError("base commit mismatch")
    directory = run_dir(root, str(manifest["change_id"]))
    if directory.exists():
        raise GateError("run already exists")
    directory.mkdir(parents=True)
    atomic_write_json(directory / "manifest.json", manifest)
    state = {
        "schema_version": SCHEMA_VERSION,
        "change_id": manifest["change_id"],
        "state": "NEW",
        "revision": 1,
        "active_phase": None,
        "resume_state": None,
        "owner": "parent",
        "updated_at": now(),
        "history_tail_hash": "",
        "baseline_fingerprint": workspace_fingerprint(root),
        "baseline_tree_digest": managed_tree_digest(root),
        "baseline_snapshot": managed_snapshot(root),
    }
    atomic_write_json(
        directory / "state.json", {**state, "integrity_sha256": state_digest(state)}
    )
    genesis = {
        "at": state["updated_at"],
        "event": "init",
        "revision": 1,
        "state": "NEW",
        "previous_hash": "",
        "details": {},
    }
    genesis["record_hash"] = record_digest(genesis)
    state["history_tail_hash"] = genesis["record_hash"]
    atomic_write_json(
        directory / "state.json", {**state, "integrity_sha256": state_digest(state)}
    )
    (directory / "history.jsonl").write_text(json.dumps(genesis, sort_keys=True) + "\n")
    (directory / "progress.md").write_text(
        f"# Progress: {manifest['change_id']}\n\nGoal: {manifest['goal']}\nBase: {manifest['base_commit']}\nPhases: {', '.join(item['id'] for item in manifest['phases'])}\n"
    )
    return {"change_id": manifest["change_id"], "revision": 1, "state": "NEW"}


def command_run_check(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    directory = run_dir(root, args.change)
    state = load_state(directory)
    manifest = load_json(directory / "manifest.json")
    specs = (
        manifest["integration_checks"]
        if args.scope == "integration"
        else phase(manifest, args.scope)["required_checks"]
    )
    spec = next((item for item in specs if item.get("name") == args.name), None)
    if spec is None:
        raise GateError("check is not approved for this scope")
    enforce_scope(
        root,
        manifest,
        None if args.scope == "integration" else args.scope,
        state.get("phase_start_snapshot"),
    )
    cwd = validate_relative_repo_path(root, spec.get("cwd", "."))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    env.update(spec.get("env", {}))
    checks_dir = (
        directory
        / ("integration" if args.scope == "integration" else f"phases/{args.scope}")
        / "checks"
    )
    checks_dir.mkdir(parents=True, exist_ok=True)
    started = now()
    try:
        result = subprocess.run(
            spec["argv"],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=spec["timeout_seconds"],
            check=False,
        )
        code, timed, output = result.returncode, False, result.stdout + result.stderr
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode()
            if isinstance(error.stdout, bytes)
            else error.stdout or ""
        )
        stderr = (
            error.stderr.decode()
            if isinstance(error.stderr, bytes)
            else error.stderr or ""
        )
        output = stdout + stderr
        code, timed = 124, True
    secret_values = tuple(
        value
        for key, value in env.items()
        if re.search(r"(?i)(key|token|password|passwd|secret)", key)
    )
    output = redact_text(output, secret_values)
    log_path = checks_dir / f"{args.name}.log"
    log_path.write_text(output, encoding="utf-8")
    record_path = checks_dir.parent / "verification.json"
    record = (
        load_json(record_path)
        if record_path.exists()
        else {"scope": args.scope, "checks": []}
    )
    if (
        record.get("scope") not in {None, args.scope}
        or not isinstance(record.get("checks"), list)
        or not all(
            isinstance(item, dict) and isinstance(item.get("name"), str)
            for item in record["checks"]
        )
    ):
        raise GateError("malformed verification record")
    record["scope"] = args.scope
    record["checks"] = [x for x in record["checks"] if x["name"] != args.name] + [
        {
            "name": args.name,
            "argv": [redact_text(item, secret_values) for item in spec["argv"]],
            "cwd": str(cwd.relative_to(root)),
            "started_at": started,
            "finished_at": now(),
            "exit_code": code,
            "timed_out": timed,
            "log_path": str(log_path.relative_to(root)),
            "environment": verification_environment(env, cwd),
        }
    ]
    record["managed_tree_digest"] = managed_tree_digest(root)
    record["workspace_fingerprint"] = workspace_fingerprint(root)
    record["verdict"] = (
        "PASS"
        if all(x["exit_code"] == 0 and not x["timed_out"] for x in record["checks"])
        and {x["name"] for x in record["checks"]} >= {x["name"] for x in specs}
        else "FAIL"
    )
    baseline = (
        None if args.scope == "integration" else state.get("phase_start_snapshot")
    )
    record["scope_paths"] = sorted(
        changed_paths(root, manifest["base_commit"], baseline)
    )
    atomic_write_json(record_path, record)
    return {
        "check": args.name,
        "exit_code": code,
        "verdict": record["verdict"],
        "revision": state["revision"],
        "details": {"scope": args.scope, "name": args.name},
    }


def validate_brief(root: Path, path: str) -> str:
    target = validate_relative_repo_path(root, path)
    content = target.read_text(encoding="utf-8")
    required = (
        "Change:",
        "Why:",
        "Residual risk:",
        "Documentation impact:",
        "Status:",
        "## Evidence",
    )
    if not all(marker in content for marker in required):
        raise GateError("brief lacks required sections")
    return sha256_bytes(target.read_bytes())


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init")
    init.add_argument("--manifest", required=True)
    register = subs.add_parser("register-run")
    register.add_argument("--manifest", required=True)
    status = subs.add_parser("status")
    status.add_argument("--change", required=True)
    doctor = subs.add_parser("doctor")
    doctor.add_argument("--change", required=True)
    for name in (
        "record-plan-review",
        "phase-parent-review",
        "doc-impact",
        "docs-verify",
        "final-review",
    ):
        c = subs.add_parser(name)
        c.add_argument("--change", required=True)
        c.add_argument("--artifact", required=True)
        c.add_argument("--expected-revision", type=int, required=True)
        c.add_argument("--phase")
    start = subs.add_parser("phase-start")
    start.add_argument("--change", required=True)
    start.add_argument("--phase", required=True)
    start.add_argument("--implementer", required=True)
    start.add_argument("--expected-revision", type=int, required=True)
    verify = subs.add_parser("phase-verify")
    verify.add_argument("--change", required=True)
    verify.add_argument("--phase", required=True)
    verify.add_argument("--expected-revision", type=int, required=True)
    integration = subs.add_parser("integration-verify")
    integration.add_argument("--change", required=True)
    integration.add_argument("--expected-revision", type=int, required=True)
    for name in (
        "block",
        "resume",
        "interrupt",
        "reconcile",
        "repair-complete",
        "prepare-commit",
    ):
        c = subs.add_parser(name)
        c.add_argument("--change", required=True)
        c.add_argument("--expected-revision", type=int, required=True)
        c.add_argument("--reason", default="")
        c.add_argument("--outcome", choices=sorted(RECOVERY_OUTCOMES), default="clean")
        if name == "repair-complete":
            c.add_argument("--target", required=True)
    revise = subs.add_parser("revise-plan")
    revise.add_argument("--change", required=True)
    revise.add_argument("--manifest", required=True)
    revise.add_argument("--expected-revision", type=int, required=True)
    commit = subs.add_parser("record-commit")
    commit.add_argument("--change", required=True)
    commit.add_argument("--commit", required=True)
    commit.add_argument("--expected-revision", type=int, required=True)
    smoke = subs.add_parser("smoke-skill-load")
    smoke.add_argument("--change", required=True)
    smoke.add_argument("--expected-revision", type=int, required=True)
    smoke.add_argument("--runner", choices=sorted(SMOKE_RUNNERS), default="opencode")
    smoke = subs.add_parser("record-smoke")
    smoke.add_argument("--change", required=True)
    smoke.add_argument("--artifact", required=True)
    smoke.add_argument("--expected-revision", type=int, required=True)
    check = subs.add_parser("run-check")
    check.add_argument("--change", required=True)
    check.add_argument("--scope", required=True)
    check.add_argument("--name", required=True)
    check.add_argument("--expected-revision", type=int, required=True)
    brief = subs.add_parser("validate-brief")
    brief.add_argument("--change", required=True)
    brief.add_argument("--path", required=True)
    approve = subs.add_parser("human-approve")
    approve.add_argument("--change", required=True)
    approve.add_argument("--scope", required=True)
    approve.add_argument("--reference", required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    reject = subs.add_parser("human-reject")
    reject.add_argument("--change", required=True)
    reject.add_argument("--scope", required=True)
    reject.add_argument("--reference", required=True)
    reject.add_argument("--reason", required=True)
    reject.add_argument("--expected-revision", type=int, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = resolve_repo_root(Path.cwd())
        if args.command in {"init", "register-run"}:
            with run_lock(root / ".hermes/runs/.register"):
                output = command_init(root, args)
        elif args.command == "status":
            output = load_state(run_dir(root, args.change))
        elif args.command == "doctor":
            bootstrap_root = root / ".hermes/bootstrap"
            manifest_path = bootstrap_root / args.change / "manifest.json"
            if not manifest_path.is_file():
                candidates = sorted(bootstrap_root.glob("*/manifest.json"))
                matching = [
                    path
                    for path in candidates
                    if load_json(path).get("change_id") == args.change
                ]
                if len(matching) != 1:
                    raise GateError("bootstrap manifest not found or ambiguous")
                manifest_path = matching[0]
            manifest = load_json(manifest_path)
            validate_manifest(root, manifest)
            if manifest.get("change_id") != args.change:
                raise GateError("bootstrap manifest change_id mismatch")
            if manifest_digest(manifest) != manifest.get("manifest_sha256"):
                raise GateError("bootstrap manifest digest mismatch")
            output = {
                "change": args.change,
                "manifest": str(manifest_path.relative_to(root)),
                "status": "PASS",
            }
        elif args.command == "validate-brief":
            output = {"valid": True, "sha256": validate_brief(root, args.path)}
        else:
            directory = run_dir(root, args.change)
            with run_lock(directory):
                state = load_state(directory)
                manifest = load_json(directory / "manifest.json")
                event = args.command
                details = {"reason": getattr(args, "reason", "")}
                if (
                    getattr(args, "expected_revision", state["revision"])
                    != state["revision"]
                ):
                    raise GateError("revision conflict")
                if event == "run-check":
                    output = command_run_check(root, args)
                    save_transition(
                        directory,
                        state,
                        event,
                        args.expected_revision,
                        output.pop("details"),
                    )
                    output.update(state=state["state"], revision=state["revision"])
                    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
                    return 0
                if event == "record-plan-review":
                    artifact = load_json(
                        validate_relative_repo_path(root, args.artifact)
                    )
                    require_approval(artifact)
                    expected_scope = {
                        "plan_path": manifest["plan_path"],
                        "plan_sha256": manifest["plan_sha256"],
                        "manifest_sha256": manifest["manifest_sha256"],
                        "base_commit": manifest["base_commit"],
                        "baseline_fingerprint": state["baseline_fingerprint"],
                    }
                    if artifact.get("scope") != expected_scope:
                        raise GateError("plan review provenance mismatch")
                    if state["state"] != "NEW":
                        raise GateError("plan review is out of order")
                    state["state"] = "PLAN_APPROVED"
                elif event == "phase-start":
                    item = phase(manifest, args.phase)
                    index = manifest["phases"].index(item)
                    if state["state"] not in {"PLAN_APPROVED", "PHASE_N_VERIFIED"} or (
                        index
                        and state.get("last_phase")
                        != manifest["phases"][index - 1]["id"]
                    ):
                        raise GateError("phase cannot start")
                    baseline = managed_snapshot(root)
                    enforce_scope(
                        root, manifest, args.phase, state.get("baseline_snapshot")
                    )
                    state.update(
                        state="PHASE_N_IMPLEMENTING",
                        active_phase=args.phase,
                        implementer=args.implementer,
                        phase_start_tree_digest=managed_tree_digest(root),
                        phase_start_fingerprint=workspace_fingerprint(root),
                        phase_start_snapshot=baseline,
                    )
                elif event == "phase-parent-review":
                    if state[
                        "state"
                    ] != "PHASE_N_IMPLEMENTING" or args.phase != state.get(
                        "active_phase"
                    ):
                        raise GateError("phase review is out of order")
                    artifact = load_json(
                        validate_relative_repo_path(root, args.artifact)
                    )
                    require_approval(artifact)
                    require_provenance(
                        artifact,
                        {
                            "phase": args.phase,
                            "base_commit": manifest["base_commit"],
                            "phase_start_fingerprint": state["phase_start_fingerprint"],
                            "phase_start_tree_digest": state["phase_start_tree_digest"],
                        },
                        "phase review",
                    )
                    state["state"] = "PHASE_N_PARENT_REVIEWED"
                    state["open_majors"] = artifact.get("majors", [])
                elif event == "phase-verify":
                    if state[
                        "state"
                    ] != "PHASE_N_PARENT_REVIEWED" or args.phase != state.get(
                        "active_phase"
                    ):
                        raise GateError("phase verification is out of order")
                    record = load_json(
                        directory / f"phases/{args.phase}/verification.json"
                    )
                    if (
                        record.get("verdict") != "PASS"
                        or record.get("managed_tree_digest")
                        != managed_tree_digest(root)
                        or record.get("workspace_fingerprint")
                        != workspace_fingerprint(root)
                        or record.get("scope") != args.phase
                    ):
                        raise GateError(
                            "phase evidence is missing, stale, or out of scope"
                        )
                    validate_verification(
                        root,
                        record,
                        phase(manifest, args.phase)["required_checks"],
                        set(
                            changed_paths(
                                root,
                                manifest["base_commit"],
                                state.get("phase_start_snapshot"),
                            )
                        ),
                        args.phase,
                    )
                    state.update(
                        state="PHASE_N_VERIFIED",
                        last_phase=args.phase,
                        active_phase=None,
                    )
                    if args.phase == manifest["phases"][-1]["id"]:
                        state["state"] = "INTEGRATION_VERIFYING"
                elif event == "integration-verify":
                    record = load_json(directory / "integration/verification.json")
                    if state["state"] != "INTEGRATION_VERIFYING":
                        raise GateError("integration verification is out of order")
                    if (
                        record.get("managed_tree_digest") != managed_tree_digest(root)
                        or record.get("workspace_fingerprint")
                        != workspace_fingerprint(root)
                        or (
                            (
                                any(
                                    item.get("smoke_required")
                                    for item in manifest["phases"]
                                )
                                or manifest.get("real_smoke", {}).get("required")
                                is True
                            )
                            and state.get("smoke", {}).get("status") != "PASS"
                        )
                    ):
                        raise GateError("integration evidence is missing or stale")
                    validate_verification(
                        root,
                        record,
                        manifest["integration_checks"],
                        set(changed_paths(root, manifest["base_commit"])),
                        "integration",
                    )
                    state["state"] = "INTEGRATION_VERIFIED"
                elif event == "doc-impact":
                    artifact = load_json(
                        validate_relative_repo_path(root, args.artifact)
                    )
                    if (
                        state["state"] != "INTEGRATION_VERIFIED"
                        or artifact.get("decision") not in {"update", "none"}
                        or set(artifact.get("targets", []))
                        - set(manifest["canonical_doc_targets"])
                    ):
                        raise GateError("invalid documentation impact")
                    state.update(
                        state="DOCS_VERIFIED"
                        if artifact["decision"] == "none"
                        else "DOC_IMPACT_DECIDED",
                        doc_targets=artifact.get("targets", []),
                    )
                elif event == "docs-verify":
                    if state["state"] != "DOC_IMPACT_DECIDED":
                        raise GateError("documentation verification is out of order")
                    require_approval(
                        load_json(validate_relative_repo_path(root, args.artifact))
                    )
                    enforce_scope(
                        root,
                        {
                            **manifest,
                            "phases": [
                                {
                                    "id": "docs",
                                    "allowed_paths": state.get("doc_targets", []),
                                    "forbidden_paths": [],
                                }
                            ],
                        },
                        "docs",
                        state.get("phase_start_snapshot"),
                    )
                    state["state"] = "DOCS_VERIFIED"
                elif event == "final-review":
                    artifact = load_json(
                        validate_relative_repo_path(root, args.artifact)
                    )
                    require_approval(artifact)
                    if (
                        state["state"] != "DOCS_VERIFIED"
                        or state.get("open_majors")
                        or artifact.get("majors")
                    ):
                        raise GateError("final review is out of order")
                    if not artifact.get("brief_path") or artifact.get(
                        "brief_sha256"
                    ) != validate_brief(root, artifact["brief_path"]):
                        raise GateError("brief is not sealed or fresh")
                    require_provenance(
                        artifact,
                        {
                            "manifest_sha256": manifest["manifest_sha256"],
                            "base_commit": manifest["base_commit"],
                            "tree_digest": managed_tree_digest(root),
                            "workspace_fingerprint": workspace_fingerprint(root),
                        },
                        "final review",
                    )
                    state.update(
                        state="HUMAN_REVIEW",
                        brief_path=artifact["brief_path"],
                        brief_sha256=artifact["brief_sha256"],
                        final_tree_digest=managed_tree_digest(root),
                        final_tree_fingerprint=workspace_fingerprint(root),
                    )
                elif event == "human-approve":
                    if (
                        state["state"] != "HUMAN_REVIEW"
                        or args.scope != "commit"
                        or not args.reference
                    ):
                        raise GateError("human approval is not allowed")
                    require_fresh_workspace(root, state, "final_tree")
                    if state.get("final_tree_digest") != managed_tree_digest(root):
                        raise GateError("final review is stale")
                    state.update(
                        state="HUMAN_APPROVED", approval_reference=args.reference
                    )
                elif event == "human-reject":
                    if (
                        state["state"] != "HUMAN_REVIEW"
                        or args.scope != "commit"
                        or not args.reference
                        or not args.reason.strip()
                    ):
                        raise GateError("human rejection is not allowed")
                    state.update(
                        state="BLOCKED",
                        blocked_from="HUMAN_REVIEW",
                        rejection_reference=args.reference,
                        rejection_reason=args.reason,
                        blocked_fingerprint=workspace_fingerprint(root),
                    )
                elif event == "block":
                    if state["state"] in TERMINAL:
                        raise GateError("terminal run cannot be blocked")
                    state.update(
                        state="BLOCKED",
                        resume_state=state["state"],
                        blocked_from=state["state"],
                        blocked_fingerprint=workspace_fingerprint(root),
                    )
                elif event == "interrupt":
                    if state["state"] in TERMINAL:
                        raise GateError("terminal run cannot be interrupted")
                    state.update(
                        state="INTERRUPTED",
                        resume_state=state["state"],
                        interrupted_from=state["state"],
                        interrupted_fingerprint=workspace_fingerprint(root),
                    )
                elif event == "resume":
                    source = (
                        state.get("blocked_from")
                        if state["state"] == "BLOCKED"
                        else state.get("interrupted_from")
                    )
                    if state["state"] not in {"BLOCKED", "INTERRUPTED"} or not source:
                        raise GateError("nothing can be resumed")
                    require_fresh_workspace(
                        root,
                        state,
                        "blocked" if state["state"] == "BLOCKED" else "interrupted",
                    )
                    state.update(state=source, resume_state=None)
                elif event == "reconcile":
                    if state["state"] not in {"BLOCKED", "INTERRUPTED"}:
                        raise GateError("nothing to reconcile")
                    state.update(
                        state="RECONCILING",
                        reconciliation={
                            "at": now(),
                            "tree_digest": managed_tree_digest(root),
                            "fingerprint": workspace_fingerprint(root),
                            "outcome": args.outcome,
                            "source": state["state"],
                        },
                    )
                    if args.outcome in {"clean", "adopt"}:
                        state.update(
                            state=state.get("resume_state")
                            or state.get("blocked_from", state.get("interrupted_from")),
                            resume_state=None,
                        )
                        state["blocked_fingerprint"] = workspace_fingerprint(root)
                        state["interrupted_fingerprint"] = workspace_fingerprint(root)
                    else:
                        state.update(
                            state="REPAIRING",
                            resume_state=state.get(
                                "blocked_from", state.get("interrupted_from")
                            ),
                            repair_target=state.get(
                                "blocked_from", state.get("interrupted_from")
                            ),
                        )
                elif event == "repair-complete":
                    target = getattr(args, "target", "")
                    if state["state"] != "REPAIRING" or target != state.get(
                        "repair_target"
                    ):
                        raise GateError("repair target does not match")
                    state.update(state=target, resume_state=None)
                elif event == "revise-plan":
                    if state["state"] not in {
                        "NEW",
                        "PLAN_APPROVED",
                        "BLOCKED",
                        "RECONCILING",
                    }:
                        raise GateError("plan cannot be revised now")
                    new_manifest = load_json(
                        validate_relative_repo_path(root, args.manifest)
                    )
                    validate_manifest(root, new_manifest)
                    plan = validate_relative_repo_path(root, new_manifest["plan_path"])
                    if new_manifest["base_commit"] != git(
                        root, "rev-parse", "HEAD"
                    ) or new_manifest["plan_sha256"] != sha256_bytes(plan.read_bytes()):
                        raise GateError("revised plan is not current")
                    atomic_write_json(directory / "manifest.json", new_manifest)
                    state.update(
                        state="NEW", plan_revision=state.get("plan_revision", 0) + 1
                    )
                elif event == "prepare-commit":
                    if state["state"] != "HUMAN_APPROVED" or managed_tree_digest(
                        root
                    ) != state.get("final_tree_digest", managed_tree_digest(root)):
                        raise GateError("commit is not prepared")
                    unstaged = subprocess.run(
                        ["git", "diff", "--quiet"],
                        cwd=root,
                        check=False,
                    )
                    status_lines = git(
                        root, "status", "--porcelain=v1", "--untracked-files=all"
                    ).splitlines()
                    if unstaged.returncode != 0 or any(
                        line.startswith("??") or len(line) < 2 or line[1] != " "
                        for line in status_lines
                    ):
                        raise GateError("unstaged changes prevent commit preparation")
                    state.update(
                        state="COMMIT_PREPARED",
                        prepared_index_tree=index_tree(root),
                        prepared_head=git(root, "rev-parse", "HEAD"),
                    )
                elif event == "record-commit":
                    if (
                        state["state"] != "COMMIT_PREPARED"
                        or len(args.commit) != 40
                        or any(c not in "0123456789abcdef" for c in args.commit)
                    ):
                        raise GateError("invalid commit record")
                    if git(root, "rev-parse", "HEAD") != args.commit:
                        raise GateError("recorded commit is not current HEAD")
                    commit_tree = git(root, "show", "-s", "--format=%T", args.commit)
                    if commit_tree != state.get("prepared_index_tree"):
                        raise GateError("commit tree differs from prepared index")
                    state.update(state="COMMITTED", commit=args.commit)
                elif event == "smoke-skill-load":
                    runner = getattr(args, "runner", "opencode")
                    if runner not in SMOKE_RUNNERS:
                        raise GateError("unsupported smoke runner")
                    resolved = (root / ".agents/skills" / SMOKE_SKILL).resolve()
                    expected_digest = skill_digest(root)
                    probe = subprocess.run(
                        [
                            runner,
                            "run",
                            "--format",
                            "json",
                            f"Load the {SMOKE_SKILL} skill.",
                        ],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                    log_path = directory / "evidence" / "opencode-skill-load.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(probe.stdout + probe.stderr, encoding="utf-8")
                    loaded = False
                    event_digest = ""
                    event_proof = ""
                    for line in probe.stdout.splitlines():
                        try:
                            event_data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        part = event_data.get("part", {})
                        metadata = part.get("state", {}).get("metadata", {})
                        if part.get("type") == "tool" and part.get("tool") == "skill":
                            tool_output = part.get("state", {}).get("output", "")
                            event_digest = metadata.get(
                                "sha256",
                                metadata.get(
                                    "digest", metadata.get("skill_sha256", "")
                                ),
                            )
                            if not event_digest and (
                                f'<skill_content name="{SMOKE_SKILL}">' in tool_output
                            ):
                                event_digest = expected_digest
                                event_proof = "skill-tool-event+resolved-local-tree"
                        if (
                            metadata.get("dir") == str(resolved)
                            or event_proof == "skill-tool-event+resolved-local-tree"
                        ) and event_digest == expected_digest:
                            loaded = True
                            break
                    smoke = {
                        "skill": "controlled-feature-delivery",
                        "resolved_path": str(resolved),
                        "loaded": loaded,
                        "runner": runner,
                        "skill_sha256": expected_digest,
                        "event_skill_sha256": event_digest,
                        "event_proof": event_proof or "runner-metadata",
                        "exit_code": probe.returncode,
                        "log_path": str(log_path.relative_to(root)),
                        "status": "PASS"
                        if loaded and probe.returncode == 0
                        else "FAIL",
                    }
                    atomic_write_json(directory / "smoke-skill-load.json", smoke)
                    details["smoke"] = smoke
                elif event == "record-smoke":
                    smoke = load_json(validate_relative_repo_path(root, args.artifact))
                    expected = str(
                        (root / ".agents/skills/controlled-feature-delivery").resolve()
                    )
                    if (
                        smoke.get("skill") != SMOKE_SKILL
                        or smoke.get("runner") not in SMOKE_RUNNERS
                        or smoke.get("resolved_path") != expected
                        or smoke.get("skill_sha256") != skill_digest(root)
                        or smoke.get("event_skill_sha256") != smoke.get("skill_sha256")
                        or smoke.get("event_proof")
                        not in {
                            "runner-metadata",
                            "skill-tool-event+resolved-local-tree",
                        }
                        or smoke.get("loaded") is not True
                        or smoke.get("status") != "PASS"
                    ):
                        raise GateError("invalid smoke evidence")
                    state["smoke"] = smoke
                else:
                    raise GateError("unknown transition")
                save_transition(
                    directory, state, event, args.expected_revision, details
                )
                output = {"state": state["state"], "revision": state["revision"]}
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        GateError,
        subprocess.CalledProcessError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""P1 artifact 服务：attempt-root 受限 ArtifactStore、series lease/fencing、不可变 manifest/ledger、selected pointer CAS。

实现范围（设计 §3.1-3.5、§1.2 中 P1 需要的持久化原语）：

- `ArtifactStore`：用 pathlib resolve 做 attempt-root containment，拒绝 symlink /
  hardlink 逃逸；同目录 temp → flush → fsync → hash → atomic rename → fsync parent；
  已验证读取（digest/bytes 双校验）；不可变 canonical JSON 写入。
- 白名单 source 快照与确定性 redaction（排除 evaluator 输出 / eval_attempts /
  eval_workspace / secrets / Thinking）。
- `AttemptLease`：基于 flock 的 series-scoped 排他租约 + 单调 fencing token；
  无有效 token 的进程不得写 result / 发布 pointer（§1.2-3）。
- `SelectedPointer`：expected-revision CAS；校验 manifest/ledger/report digest 链；
  仅 terminal=SUCCEEDED 且 digest 一致的 attempt 可发布（§3.3-4 / §3.5）。

本模块不含 workflow、CLI 或任何 LLM 调用。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from sar_orch.eval import contracts as c
from sar_orch.eval.audit import AuditJournal

__all__ = [
    "ArtifactError",
    "ArtifactStore",
    "AttemptBusy",
    "AttemptLease",
    "ContainmentError",
    "FencingError",
    "ImmutableArtifactError",
    "PublishConflict",
    "SelectedPointer",
    "SourceSnapshot",
    "VerificationError",
    "redact_text",
    "snapshot_source_files",
]


# ─────────────────────────────────────────────────────────────────────────────
# 异常与基础工具
# ─────────────────────────────────────────────────────────────────────────────


class ArtifactError(Exception):
    """artifact 服务基异常。"""


class ContainmentError(ArtifactError, ValueError):
    """path 逃逸 attempt root（绝对路径 / .. / symlink / hardlink）。"""


class ImmutableArtifactError(ArtifactError, ValueError):
    """不可变 artifact 已被写入不同字节。"""


class VerificationError(ArtifactError, ValueError):
    """digest / bytes / 引用链校验失败。"""


class AttemptBusy(ArtifactError):
    """series 排他租约被其他进程持有。"""


class FencingError(ArtifactError):
    """fencing token 过期/缺失，禁止受保护写入。"""


class PublishConflict(ArtifactError):
    """selected pointer CAS 冲突（revision 不匹配）。"""


_RELPATH_ADAPTER = TypeAdapter(c.AttemptRelativePath)


def _validate_rel(rel: str) -> str:
    """复用 contracts 的 AttemptRelativePath 校验，统一转成 ContainmentError。"""
    try:
        return _RELPATH_ADAPTER.validate_python(rel)
    except ValidationError as exc:
        raise ContainmentError(f"invalid attempt-relative path {rel!r}: {exc}") from exc


def _resolve_contained(root: Path, rel: str) -> Path:
    """把 rel 解析到 root 内；resolved != lexical 说明存在 symlink 组件，一律拒绝。"""
    rel = _validate_rel(rel)
    root = Path(root).resolve()
    lexical = root.joinpath(rel)
    if not lexical.is_relative_to(root):
        raise ContainmentError(f"path escapes attempt root: {rel!r}")
    resolved = lexical.resolve()
    if resolved != lexical:
        raise ContainmentError(
            f"symlink components not allowed in artifact path: {rel!r}"
        )
    return resolved


def _reject_hardlink(path: Path, rel: str) -> None:
    """attempt root 内的文件不得是 hardlink（含指向 source/legacy workspace 的情况）。"""
    if path.exists() and path.stat().st_nlink > 1:
        raise ContainmentError(f"hardlink not allowed in attempt root: {rel!r}")


def _validate_attempt_root(attempt_root: os.PathLike[str] | str) -> Path:
    """§3.1：attempt root 必须已解析、非 symlink（在 mkdir/open 前验证，不创建任何文件）。

    - root 本身是 symlink → 拒绝；
    - 任一**已存在**的祖先组件是 symlink → 拒绝（否则 resolve() 会让受信 root
      静默落在被重定向的位置，containment 失去意义）。
    仅检查已存在组件；不存在的 tail 组件由 resolve() 追加，不存在逃逸问题。
    """
    root = Path(os.path.abspath(attempt_root))
    cur: Path = root
    while True:
        if cur.is_symlink():
            raise ContainmentError(
                f"attempt root must not be or lie under a symlink: {cur}"
            )
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return root.resolve()


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录 temp → flush → fsync → atomic rename → fsync parent。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_bytes(path, (c.canonical_json(data) + "\n").encode("utf-8"))


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _infer_media_type(rel: str) -> str:
    suffix = rel.rsplit(".", 1)[-1].lower()
    return {
        "json": "application/json",
        "md": "text/markdown",
        "yaml": "application/yaml",
        "yml": "application/yaml",
        "csv": "text/csv",
        "txt": "text/plain",
        "ndjson": "application/x-ndjson",
        "jsonl": "application/x-ndjson",
    }.get(suffix, "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactStore：containment + verified read + atomic canonical write
# ─────────────────────────────────────────────────────────────────────────────


class ArtifactStore:
    """attempt-root 受限 artifact store。

    root 必须是 resolved、非 symlink 的 attempt root（§3.1）。所有写入走
    同目录 atomic rename（temp → fsync → rename → fsync parent）；manifest/ledger
    写入是 immutable 的（首次写入后字节与 SHA-256 永不改变，§1.2-5）。
    """

    MANIFEST_REL = "input_manifest.json"
    LEDGER_REL = "audit/final_ledger.json"
    JOURNAL_REL = "audit/events.jsonl"

    def __init__(self, attempt_root: Path):
        self.root = _validate_attempt_root(attempt_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        return _resolve_contained(self.root, rel)

    def contained_path(self, rel: str, *, reject_hardlink: bool = True) -> Path:
        """attempt-root 受限路径解析：symlink component / hardlink / traversal 一律拒绝。

        §3.1：checkpointer、audit 等**所有** attempt 内持久化面必须经过此解析，
        不得直接 `root / rel`。失败发生在任何 mkdir/open/write 之前。
        """
        path = _resolve_contained(self.root, rel)
        if reject_hardlink:
            _reject_hardlink(path, rel)
        return path

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def write_atomic(
        self,
        rel: str,
        data: bytes,
        *,
        media_type: str | None = None,
        producer: str = "artifact-store",
    ) -> c.ArtifactRef:
        """atomic 写入并返回 ArtifactRef（sha256 / bytes / media_type 由实际字节决定）。"""
        target = _resolve_contained(self.root, rel)
        _reject_hardlink(target, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".tmp-", suffix=".part", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            _fsync_dir(target.parent)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return c.ArtifactRef(
            path=rel,
            sha256=c.sha256_hex(data),
            bytes=len(data),
            media_type=media_type or _infer_media_type(rel),
            producer=producer,
        )

    def write_canonical_json(
        self,
        rel: str,
        data: Any,
        *,
        newline: bool = True,
        producer: str = "artifact-store",
    ) -> c.ArtifactRef:
        text = c.canonical_json(data)
        if newline:
            text += "\n"
        return self.write_atomic(rel, text.encode("utf-8"), producer=producer)

    def read_bytes(self, rel: str, expected_sha: str | None = None) -> bytes:
        target = _resolve_contained(self.root, rel)
        _reject_hardlink(target, rel)
        data = target.read_bytes()
        if expected_sha is not None:
            actual = c.sha256_hex(data)
            if actual != expected_sha:
                raise VerificationError(
                    f"sha256 mismatch for {rel!r}: expected {expected_sha}, got {actual}"
                )
        return data

    def read_verified(self, ref: c.ArtifactRef) -> bytes:
        """verified read：containment + digest + bytes 三重复核（§4 evidence reader）。"""
        if not isinstance(ref, c.ArtifactRef):
            raise ArtifactError("read_verified requires an ArtifactRef")
        data = self.read_bytes(ref.path, expected_sha=ref.sha256)
        if len(data) != ref.bytes:
            raise VerificationError(
                f"byte size mismatch for {ref.path!r}: expected {ref.bytes}, got {len(data)}"
            )
        return data

    def read_text(self, rel: str, expected_sha: str | None = None) -> str:
        return self.read_bytes(rel, expected_sha=expected_sha).decode("utf-8")

    def _write_immutable_json(
        self, rel: str, payload: dict[str, Any], *, producer: str
    ) -> c.ArtifactRef:
        payload_bytes = c.canonical_json(payload).encode("utf-8")
        target = _resolve_contained(self.root, rel)
        if target.exists():
            existing = target.read_bytes()
            if existing == payload_bytes:
                return c.ArtifactRef(
                    path=rel,
                    sha256=c.sha256_hex(payload_bytes),
                    bytes=len(payload_bytes),
                    media_type="application/json",
                    producer=producer,
                )
            raise ImmutableArtifactError(
                f"{rel} is immutable and already written with different bytes"
            )
        return self.write_atomic(
            rel, payload_bytes, media_type="application/json", producer=producer
        )

    # ── input manifest（immutable after freeze） ──────────────────────────────
    def write_input_manifest(self, frozen: c.FrozenInputManifest) -> c.ArtifactRef:
        return self._write_immutable_json(
            self.MANIFEST_REL, frozen.payload(), producer="freeze"
        )

    def read_input_manifest(self) -> c.FrozenInputManifest:
        try:
            data = self.read_bytes(self.MANIFEST_REL)
            manifest = c.InputManifest.model_validate(json.loads(data.decode("utf-8")))
            return c.FrozenInputManifest(manifest=manifest)
        except (OSError, ValueError, ValidationError) as exc:
            raise VerificationError(
                "input_manifest.json is corrupted or missing"
            ) from exc

    # ── final ledger（immutable terminal owner，§1.2-5） ──────────────────────
    def write_final_ledger(self, ledger: c.FinalLedger) -> c.ArtifactRef:
        return self._write_immutable_json(
            self.LEDGER_REL, ledger.model_dump(mode="json"), producer="finalizer"
        )

    def read_final_ledger(self) -> c.FinalLedger | None:
        if not self.exists(self.LEDGER_REL):
            return None
        try:
            data = self.read_bytes(self.LEDGER_REL)
            return c.FinalLedger.model_validate(json.loads(data.decode("utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            raise VerificationError("final_ledger.json is corrupted") from exc

    def audit_journal(self) -> AuditJournal:
        # 走 contained_path：`audit/` 目录被替换为 symlink/hardlink 时，
        # 必须在构造 AuditJournal（其 __init__ 会 mkdir parent）之前报错。
        return AuditJournal(self.contained_path(self.JOURNAL_REL))


# ─────────────────────────────────────────────────────────────────────────────
# source 白名单快照与确定性 redaction
# ─────────────────────────────────────────────────────────────────────────────

_THINKING_TAG_RE = re.compile(r"(?is)<thinking>.*?</thinking>")
_THINKING_LINE_RE = re.compile(r"(?im)^\s*\*?Thinking\*?:.*$")
_JSON_SECRET_RE = re.compile(
    r'(?i)"(api[_-]?key|api[_-]?base|secret|token|password|authorization|cookie)"\s*:\s*"[^"]*"'
)
_SECRET_LINE_RE = re.compile(
    r"(?im)^(\s*(?:api[_-]?key|api[_-]?base|secret|token|password|authorization|cookie)"
    r"\s*[:=]\s*)(\S+)\s*$"
)


def redact_text(text: str) -> tuple[str, list[str]]:
    """确定性 redaction：抹除 Thinking 块/行与 secret key=value，返回 (文本, 原因列表)。

    任何 audit level 都不允许 CoT/secret（§3.4 / §1.2-6）。
    """
    reasons: list[str] = []
    if _THINKING_TAG_RE.search(text):
        text = _THINKING_TAG_RE.sub("[Thinking redacted]", text)
        reasons.append("thinking_block")
    if _THINKING_LINE_RE.search(text):
        text = _THINKING_LINE_RE.sub("[Thinking redacted]", text)
        reasons.append("thinking_line")

    def _mask_json(match: re.Match[str]) -> str:
        reasons.append("secret_value")
        return f'"{match.group(1)}": "[REDACTED]"'

    if _JSON_SECRET_RE.search(text):
        text = _JSON_SECRET_RE.sub(_mask_json, text)

    def _mask(match: re.Match[str]) -> str:
        reasons.append("secret_value")
        return match.group(1) + "[REDACTED]"

    text = _SECRET_LINE_RE.sub(_mask, text)
    return text, reasons


@dataclass(frozen=True)
class SourceSnapshot:
    """source run 白名单快照：relpath → raw SHA-256 / size；redaction 原因与排除清单。

    digest 覆盖 files+sizes，作为 source_input_manifest 的稳定锚点。
    """

    files: dict[str, str]
    sizes: dict[str, int]
    redacted: dict[str, list[str]]
    excluded: dict[str, str]

    @property
    def digest(self) -> str:
        return c.sha256_hex(
            c.canonical_json({"files": self.files, "sizes": self.sizes}).encode("utf-8")
        )


def snapshot_source_files(
    source_dir: Path,
    allowlist: Sequence[str],
    *,
    reserved_dirs: tuple[str, ...] = ("eval_attempts", "eval_workspace"),
    excluded_outputs: tuple[str, ...] = ("eval_report.json", "eval_report.md"),
    redactor=redact_text,
) -> SourceSnapshot:
    """对 allowlist 中的 source 输入文件做白名单快照。

    排除 evaluator 输出（reserved dirs / eval_report.*），拒绝 symlink/hardlink 逃逸；
    文本文件做确定性 redaction，secret/Thinking 只以 redaction reason 留痕（绝不落盘）。
    """
    source_dir = Path(source_dir).resolve()
    files: dict[str, str] = {}
    sizes: dict[str, int] = {}
    redacted: dict[str, list[str]] = {}
    excluded: dict[str, str] = {}
    for rel in allowlist:
        target = _resolve_contained(source_dir, rel)
        _reject_hardlink(target, rel)
        first = rel.split("/", 1)[0]
        leaf = rel.rsplit("/", 1)[-1]
        if first in reserved_dirs or leaf in excluded_outputs:
            excluded[rel] = "reserved evaluator output"
            continue
        data = target.read_bytes()
        files[rel] = c.sha256_hex(data)
        sizes[rel] = len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        red, reasons = redactor(text)
        if red != text:
            redacted[rel] = reasons
    return SourceSnapshot(
        files=files, sizes=sizes, redacted=redacted, excluded=excluded
    )


# ─────────────────────────────────────────────────────────────────────────────
# AttemptLease：series-scoped flock 排他租约 + 单调 fencing token
# ─────────────────────────────────────────────────────────────────────────────


class AttemptLease:
    """series-scoped 排他租约（§1.2-3 / §2.1）。

    flock 在 `.attempt.lock`（文件永不删除，避免 inode swap 竞态）；每次 acquire
    在持有锁时签发严格递增的 fencing token 并原子持久化。租约被其他进程接管后，
    旧 lease 的 `ensure_current()` 会因 token 过期抛 `FencingError`。
    """

    LOCK_NAME = ".attempt.lock"
    FENCE_NAME = ".fencing_token.json"

    def __init__(
        self, series_root: Path, *, owner_id: str, fencing_token: int, _fd: int
    ) -> None:
        self.series_root = Path(series_root).resolve()
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self._fd = _fd
        self._closed = False

    @classmethod
    def acquire(
        cls,
        series_root: Path,
        *,
        owner_id: str | None = None,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> AttemptLease:
        series_root = Path(series_root).resolve()
        series_root.mkdir(parents=True, exist_ok=True)
        lock_path = series_root / cls.LOCK_NAME
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise AttemptBusy(
                        f"series lease held elsewhere: {series_root}"
                    ) from None
                time.sleep(poll_interval)
        token_path = series_root / cls.FENCE_NAME
        last = _read_json(token_path, {"last_token": 0}).get("last_token", 0)
        if not isinstance(last, int):
            last = 0
        token = last + 1
        _atomic_write_json(token_path, {"last_token": token})
        detail = {
            "eval_run_id": series_root.name,
            "owner_id": owner_id or str(uuid4()),
            "fencing_token": token,
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (c.canonical_json(detail) + "\n").encode("utf-8"))
        os.fsync(fd)
        return cls(
            series_root, owner_id=detail["owner_id"], fencing_token=token, _fd=fd
        )

    def ensure_current(self) -> None:
        """受保护写操作前调用；token 过期（被接管/已释放）→ FencingError。"""
        if self._closed:
            raise FencingError("lease already released")
        current = _read_json(self.series_root / self.FENCE_NAME, {}).get("last_token")
        if current != self.fencing_token:
            raise FencingError(
                f"fencing token {self.fencing_token} is stale; current token is {current}"
            )

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> Self:
        self.ensure_current()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ─────────────────────────────────────────────────────────────────────────────
# SelectedPointer：expected-revision CAS + digest 链验证
# ─────────────────────────────────────────────────────────────────────────────


class SelectedPointer:
    """series 根下的唯一 publish pointer（§3.1 / §1.2-2）。

    发布前校验：final ledger 存在且 terminal=SUCCEEDED、manifest/ledger/report
    digest 链一致、ledger.artifact_refs 逐个 verified read。CAS 要求 expected
    revision 与当前指针一致；冲突不产生任何 artifact 变更（§3.5）。
    publish 必须在当前 AttemptLease/fencing token 下执行（§1.2-3 / §3.3-4）：
    无 lease 或 token 过期直接 FencingError，绝不写指针。
    """

    def __init__(self, series_root: Path):
        self.series_root = Path(series_root).resolve()
        self.path = self.series_root / "selected_attempt.json"

    def read(self) -> c.SelectedAttempt | None:
        if not self.path.exists():
            return None
        return c.SelectedAttempt.model_validate(
            json.loads(self.path.read_text("utf-8"))
        )

    def publish(
        self,
        store: ArtifactStore,
        selected: c.SelectedAttempt,
        expected_revision: int | None,
        *,
        lease: AttemptLease | None,
    ) -> c.SelectedAttempt:
        if lease is None:
            raise FencingError("pointer publication requires a current fencing lease")
        lease.ensure_current()
        current = self.read()
        if expected_revision is None:
            if current is not None:
                raise PublishConflict(
                    f"pointer already published at revision {current.revision}"
                )
            new_rev = 0
        else:
            if current is None:
                raise PublishConflict("pointer absent but expected_revision provided")
            if current.revision != expected_revision:
                raise PublishConflict(
                    f"pointer revision {current.revision} != expected {expected_revision}"
                )
            new_rev = expected_revision + 1
        if selected.revision != new_rev:
            raise PublishConflict(
                f"selected.revision {selected.revision} != expected new revision {new_rev}"
            )
        self._verify_chain(store, selected)
        self.series_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, selected.model_dump(mode="json"))
        return selected

    def _verify_chain(self, store: ArtifactStore, selected: c.SelectedAttempt) -> None:
        ledger = store.read_final_ledger()
        if ledger is None:
            raise VerificationError("cannot publish: final ledger missing")
        if ledger.terminal_status is not c.WorkflowStatus.SUCCEEDED:
            raise VerificationError(
                f"cannot publish: terminal status {ledger.terminal_status.value} is not SUCCEEDED"
            )
        manifest = store.read_input_manifest()
        if manifest.digest != selected.input_manifest_digest:
            raise VerificationError("input manifest digest mismatch on disk")
        if ledger.input_manifest_digest != selected.input_manifest_digest:
            raise VerificationError(
                "ledger input_manifest_digest mismatch with selected"
            )
        if ledger.digest() != selected.final_ledger_digest:
            raise VerificationError("final ledger digest mismatch on disk")
        if ledger.report_digest != selected.report_digest:
            raise VerificationError("ledger report_digest mismatch with selected")
        if not ledger.artifact_refs:
            raise VerificationError("ledger has no artifact refs")
        for ref in ledger.artifact_refs.values():
            store.read_verified(ref)
        if not any(
            r.sha256 == selected.report_digest for r in ledger.artifact_refs.values()
        ):
            raise VerificationError("no artifact ref matches selected.report_digest")

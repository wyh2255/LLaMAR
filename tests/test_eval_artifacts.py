"""P1 artifact/audit/lease/pointer 单测（设计 §3.1-3.5 / §1.2；P1 exit）。

覆盖：attempt-root containment（bad path / symlink / hardlink 逃逸）、verified read 与
atomic canonical 写入、source 白名单快照与 redaction、immutable input manifest / final
ledger、selected pointer CAS + digest 链验证、篡改阻断发布、AuditJournal 严格序列与
torn-tail recovery、resume 不重置序号、以及两进程 lease 竞争恰好一个 winner。

不调用任何真实 LLM/provider；不使用 workflow / CLI。
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sar_orch.eval import artifacts as a
from sar_orch.eval import audit as au
from sar_orch.eval import contracts as c
from sar_orch.eval import workflow as w
from sar_orch.eval.aggregate import aggregate_attempts

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")


# ─────────────────────────────────────────────────────────────────────────────
# 构造辅助
# ─────────────────────────────────────────────────────────────────────────────


def _ts() -> datetime:
    return datetime(2026, 8, 6, 0, 42, 10, tzinfo=UTC)


def _artref(path, sha, size=12):
    return c.ArtifactRef(
        path=path,
        sha256=sha,
        bytes=size,
        media_type="application/json",
        producer="test",
    )


def _frozen_manifest() -> c.FrozenInputManifest:
    manifest = c.InputManifest(
        eval_run_id=uuid4(),
        attempt_id=uuid4(),
        created_at=_ts(),
        subject=c.SubjectRef(
            source_run_ref=_artref("subject/run_ref.json", "f" * 64),
            source_input_manifest_ref=_artref(
                "evidence/source_run_manifest.json", "e" * 64
            ),
            source_digest="d" * 64,
            scene=1,
            agents=2,
            seed=42,
            code_commit="339fc3b",
            git_dirty=False,
        ),
        evaluator=c.EvaluatorSpec(
            workflow_version="0.1.0", source_tree_digest="9" * 64
        ),
        policy=c.PolicySpec(
            llm_judge_required=False,
            audit_level=c.AuditLevel.STANDARD,
            retry=2,
            timeout_s=60,
            concurrency=2,
            retention_days=30,
            merge_policy_digest="7" * 64,
        ),
        roles=[
            c.RoleConfig(
                role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
                agent_id="dispatch_score_judge",
                prompt_ref=_artref("snapshots/prompts/dispatch.md", "b" * 64),
                prompt_digest="b" * 64,
                model_profile_ref=_artref(
                    "snapshots/model_profiles/score.json", "c" * 64
                ),
                model_profile_digest="c" * 64,
                tool_schema_ref=_artref("snapshots/tool_schemas/score.json", "d" * 64),
                tool_schema_digest="d" * 64,
            )
        ],
        rubrics=[
            c.RubricSpec(
                rubric_id="dispatch-v1",
                version="1.0.0",
                digest="e" * 64,
                target_type=c.SampleTargetType.DISPATCH,
                input_selector="dispatch_samples",
                dimensions=["pass_rate", "hallucination_rate"],
                prompt_template_ref=_artref(
                    "snapshots/rubrics/dispatch-v1.yaml", "f" * 64
                ),
                judge_role=c.JudgeRole.DISPATCH_SCORE_JUDGE,
                merge_group="dispatch",
                weight=1.0,
            )
        ],
        command=c.CommandSpec(
            argv_without_secrets=["eval", "--no-llm-judge"],
            cwd="/tmp",
            env_allowlist=["PATH"],
        ),
    )
    return manifest.freeze()


def _ledger(
    manifest_digest, report_ref, terminal=c.WorkflowStatus.SUCCEEDED
) -> c.FinalLedger:
    return c.FinalLedger(
        input_manifest_digest=manifest_digest,
        terminal_status=terminal,
        finalized_at=_ts(),
        artifact_refs={"reports/eval_report.json": report_ref},
        report_digest=report_ref.sha256,
        judge_execution_status=c.JudgeExecutionStatus.NOT_REQUESTED,
    )


def _selected(
    frozen, ledger, report_ref, revision=0, attempt_id=None, eval_run_id=None
) -> c.SelectedAttempt:
    return c.SelectedAttempt(
        eval_run_id=eval_run_id or frozen.manifest.eval_run_id,
        attempt_id=attempt_id or frozen.manifest.attempt_id,
        input_manifest_digest=frozen.digest,
        final_ledger_digest=ledger.digest(),
        report_digest=report_ref.sha256,
        revision=revision,
    )


def _make_attempt(tmp_path) -> tuple[a.ArtifactStore, Path]:
    attempt_root = tmp_path / "eval_attempts" / str(uuid4()) / "attempts" / str(uuid4())
    store = a.ArtifactStore(attempt_root)
    return store, attempt_root


def _publishable(
    tmp_path,
) -> tuple[a.ArtifactStore, Path, c.FrozenInputManifest, c.FinalLedger, c.ArtifactRef]:
    store, _ = _make_attempt(tmp_path)
    frozen = _frozen_manifest()
    store.write_input_manifest(frozen)
    report_ref = store.write_atomic(
        "reports/eval_report.json", b'{"summary": "ok"}', producer="renderer"
    )
    ledger = _ledger(frozen.digest, report_ref)
    store.write_final_ledger(ledger)
    return store, _series_root(tmp_path), frozen, ledger, report_ref


def _series_root(tmp_path) -> Path:
    return tmp_path / "eval_attempts" / str(uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# containment：bad paths / symlink / hardlink 逃逸
# ─────────────────────────────────────────────────────────────────────────────


class TestContainment:
    def test_rejects_parent_traversal(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        with pytest.raises(a.ContainmentError):
            store.write_atomic("../escape.txt", b"x")

    def test_rejects_absolute_path(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        with pytest.raises(a.ContainmentError):
            store.write_atomic("/etc/passwd", b"x")

    def test_rejects_symlink_escape(self, tmp_path):
        store, root = _make_attempt(tmp_path)
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"TOP SECRET")
        os.symlink(outside, root / "leak.json")
        with pytest.raises(a.ContainmentError):
            store.read_bytes("leak.json")
        with pytest.raises(a.ContainmentError):
            store.write_atomic("leak.json", b"x")

    def test_rejects_hardlink_escape(self, tmp_path):
        store, root = _make_attempt(tmp_path)
        outside = tmp_path / "outside.csv"
        outside.write_bytes(b"legacy data")
        os.link(outside, root / "evil.json")
        with pytest.raises(a.ContainmentError):
            store.read_bytes("evil.json")
        with pytest.raises(a.ContainmentError):
            store.write_atomic("evil.json", b"x")

    def test_rejects_symlinked_attempt_root(self, tmp_path):
        real = tmp_path / "real_root"
        real.mkdir()
        link = tmp_path / "root_link"
        os.symlink(real, link)
        with pytest.raises(a.ContainmentError):
            a.ArtifactStore(link)
        assert not (link / ".written").exists()

    def test_rejects_symlinked_attempt_root_ancestor(self, tmp_path):
        real = tmp_path / "real_series"
        real.mkdir()
        alias = tmp_path / "alias_series"
        os.symlink(real, alias)
        attempt = alias / "attempts" / str(uuid4())
        with pytest.raises(a.ContainmentError):
            a.ArtifactStore(attempt)
        assert not (real / "attempts").exists()  # 验证失败不得在真实位置创建任何文件

    def test_valid_nested_path_roundtrip(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        ref = store.write_canonical_json("evidence/steps/s1.json", {"step": 1})
        assert store.read_verified(ref) == b'{"step":1}\n'
        assert store.read_text("evidence/steps/s1.json") == '{"step":1}\n'


# ─────────────────────────────────────────────────────────────────────────────
# atomic write / verified read
# ─────────────────────────────────────────────────────────────────────────────


class TestAtomicWriteAndVerifiedRead:
    def test_atomic_write_leaves_no_temp_files(self, tmp_path):
        store, root = _make_attempt(tmp_path)
        store.write_atomic(
            "a/b/c.bin", b"\x00\x01\x02", media_type="application/octet-stream"
        )
        leftovers = [p for p in root.rglob("*") if p.name.startswith(".tmp-")]
        assert leftovers == []
        assert store.read_bytes("a/b/c.bin") == b"\x00\x01\x02"

    def test_verified_read_detects_tampering(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        ref = store.write_atomic("evidence/x.json", b'{"a":1}')
        assert store.read_verified(ref) == b'{"a":1}'
        (store.root / "evidence" / "x.json").write_bytes(b'{"a":2}')
        with pytest.raises(a.VerificationError):
            store.read_verified(ref)

    def test_write_canonical_json_is_stable(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        ref1 = store.write_canonical_json("data.json", {"b": 1, "a": [1, 2]})
        ref2 = store.write_canonical_json("data.json", {"a": [1, 2], "b": 1})
        assert ref1.sha256 == ref2.sha256
        assert json.loads(store.read_text("data.json")) == {"a": [1, 2], "b": 1}


# ─────────────────────────────────────────────────────────────────────────────
# redaction + source 白名单快照
# ─────────────────────────────────────────────────────────────────────────────


class TestRedaction:
    def test_redact_thinking_and_secret(self):
        text = (
            "### Thinking\n"
            "assistant should silently reason here\n"
            "<thinking>hidden CoT</thinking>\n"
            "api_key=sk-abc123\n"
            "api_base=https://example.com/v1\n"
            "normal = fine\n"
        )
        red, reasons = a.redact_text(text)
        assert "sk-abc123" not in red
        assert "hidden CoT" not in red
        assert "[Thinking redacted]" in red
        assert "[REDACTED]" in red
        assert "api_base=https://example.com/v1" not in red
        assert "normal = fine" in red
        assert "thinking_block" in reasons
        assert "secret_value" in reasons

    def test_snapshot_allowlist_exclusion_and_redaction(self, tmp_path):
        src = tmp_path / "source_run"
        src.mkdir(parents=True)
        (src / "trajectory.csv").write_text("step,successes\n1,True\n")
        (src / "metadata.json").write_text('{"api_key": "sk-topsecret", "seed": 42}')
        (src / "agent_interactions.csv").write_text("step,agent\n")
        (src / "eval_workspace").mkdir()
        (src / "eval_workspace" / "conclusion.md").write_text("x")
        (src / "eval_attempts").mkdir()
        (src / "eval_report.json").write_text('{"family": "legacy"}')
        (src / "eval_report.md").write_text("# old")

        snap = a.snapshot_source_files(
            src,
            [
                "trajectory.csv",
                "metadata.json",
                "agent_interactions.csv",
                "eval_workspace/conclusion.md",
                "eval_report.json",
            ],
        )
        assert set(snap.files) == {
            "trajectory.csv",
            "metadata.json",
            "agent_interactions.csv",
        }
        assert "eval_workspace/conclusion.md" in snap.excluded
        assert "eval_report.json" in snap.excluded
        assert "metadata.json" in snap.redacted
        assert snap.files["trajectory.csv"] == c.sha256_hex(b"step,successes\n1,True\n")
        assert len(snap.digest) == 64

    def test_snapshot_rejects_symlink_escape(self, tmp_path):
        src = tmp_path / "source_run"
        src.mkdir()
        outside = tmp_path / "outside.csv"
        outside.write_bytes(b"x")
        os.symlink(outside, src / "trajectory.csv")
        with pytest.raises(a.ContainmentError):
            a.snapshot_source_files(src, ["trajectory.csv"])


# ─────────────────────────────────────────────────────────────────────────────
# immutable input manifest / final ledger
# ─────────────────────────────────────────────────────────────────────────────


class TestImmutableManifestAndLedger:
    def test_manifest_immutable_and_idempotent(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        frozen = _frozen_manifest()
        ref1 = store.write_input_manifest(frozen)
        ref2 = store.write_input_manifest(frozen)
        assert ref1.sha256 == ref2.sha256
        assert ref1.sha256 == frozen.digest
        other = _frozen_manifest()
        with pytest.raises(a.ImmutableArtifactError):
            store.write_input_manifest(other)
        assert store.read_input_manifest().digest == frozen.digest

    def test_manifest_tampering_detected(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        store.write_input_manifest(_frozen_manifest())
        path = store.path("input_manifest.json")
        data = bytearray(path.read_bytes())
        data[10] ^= 0xFF
        path.write_bytes(bytes(data))
        with pytest.raises(a.VerificationError):
            store.read_input_manifest()

    def test_ledger_immutable(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        frozen = _frozen_manifest()
        store.write_input_manifest(frozen)
        report_ref = store.write_atomic(
            "reports/eval_report.json", b'{"x":1}', producer="renderer"
        )
        ledger = _ledger(frozen.digest, report_ref)
        store.write_final_ledger(ledger)
        assert store.write_final_ledger(ledger).sha256 == ledger.digest()
        with pytest.raises(a.ImmutableArtifactError):
            store.write_final_ledger(
                _ledger(frozen.digest, report_ref, terminal=c.WorkflowStatus.FAILED)
            )

    def test_ledger_read_roundtrip(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        frozen = _frozen_manifest()
        store.write_input_manifest(frozen)
        report_ref = store.write_atomic(
            "reports/eval_report.json", b'{"x":1}', producer="renderer"
        )
        ledger = _ledger(frozen.digest, report_ref)
        store.write_final_ledger(ledger)
        read_back = store.read_final_ledger()
        assert read_back is not None
        assert read_back.digest() == ledger.digest()
        assert read_back.terminal_status is c.WorkflowStatus.SUCCEEDED


# ─────────────────────────────────────────────────────────────────────────────
# selected pointer CAS + digest 链 / 篡改阻断发布
# ─────────────────────────────────────────────────────────────────────────────


class TestSelectedPointer:
    def test_publish_happy_path(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        pointer = a.SelectedPointer(series)
        selected = _selected(frozen, ledger, report_ref)
        with a.AttemptLease.acquire(series, owner_id="publisher") as lease:
            pointer.publish(store, selected, None, lease=lease)
        read_back = pointer.read()
        assert read_back is not None
        assert read_back.revision == 0

    def test_publish_rejects_non_succeeded(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        series = _series_root(tmp_path)
        frozen = _frozen_manifest()
        store.write_input_manifest(frozen)
        report_ref = store.write_atomic(
            "reports/eval_report.json", b'{"x":1}', producer="renderer"
        )
        store.write_final_ledger(
            _ledger(frozen.digest, report_ref, terminal=c.WorkflowStatus.FAILED)
        )
        selected = _selected(frozen, store.read_final_ledger(), report_ref)
        with (
            a.AttemptLease.acquire(series, owner_id="publisher") as lease,
            pytest.raises(a.VerificationError),
        ):
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)
        assert not (series / "selected_attempt.json").exists()

    def test_publish_requires_fencing_lease(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        selected = _selected(frozen, ledger, report_ref)
        with pytest.raises(a.FencingError):
            a.SelectedPointer(series).publish(store, selected, None, lease=None)
        assert not (series / "selected_attempt.json").exists()

    def test_publish_cas_revision_conflicts(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        pointer = a.SelectedPointer(series)
        selected0 = _selected(frozen, ledger, report_ref, revision=0)
        selected1 = _selected(frozen, ledger, report_ref, revision=1)
        with a.AttemptLease.acquire(series, owner_id="publisher") as lease:
            pointer.publish(store, selected0, None, lease=lease)
            with pytest.raises(a.PublishConflict):
                pointer.publish(
                    store, selected1, None, lease=lease
                )  # already published
            with pytest.raises(a.PublishConflict):
                pointer.publish(
                    store, selected1, 5, lease=lease
                )  # wrong expected revision
            assert (
                pointer.publish(store, selected1, 0, lease=lease).revision == 1
            )  # expected 0 -> 1
        read_back = pointer.read()
        assert read_back is not None
        assert read_back.revision == 1

    def test_publish_blocks_when_ledger_tampered(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        path = store.path("audit/final_ledger.json")
        data = bytearray(path.read_bytes())
        data[5] ^= 0xFF
        path.write_bytes(bytes(data))
        selected = _selected(frozen, ledger, report_ref)
        with (
            a.AttemptLease.acquire(series, owner_id="publisher") as lease,
            pytest.raises(a.VerificationError),
        ):
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)
        assert not (series / "selected_attempt.json").exists()

    def test_publish_blocks_when_manifest_tampered(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        path = store.path("input_manifest.json")
        data = bytearray(path.read_bytes())
        data[20] ^= 0xFF
        path.write_bytes(bytes(data))
        selected = _selected(frozen, ledger, report_ref)
        with (
            a.AttemptLease.acquire(series, owner_id="publisher") as lease,
            pytest.raises(a.VerificationError),
        ):
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)
        assert not (series / "selected_attempt.json").exists()

    def test_publish_blocks_when_report_tampered(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        (store.root / "reports" / "eval_report.json").write_bytes(b'{"tampered": true}')
        selected = _selected(frozen, ledger, report_ref)
        with (
            a.AttemptLease.acquire(series, owner_id="publisher") as lease,
            pytest.raises(a.VerificationError),
        ):
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)

    def test_publish_requires_ledger_refs_match_report_digest(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        series = _series_root(tmp_path)
        frozen = _frozen_manifest()
        store.write_input_manifest(frozen)
        store.write_atomic("reports/eval_report.json", b'{"x":1}', producer="renderer")
        bogus_ref = _artref("reports/eval_report.json", "1" * 64, size=11)
        store.write_final_ledger(_ledger(frozen.digest, bogus_ref))
        selected = _selected(frozen, store.read_final_ledger(), bogus_ref)
        with (
            a.AttemptLease.acquire(series, owner_id="publisher") as lease,
            pytest.raises(a.VerificationError),
        ):
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)

    def test_publish_under_lease(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        lease = a.AttemptLease.acquire(series, owner_id="publisher")
        selected = _selected(frozen, ledger, report_ref)
        a.SelectedPointer(series).publish(store, selected, None, lease=lease)
        with pytest.raises(a.AttemptBusy):
            a.AttemptLease.acquire(series, owner_id="sneaky")  # 租约仍被持有
        lease.release()
        lease2 = a.AttemptLease.acquire(series, owner_id="sneaky")
        assert lease2.fencing_token == lease.fencing_token + 1
        lease2.release()


# ─────────────────────────────────────────────────────────────────────────────
# AttemptLease：flock + fencing token
# ─────────────────────────────────────────────────────────────────────────────


class TestAttemptLease:
    def test_in_process_second_acquire_busy_and_fence_increments(self, tmp_path):
        series = _series_root(tmp_path)
        lease1 = a.AttemptLease.acquire(series, owner_id="a")
        with pytest.raises(a.AttemptBusy):
            a.AttemptLease.acquire(series, owner_id="b")
        lease1.release()
        lease2 = a.AttemptLease.acquire(series, owner_id="b")
        assert lease2.fencing_token == lease1.fencing_token + 1
        lease2.release()

    def test_stale_lease_fencing_detected(self, tmp_path):
        series = _series_root(tmp_path)
        lease1 = a.AttemptLease.acquire(series, owner_id="a")
        lease1.release()
        lease2 = a.AttemptLease.acquire(series, owner_id="b")
        assert lease2.fencing_token > lease1.fencing_token
        with pytest.raises(a.FencingError):
            lease1.ensure_current()
        with pytest.raises(a.FencingError):
            lease1.__enter__()
        lease2.release()
        with pytest.raises(a.FencingError):
            lease2.ensure_current()

    def test_timeout_acquire_waits(self, tmp_path):
        series = _series_root(tmp_path)
        holder = a.AttemptLease.acquire(series, owner_id="a")
        import time

        start = time.monotonic()
        with pytest.raises(a.AttemptBusy):
            a.AttemptLease.acquire(series, owner_id="b", timeout=0.3)
        assert time.monotonic() - start >= 0.3
        holder.release()

    def test_subprocess_competition_exactly_one_winner(self, tmp_path):
        series = tmp_path / "eval_attempts" / str(uuid4())
        worker = tmp_path / "lease_worker.py"
        worker.write_text(
            "import json, sys, time\n"
            "sys.path.insert(0, " + repr(SRC_DIR) + ")\n"
            "from sar_orch.eval.artifacts import AttemptBusy, AttemptLease\n"
            "try:\n"
            "    lease = AttemptLease.acquire(sys.argv[1], owner_id='worker')\n"
            "except AttemptBusy:\n"
            "    print('BUSY', flush=True)\n"
            "    sys.exit(0)\n"
            "print('WIN', lease.fencing_token, lease.owner_id, flush=True)\n"
            "time.sleep(2)\n"
            "lease.release()\n",
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": SRC_DIR}
        procs = [
            subprocess.Popen(
                [sys.executable, str(worker), str(series)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(4)
        ]
        outs = [p.communicate(timeout=30) for p in procs]
        winners = [o[0].strip() for o in outs if o[0].strip().startswith("WIN")]
        busy = [o[0].strip() for o in outs if o[0].strip().startswith("BUSY")]
        assert len(winners) == 1
        assert len(busy) == 3
        token = int(winners[0].split()[1])
        assert token == 1
        lease = a.AttemptLease.acquire(series, owner_id="parent")
        assert lease.fencing_token == 2
        lease.release()


# ─────────────────────────────────────────────────────────────────────────────
# AuditJournal：严格序列、resume、torn-tail recovery
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditJournal:
    def test_append_and_tail_seq(self, tmp_path):
        journal = au.AuditJournal(tmp_path / "events.jsonl")
        assert journal.append({"kind": "admitted"}) == 1
        assert journal.append({"kind": "manifest_frozen"}) == 2
        assert journal.tail_seq() == 2
        events = journal.read_all()
        assert [e["seq"] for e in events] == [1, 2]
        assert events[0]["schema_version"] == 1

    def test_resume_does_not_reset_sequence(self, tmp_path):
        path = tmp_path / "events.jsonl"
        j1 = au.AuditJournal(path)
        j1.append({"kind": "x"})
        j1.append({"kind": "y"})
        j2 = au.AuditJournal(path)  # fresh instance resumes from committed tail
        assert j2.append({"kind": "z"}) == 3
        assert [e["seq"] for e in j2.read_all()] == [1, 2, 3]
        assert len(j2.read_all()) == 3

    def test_recover_drops_torn_tail_and_reports_diagnostic(self, tmp_path):
        path = tmp_path / "events.jsonl"
        j1 = au.AuditJournal(path)
        j1.append({"kind": "a"})
        j1.append({"kind": "b"})
        with open(path, "ab") as fh:
            fh.write(b'{"seq":3,"schema_version":1,"kind":"torn"')  # 未终结
        j2 = au.AuditJournal(path)
        report = j2.recover()
        assert report.dropped_lines == 1
        assert report.dropped_bytes > 0
        assert "torn" in report.reason
        assert report.tail_seq == 2
        assert report.valid_lines == 2
        assert b"torn" not in path.read_bytes()  # 已物理截断
        assert j2.append({"kind": "c"}) == 3
        assert [e["seq"] for e in j2.read_all()] == [1, 2, 3]

    def test_strict_sequence_violation_rejected(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps({"seq": 1, "schema_version": 1})
            + "\n"
            + json.dumps({"seq": 3, "schema_version": 1})
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(au.CorruptJournalError):
            au.AuditJournal(path).read_all()

    def test_duplicate_sequence_rejected(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps({"seq": 1, "schema_version": 1})
            + "\n"
            + json.dumps({"seq": 1, "schema_version": 1})
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(au.CorruptJournalError):
            au.AuditJournal(path).read_all()

    def test_empty_journal(self, tmp_path):
        journal = au.AuditJournal(tmp_path / "events.jsonl")
        assert journal.read_all() == []
        assert journal.tail_seq() == 0
        assert journal.event_count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# P3-M5：audit/checkpoint 持久化面必须受 attempt-root containment 约束
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditJournalContainment:
    def test_audit_journal_rejects_symlinked_audit_dir(self, tmp_path):
        store, root = _make_attempt(tmp_path)
        outside = tmp_path / "outside_audit"
        outside.mkdir()
        (root / "audit").symlink_to(outside, target_is_directory=True)
        with pytest.raises(a.ContainmentError):
            store.audit_journal()
        assert not (outside / "events.jsonl").exists()

    def test_audit_journal_rejects_hardlinked_events_file(self, tmp_path):
        store, root = _make_attempt(tmp_path)
        (root / "audit").mkdir()
        outside = tmp_path / "events.out"
        outside.write_bytes(b"")
        os.link(outside, root / "audit" / "events.jsonl")
        with pytest.raises(a.ContainmentError):
            store.audit_journal()
        assert outside.read_bytes() == b""

    def test_audit_journal_normal_path_recovers(self, tmp_path):
        store, _ = _make_attempt(tmp_path)
        journal = store.audit_journal()
        journal.append({"kind": "admitted"})
        assert store.audit_journal().read_all()[0]["kind"] == "admitted"


# ─────────────────────────────────────────────────────────────────────────────
# 端到端：完整 attempt → journal → ledger → pointer，篡改 source 阻断发布
# ─────────────────────────────────────────────────────────────────────────────


class TestEndToEndPublish:
    def test_full_chain_and_source_tamper_detection(self, tmp_path):
        store, series, frozen, ledger, report_ref = _publishable(tmp_path)
        journal = store.audit_journal()
        assert journal.append({"kind": "ledger_finalized"}) == 1

        # source 快照：verify raw digest；篡改 source 后 digest 必须失效（阻断发布链）
        src = tmp_path / "source_run"
        src.mkdir()
        (src / "trajectory.csv").write_bytes(b"step,successes\n1,True\n")
        snap = a.snapshot_source_files(src, ["trajectory.csv"])
        expected = snap.files["trajectory.csv"]
        assert expected == c.sha256_hex(b"step,successes\n1,True\n")
        (src / "trajectory.csv").write_bytes(b"step,successes\n1,False\n")
        assert c.sha256_hex((src / "trajectory.csv").read_bytes()) != expected

        selected = _selected(frozen, ledger, report_ref)
        pointer = a.SelectedPointer(series)
        with a.AttemptLease.acquire(series, owner_id="publisher") as lease:
            pointer.publish(store, selected, None, lease=lease)
        read_back = pointer.read()
        assert read_back is not None
        assert read_back.revision == 0
        assert journal.tail_seq() == 1


# ─────────────────────────────────────────────────────────────────────────────
# P5：attempt-family report / attempt aggregate / CLI exit mapping
# （设计 §7.1-7.3、§9 P5）
# ─────────────────────────────────────────────────────────────────────────────


def _attempt_report_payload(
    frozen,
    *,
    scene=1,
    agents=2,
    seed=42,
    coverage=0.9,
    transport=0.8,
    family="attempt-v2",
    semantics: "str | None" = "attempt-stream-v1",
):
    payload = {
        "report_family": family,
        "eval_semantics_version": semantics,
        "eval_attempt": f"{frozen.manifest.eval_run_id}/{frozen.manifest.attempt_id}",
        "input_manifest_digest": frozen.digest,
        "judge_execution_status": "not_requested",
        "metadata": {
            "scene": scene,
            "agents": agents,
            "seed": seed,
            "eval_semantics_version": semantics,
            "report_family": family,
        },
        "episode": {
            "coverage": coverage,
            "transport_rate": transport,
            "finished": True,
        },
    }
    if semantics is None:
        del payload["metadata"]["eval_semantics_version"]
        del payload["eval_semantics_version"]
    return payload


def _publish_attempt(
    tmp_path,
    *,
    scene=1,
    agents=2,
    seed=42,
    coverage=0.9,
    publish=True,
    family="attempt-v2",
    semantics: "str | None" = "attempt-stream-v1",
    eval_run_id=None,
    report_identity=None,
    drop_report_eval_attempt=False,
    drop_report_manifest_digest=False,
):
    """构造一个可发布的 attempt（manifest + attempt report + SUCCEEDED ledger），
    可选发布 selected pointer。

    - attempt 目录名必须等于 manifest.attempt_id，series 目录名必须等于
      manifest.eval_run_id（否则 aggregate 无法从 pointer 定位 attempt root，
      也会被 series 身份检查拒绝）。
    - `eval_run_id`：覆盖 series 目录名与 pointer 的 eval_run_id（≠ manifest 时
      构造"身份不符"的伪造场景）。
    - `report_identity`：覆盖 report 的 `eval_attempt` 字段（构造 report 身份
      不符的伪造场景）。
    - `drop_report_eval_attempt` / `drop_report_manifest_digest`：删除 report 的
      对应必发字段（构造缺失字段的 incompatible 场景）。
    """
    frozen = _frozen_manifest()
    identity_eval_run_id = (
        eval_run_id if eval_run_id is not None else frozen.manifest.eval_run_id
    )
    series = tmp_path / "eval_attempts" / str(identity_eval_run_id)
    attempt_root = series / "attempts" / str(frozen.manifest.attempt_id)
    store = a.ArtifactStore(attempt_root)
    store.write_input_manifest(frozen)
    payload = _attempt_report_payload(
        frozen,
        scene=scene,
        agents=agents,
        seed=seed,
        coverage=coverage,
        family=family,
        semantics=semantics,
    )
    if report_identity is not None:
        payload["eval_attempt"] = report_identity
    if drop_report_eval_attempt:
        payload.pop("eval_attempt", None)
    if drop_report_manifest_digest:
        payload.pop("input_manifest_digest", None)
    report_ref = store.write_canonical_json(
        "reports/eval_report.json", payload, producer="renderer"
    )
    ledger = _ledger(frozen.digest, report_ref)
    store.write_final_ledger(ledger)
    if publish:
        selected = _selected(
            frozen, ledger, report_ref, eval_run_id=identity_eval_run_id
        )
        with a.AttemptLease.acquire(series, owner_id="publisher") as lease:
            a.SelectedPointer(series).publish(store, selected, None, lease=lease)
    return store, series, frozen, ledger, report_ref


def _make_results_dir(root: Path) -> Path:
    """与 test_eval_resume.py 同构的最小可运行 results dir（workflow adapter 用）。"""
    rd = root / "results"
    rd.mkdir(parents=True)
    (rd / "metadata.json").write_text(
        json.dumps(
            {
                "scene": 1,
                "agent_count": 2,
                "seed": 42,
                "model": "deepseek-v4-flash",
                "code_commit": "x",
                "git_dirty": False,
                "agent_names": ["Alice", "Bob"],
            }
        )
    )
    (rd / "trajectory.csv").write_text(
        "Step,Coverage,TransportRate,Finished\n"
        "1,0.5,0.0,false\n2,0.6,0.1,false\n3,0.7,0.2,false\n"
    )
    (rd / "router_interactions.csv").write_text(
        "Step,Subtask,AssignedTo,CorrelationID,WorkerTaskID,EventType\n"
    )
    (rd / "subtasks.csv").write_text(
        "Step,SubtaskID,Status,AssignedTo,Subtask,FailureClass\n"
    )
    (rd / "agent_interactions.csv").write_text(
        "Step,Agent,ToolName,ToolArgs,Action,Observation\n"
        '2,Alice,report_observation,"{}",report_observation(),report_observation ok\n'
    )
    (rd / "summary.csv").write_text("Scene,Agents,Seed\n1,2,42\n")
    (rd / "token_usage.csv").write_text("Step,Agent,TotalTokens\n")
    (rd / "semantic_map.jsonl").write_text("")
    (rd / "map_summary.jsonl").write_text("")
    return rd


def _adapter_args(rd: Path, **overrides) -> SimpleNamespace:
    base = {
        "results_dir": str(rd),
        "no_llm_judge": True,
        "judge_sample_steps": 20,
        "eval_run_id": None,
        "attempt_id": None,
        "resume_attempt": None,
        "attempt_root": None,
        "output": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestAttemptReportDigestChain:
    def test_report_ledger_pointer_chain(self, tmp_path):
        """最终 report/ledger/pointer 三方 digest 必须一致（§1.2-2 / §3.5）。"""
        store, series, frozen, ledger, report_ref = _publish_attempt(tmp_path)
        pointer = a.SelectedPointer(series).read()
        assert pointer is not None
        report_bytes = store.read_bytes("reports/eval_report.json")
        assert c.sha256_hex(report_bytes) == pointer.report_digest
        assert pointer.report_digest == report_ref.sha256
        assert pointer.report_digest == ledger.report_digest
        assert store.read_final_ledger().digest() == pointer.final_ledger_digest
        assert frozen.digest == pointer.input_manifest_digest

    def test_tampered_report_fails_verified_read(self, tmp_path):
        store, _, _, _, report_ref = _publish_attempt(tmp_path)
        (store.root / "reports" / "eval_report.json").write_bytes(b'{"x":1}')
        with pytest.raises(a.VerificationError):
            store.read_verified(report_ref)


class TestAttemptAggregate:
    def test_reads_only_selected_attempts(self, tmp_path):
        """只读 selected_attempt.json：未发布的 attempt 记入 skipped（not selected）。"""
        _publish_attempt(tmp_path, scene=1, agents=2)
        _publish_attempt(tmp_path, scene=1, agents=2, publish=False)
        agg = aggregate_attempts(tmp_path)
        assert agg["report_family"] == "attempt-v2"
        assert agg["valid_run_dirs"] == 1
        assert len(agg["groups"]) == 1
        assert any("not selected" in s for s in agg["skipped_dirs"])

    def test_groups_by_scene_agents(self, tmp_path):
        _publish_attempt(tmp_path, scene=1, agents=2)
        _publish_attempt(tmp_path, scene=2, agents=4)
        agg = aggregate_attempts(tmp_path)
        assert agg["num_groups"] == 2
        keys = {(g["key"]["scene"], g["key"]["agents"]) for g in agg["groups"]}
        assert keys == {(1, 2), (2, 4)}
        # 逐指标聚合（episode 投影与 legacy aggregate_group 同源）
        g = next(g for g in agg["groups"] if g["key"] == {"scene": 1, "agents": 2})
        assert g["episode_stats"]["coverage"]["mean"] == pytest.approx(0.9)

    def test_corrupt_pointer_skipped(self, tmp_path):
        series = _series_root(tmp_path)
        (series / "attempts").mkdir(parents=True)
        (series / "selected_attempt.json").write_text("{not json", encoding="utf-8")
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("corrupt pointer" in s for s in agg["skipped_dirs"])

    def test_tampered_manifest_breaks_chain_and_skips(self, tmp_path):
        store, _, _, _, _ = _publish_attempt(tmp_path)
        path = store.path("input_manifest.json")
        data = bytearray(path.read_bytes())
        data[10] ^= 0xFF
        path.write_bytes(bytes(data))
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("corrupt digest chain" in s for s in agg["skipped_dirs"])

    def test_tampered_report_breaks_chain_and_skips(self, tmp_path):
        store, _, _, _, _ = _publish_attempt(tmp_path)
        (store.root / "reports" / "eval_report.json").write_bytes(b'{"tampered": true}')
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("corrupt digest chain" in s for s in agg["skipped_dirs"])

    def test_missing_semantics_version_skipped_incompatible(self, tmp_path):
        """缺 evaluator semantics version 的 manifest-bound report 不可聚合（§7.2）。"""
        _publish_attempt(tmp_path, semantics=None)
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any(
            "incompatible" in s and "semantics" in s for s in agg["skipped_dirs"]
        )

    def test_wrong_family_skipped_incompatible(self, tmp_path):
        _publish_attempt(tmp_path, family="legacy")
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("report family" in s for s in agg["skipped_dirs"])


class TestAttemptAggregateIdentityGuard:
    """§1.2-1/2、§3.5、§7.2：aggregate 必须 fail-closed 校验完整 selected-attempt
    链 —— 仅 digest 对齐不足以放行，伪造 pointer 身份/非 SUCCEEDED attempt 均拒绝。"""

    def test_forged_pointer_eval_run_id_skipped(self, tmp_path):
        """伪造 pointer 的 eval_run_id 与 series 目录不符 → 拒绝（§3.5）。"""
        _publish_attempt(tmp_path, scene=1, agents=2)
        series = next((tmp_path / "eval_attempts").iterdir())
        pointer = json.loads((series / "selected_attempt.json").read_text("utf-8"))
        pointer["eval_run_id"] = str(uuid4())
        (series / "selected_attempt.json").write_text(
            json.dumps(pointer), encoding="utf-8"
        )
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("identity mismatch" in s for s in agg["skipped_dirs"])

    def test_forged_pointer_manifest_identity_skipped(self, tmp_path):
        """pointer 身份与 frozen manifest 不符（series 名与 pointer 一致，但 manifest
        身份不同）→ 拒绝 —— digest 对齐不能替代身份绑定（§1.2-1 / §7.2）。"""
        _publish_attempt(tmp_path, scene=1, agents=2, eval_run_id=uuid4())
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("frozen input manifest" in s for s in agg["skipped_dirs"])

    def test_non_succeeded_ledger_skipped(self, tmp_path):
        """手动伪造 pointer 指向 CANCELLED attempt（digest 完全对齐）→ 拒绝聚合。

        绕过 SelectedPointer.publish 直接落盘 pointer —— 聚合端必须独立校验
        ledger terminal=SUCCEEDED，否则非发布 attempt 会被混入 pass@k。
        """
        frozen = _frozen_manifest()
        series = tmp_path / "eval_attempts" / str(frozen.manifest.eval_run_id)
        attempt_root = series / "attempts" / str(frozen.manifest.attempt_id)
        store = a.ArtifactStore(attempt_root)
        store.write_input_manifest(frozen)
        payload = _attempt_report_payload(frozen)
        report_ref = store.write_canonical_json(
            "reports/eval_report.json", payload, producer="renderer"
        )
        ledger = _ledger(frozen.digest, report_ref, terminal=c.WorkflowStatus.CANCELLED)
        store.write_final_ledger(ledger)
        selected = _selected(frozen, ledger, report_ref)
        (series / "selected_attempt.json").write_text(
            json.dumps(selected.model_dump(mode="json")), encoding="utf-8"
        )
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("not SUCCEEDED" in s for s in agg["skipped_dirs"])

    def test_report_identity_mismatch_skipped(self, tmp_path):
        """report 的 eval_attempt 身份与 selected pointer 不符 → 拒绝（§7.2）。"""
        forged = f"{uuid4()}/{uuid4()}"
        _publish_attempt(tmp_path, scene=1, agents=2, report_identity=forged)
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any(
            "incompatible" in s and "eval_attempt" in s for s in agg["skipped_dirs"]
        )

    def test_report_missing_eval_attempt_skipped(self, tmp_path):
        """attempt-v2 report 缺失 eval_attempt 身份 → incompatible 跳过（§7.2）。"""
        _publish_attempt(tmp_path, scene=1, agents=2, drop_report_eval_attempt=True)
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("eval_attempt missing" in s for s in agg["skipped_dirs"])

    def test_report_malformed_eval_attempt_skipped(self, tmp_path):
        """attempt-v2 report 的 eval_attempt 非良构 locator → incompatible 跳过。"""
        _publish_attempt(tmp_path, scene=1, agents=2, report_identity="not-a-locator")
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("eval_attempt malformed" in s for s in agg["skipped_dirs"])

    def test_report_missing_manifest_digest_skipped(self, tmp_path):
        """attempt-v2 report 缺失必发 input_manifest_digest → incompatible 跳过。"""
        _publish_attempt(tmp_path, scene=1, agents=2, drop_report_manifest_digest=True)
        agg = aggregate_attempts(tmp_path)
        assert agg["valid_run_dirs"] == 0
        assert any("input_manifest_digest" in s for s in agg["skipped_dirs"])


class TestP5CliExitMapping:
    def test_invalid_resume_locator_exit_2(self, tmp_path):
        """裸 attempt_id / 非 `<uuid>/<uuid>` locator → exit 2（§1.2-1 / §7.1）。"""
        rd = _make_results_dir(tmp_path)
        args = _adapter_args(rd, resume_attempt="not-a-locator")
        assert w.run_from_results_dir(args) == 2
        assert not (rd / "eval_attempts").exists()

    def test_resume_locator_short_circuits(self, tmp_path):
        """合法 `<eval_run_id>/<attempt_id>` → 精确恢复同一 attempt（short-circuit）。"""
        rd = _make_results_dir(tmp_path)
        run_id, attempt_id = uuid4(), uuid4()
        assert (
            w.run_from_results_dir(
                _adapter_args(rd, eval_run_id=run_id, attempt_id=attempt_id)
            )
            == 0
        )
        assert (
            w.run_from_results_dir(
                _adapter_args(rd, resume_attempt=f"{run_id}/{attempt_id}")
            )
            == 0
        )
        store = a.ArtifactStore(
            rd / "eval_attempts" / str(run_id) / "attempts" / str(attempt_id)
        )
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.SUCCEEDED
        events = store.audit_journal().read_all()
        assert events[-1]["kind"] == "pointer_published"

    def test_adapter_publishes_pointer_after_success(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        run_id, attempt_id = uuid4(), uuid4()
        assert (
            w.run_from_results_dir(
                _adapter_args(rd, eval_run_id=run_id, attempt_id=attempt_id)
            )
            == 0
        )
        pointer = a.SelectedPointer(rd / "eval_attempts" / str(run_id)).read()
        assert pointer is not None
        assert pointer.revision == 0
        assert str(pointer.attempt_id) == str(attempt_id)

    def test_output_exports_final_report(self, tmp_path):
        """`--output` 只导出已 final 的 attempt report（§7.1 / §3.1）。"""
        rd = _make_results_dir(tmp_path)
        out = tmp_path / "export" / "report.json"
        assert w.run_from_results_dir(_adapter_args(rd, output=str(out))) == 0
        data = json.loads(out.read_text("utf-8"))
        assert data["report_family"] == "attempt-v2"
        assert data["metadata"]["eval_semantics_version"] == "attempt-stream-v1"
        assert data["judge_execution_status"] == "not_requested"
        assert "eval_attempt" in data
        assert "episode" in data

    def test_deprecated_aliases_recorded_in_manifest(self, tmp_path):
        """`--agent-model`/`--judge-model` 是 deprecated alias，须在 manifest 留痕。"""
        rd = _make_results_dir(tmp_path)
        run_id, attempt_id = uuid4(), uuid4()
        assert (
            w.run_from_results_dir(
                _adapter_args(
                    rd,
                    eval_run_id=run_id,
                    attempt_id=attempt_id,
                    agent_model="old-model",
                    judge_model="old-judge",
                )
            )
            == 0
        )
        store = a.ArtifactStore(
            rd / "eval_attempts" / str(run_id) / "attempts" / str(attempt_id)
        )
        argv = store.read_input_manifest().manifest.command.argv_without_secrets
        assert "--agent-model" in argv and "old-model" in argv
        assert "--judge-model" in argv and "old-judge" in argv

    def test_attempt_busy_exit_2(self, tmp_path):
        rd = _make_results_dir(tmp_path)
        run_id = uuid4()
        series = rd / "eval_attempts" / str(run_id)
        lease = a.AttemptLease.acquire(series, owner_id="holder")
        try:
            args = _adapter_args(rd, eval_run_id=run_id)
            assert w.run_from_results_dir(args) == 2
        finally:
            lease.release()

    def test_publish_conflict_maps_to_exit_2(self, tmp_path):
        """pointer CAS 冲突（expected revision 不符）→ exit 2，且不产生任何变更。"""
        store, series, _, _, _ = _publish_attempt(tmp_path)
        lease = a.AttemptLease.acquire(series, owner_id="p")
        try:
            code = w.publish_selected_attempt(
                store, series, lease, expected_revision=99
            )
        finally:
            lease.release()
        assert code == 2
        assert a.SelectedPointer(series).read().revision == 0

    def test_publish_verification_failure_maps_to_exit_1(self, tmp_path):
        """发布前 digest 校验失败（report 被篡改）→ exit 1，不发布（§1.2-2）。"""
        store, series, _, _, _ = _publish_attempt(tmp_path, publish=False)
        (store.root / "reports" / "eval_report.json").write_bytes(b'{"tampered": true}')
        lease = a.AttemptLease.acquire(series, owner_id="p")
        try:
            code = w.publish_selected_attempt(store, series, lease)
        finally:
            lease.release()
        assert code == 1
        assert not (series / "selected_attempt.json").exists()

    def test_cancelled_no_publish_returns_130(self, tmp_path):
        """CANCELLED → 写 cancellation ledger、绝不发布、exit 130（§7.1）。"""
        rd = _make_results_dir(tmp_path)
        run_id, attempt_id = uuid4(), uuid4()
        assert (
            w.run_from_results_dir(
                _adapter_args(rd, eval_run_id=run_id, attempt_id=attempt_id)
            )
            == 0
        )
        series = rd / "eval_attempts" / str(run_id)
        store = a.ArtifactStore(series / "attempts" / str(attempt_id))
        (series / "selected_attempt.json").unlink()
        (store.root / "audit" / "final_ledger.json").unlink()
        frozen = store.read_input_manifest()
        w.write_cancellation_ledger(store, frozen, store.audit_journal())
        assert store.read_final_ledger().terminal_status is c.WorkflowStatus.CANCELLED
        assert (
            w.run_from_results_dir(
                _adapter_args(rd, resume_attempt=f"{run_id}/{attempt_id}")
            )
            == 130
        )
        assert not (series / "selected_attempt.json").exists()

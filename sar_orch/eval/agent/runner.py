"""P3 async AgentRunner 协议与确定性 FakeRunner（不接触任何真实 LLM）。

角色 runner 是唯一可能联络模型的地方；workflow 从不 import DeepAgent /
ChatOpenAI，只在 score job 被 claim 的时刻向 `RunnerFactory` 要一个
`AgentRunner`。`--no-llm-judge` 永不请求 runner —— 因此零模型/agent 构造
（§1.2-12、§2.3、§9 P3）。

`FakeRunner` 是确定性、可注入的测试 runner；P3 全部 graph 测试只用 fake
runner，不产生网络/真实模型调用。行为按 `str(job.job_id)` 键控：

- ``ok``（默认）：SUCCEEDED，rubric 每个维度打 1.0；
- ``unknown``：UNKNOWN，带 `unknown_reason` 的 draft；
- ``fail``：每次调用抛 retryable `RunnerError` → 重试耗尽后 FAILED；
- ``hard_fail``：抛 non-retryable `RunnerError` → 立即 FAILED；
- ``timeout``：阻塞超过 workflow timeout → 重试耗尽后 TIMED_OUT；
- ``timeout_once``：第一次 attempt 阻塞（被 wait_for 取消），重试成功；
- ``invalid_draft``：返回 role 与 job 不符的 draft → 校验 FAILED；
- ``evidence_mismatch``：dimensions 与 evidence 数量不一致 → 校验 FAILED（P3-M6）；
- ``slow``：睡 `sleep` 秒后成功（semaphore 并发断言用）；
- ``crash``：抛 RuntimeError（基础设施错误）→ 中断整个 graph run。

禁止：本模块不得 import / 构造 DeepAgent 或任何 chat 模型。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4

from sar_orch.eval import contracts as c

__all__ = [
    "AgentRunner",
    "FakeRunner",
    "RunnerError",
    "RunnerFactory",
    "RunnerOutcome",
    "RunnerStatus",
    "make_fake_runner_factory",
]


class RunnerStatus(str, Enum):
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunnerOutcome:
    """一次 runner 调用的结果。`draft` 仅当 status=SUCCEEDED/UNKNOWN 时有效。"""

    status: RunnerStatus
    draft: c.ScoreDraft | None = None
    error: str | None = None
    usage: c.UsageSnapshot | None = None
    latency_ms: int | None = None
    model_requested: bool = True
    validation_errors: list[str] = field(default_factory=list)


class RunnerError(Exception):
    """可重试或不可重试的 runner 失败；retryable=True 时 workflow 会重试。"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class AgentRunner(Protocol):
    async def run(
        self, job: c.ScoreJob, *, invocation_id: UUID, node_attempt: int
    ) -> RunnerOutcome: ...


RunnerFactory = Callable[[], AgentRunner]


class FakeRunner:
    """确定性 fake runner。构造不产生任何模型对象；调用计数可见。"""

    def __init__(
        self,
        manifest: c.FrozenInputManifest,
        configs: dict[str, Any] | None = None,
        *,
        sleep: float = 0.02,
        call_sink: list[tuple[str, str, int]] | None = None,
    ) -> None:
        self._manifest = manifest
        self._configs = dict(configs or {})
        self._sleep = sleep
        self.calls = call_sink if call_sink is not None else []

    # ── rubric 查询 ──────────────────────────────────────────────────────────
    def _rubric(self, job: c.ScoreJob) -> c.RubricSpec:
        for r in self._manifest.manifest.rubrics:
            if r.rubric_id == job.rubric_id and r.digest == job.rubric_digest:
                return r
        raise RunnerError(
            f"rubric {job.rubric_id}/{job.rubric_digest[:8]} not in frozen manifest",
            retryable=False,
        )

    def _usage(self, latency_ms: int) -> c.UsageSnapshot:
        return c.UsageSnapshot(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_ms=latency_ms,
            cost=0.001,
        )

    def _draft_ok(self, job: c.ScoreJob, rubric: c.RubricSpec) -> c.ScoreDraft:
        evidence: list[c.EvidenceRef] = []
        for dim in rubric.dimensions:
            sha = c.sha256_hex(f"{job.job_id}:{dim}".encode())
            ref = c.ArtifactRef(
                path=f"evidence/job-scoped/{job.job_id}/{dim}.json",
                sha256=sha,
                bytes=len(sha),
                media_type="application/json",
                producer="fake-runner",
            )
            evidence.append(
                c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)
            )
        return c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={dim: 1.0 for dim in rubric.dimensions},
            evidence=evidence,
            model_used="fake-runner",
        )

    def _draft_unknown(self, job: c.ScoreJob) -> c.ScoreDraft:
        return c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions={},
            model_used="fake-runner",
            unknown_reason="fake runner abstained",
        )

    def _draft_wrong_role(self, job: c.ScoreJob, rubric: c.RubricSpec) -> c.ScoreDraft:
        other = (
            c.JudgeRole.OBSERVATION_SCORE_JUDGE
            if job.role is c.JudgeRole.DISPATCH_SCORE_JUDGE
            else c.JudgeRole.DISPATCH_SCORE_JUDGE
        )
        return c.ScoreDraft(
            role=other,
            invocation_id=uuid4(),
            dimensions={dim: 0.5 for dim in rubric.dimensions},
            model_used="fake-runner",
        )

    def _draft_evidence_mismatch(
        self, job: c.ScoreJob, rubric: c.RubricSpec
    ) -> c.ScoreDraft:
        """dimensions 2 个但只给 1 条 evidence（P3-M6 反例：静默丢维）。"""
        dims = {dim: 1.0 for dim in rubric.dimensions}
        first_dim = rubric.dimensions[0]
        sha = c.sha256_hex(f"{job.job_id}:{first_dim}".encode())
        ref = c.ArtifactRef(
            path=f"evidence/job-scoped/{job.job_id}/{first_dim}.json",
            sha256=sha,
            bytes=len(sha),
            media_type="application/json",
            producer="fake-runner",
        )
        return c.ScoreDraft(
            role=job.role,
            invocation_id=uuid4(),
            dimensions=dims,
            evidence=[c.EvidenceRef(ref=ref, claim_type=c.ClaimType.SCORE, digest=sha)],
            model_used="fake-runner",
        )

    # ── protocol ─────────────────────────────────────────────────────────────
    async def run(
        self, job: c.ScoreJob, *, invocation_id: UUID, node_attempt: int
    ) -> RunnerOutcome:
        self.calls.append((str(job.job_id), str(invocation_id), node_attempt))
        raw = self._configs.get(str(job.job_id), self._configs.get("__default__", "ok"))
        opts = raw if isinstance(raw, dict) else {"behavior": raw}
        behavior = opts.get("behavior", "ok")
        sleep_s = float(opts.get("sleep", self._sleep))

        rubric = self._rubric(job)

        if behavior == "timeout_once" and node_attempt == 1:
            await asyncio.sleep(3600.0)
        if behavior == "timeout":
            await asyncio.sleep(3600.0)
        if behavior == "slow":
            await asyncio.sleep(sleep_s)
        if behavior == "unknown":
            return RunnerOutcome(
                status=RunnerStatus.UNKNOWN,
                draft=self._draft_unknown(job),
                usage=self._usage(3),
                latency_ms=3,
            )
        if behavior == "fail":
            raise RunnerError(f"fake retryable failure for job {job.job_id}")
        if behavior == "fail_then_ok" and node_attempt == 1:
            raise RunnerError(
                f"fake retryable failure on first attempt for job {job.job_id}"
            )
        if behavior == "hard_fail":
            raise RunnerError(
                f"fake non-retryable failure for job {job.job_id}", retryable=False
            )
        if behavior == "invalid_draft":
            return RunnerOutcome(
                status=RunnerStatus.SUCCEEDED,
                draft=self._draft_wrong_role(job, rubric),
                usage=self._usage(3),
                latency_ms=3,
            )
        if behavior == "evidence_mismatch":
            return RunnerOutcome(
                status=RunnerStatus.SUCCEEDED,
                draft=self._draft_evidence_mismatch(job, rubric),
                usage=self._usage(3),
                latency_ms=3,
            )
        if behavior == "crash":
            raise RuntimeError("fake infrastructure crash (simulated kill)")
        return RunnerOutcome(
            status=RunnerStatus.SUCCEEDED,
            draft=self._draft_ok(job, rubric),
            usage=self._usage(3),
            latency_ms=3,
        )


def make_fake_runner_factory(
    manifest: c.FrozenInputManifest,
    configs: dict[str, Any] | None = None,
    *,
    sleep: float = 0.02,
) -> tuple[RunnerFactory, dict[str, Any]]:
    """构造 FakeRunner 的工厂 + 可观测统计。

    `stats["constructed"]` 是 runner 构造次数（零模型构造断言用）；
    `stats["calls"]` 聚合全部 runner 的 `(job_id, invocation_id, node_attempt)`。
    """
    stats: dict[str, Any] = {"constructed": 0, "calls": []}

    def factory() -> AgentRunner:
        stats["constructed"] += 1
        return FakeRunner(manifest, configs, sleep=sleep, call_sink=stats["calls"])

    return factory, stats

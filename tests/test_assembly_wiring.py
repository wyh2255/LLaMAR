"""env-contract P4-4：实验装配通用化 —— 通用装配骨架的注入面与全流程。

三层证据：

1. fake EnvPack 驱动 ``run_assembly`` 全流程（与 SAR 无关）：barrier 工厂消费
   （``env_params`` 透传）、端口选择、poll 循环逐回合落盘、步数终局判定、
   teardown 顺序、返回值；
2. 装配钩子生命周期顺序（``on_environment_ready`` → ``on_coordinator_started``
   → ``step_metrics``/``on_step`` → ``on_poll`` → ``finalize_artifacts`` →
   ``run_terminal_evaluations``）；
3. EnvPack prompts 目录 fail-fast（显式声明却不存在 → 硬错误，不静默回退）。
"""

from __future__ import annotations

import asyncio

import pytest

import sar_orch.experiment as sar_experiment
from orchestration.assembly import (
    AssemblyHooks,
    AssemblySpec,
    classify_end_reason,
    run_assembly,
)
from orchestration.env_pack import EnvPack


class _FakePack(EnvPack):
    """非 SAR 的最小环境包：记录 barrier 工厂入参。"""

    name = "fake"

    def __init__(self, *, coordinator_prompts_dir=None, worker_prompts_dir=None):
        self._coordinator_prompts_dir = coordinator_prompts_dir
        self._worker_prompts_dir = worker_prompts_dir
        self.barrier_calls: list[dict] = []

    @property
    def coordinator_prompts_dir(self):
        return self._coordinator_prompts_dir

    @property
    def worker_prompts_dir(self):
        return self._worker_prompts_dir

    def build_barrier(self, *, num_agents, seed, **env_params):
        call = {"num_agents": num_agents, "seed": seed, **env_params}
        self.barrier_calls.append(call)
        return _SteppingBarrier(**call)


class _SteppingBarrier:
    """脚本化 barrier：每轮 drain 前进一步；``finish_after`` 到达即 finished。"""

    def __init__(self, *, num_agents, seed, finish_after=None, **_env_params):
        self.num_agents = num_agents
        self.seed = seed
        self.finish_after = finish_after
        self.steps = 0
        self.stopped = False
        self._pending: list[dict] = []

    def is_finished(self):
        return self.finish_after is not None and self.steps >= self.finish_after

    def get_metrics(self):
        return {
            "finished": self.is_finished(),
            "steps": self.steps,
            "coverage": 0.1 * self.steps,
            "transport_rate": 0.0,
        }

    def drain_step_logs(self):
        if self.is_finished():
            return []
        self.steps += 1
        self._pending.append(
            {
                "step": self.steps,
                "actions": [f"act-{self.steps}"],
                "successes": [True],
                "observations": [f"obs-{self.steps}"],
                "coverage": 0.1 * self.steps,
                "transport_rate": 0.0,
                "finished": self.is_finished(),
                "timeout_agents": [],
                "noop_sources": [""],
            }
        )
        drained, self._pending = self._pending, []
        return drained

    def stop(self):
        self.stopped = True


class _FakeLogger:
    """记录装配层全部日志接线调用（含顺序）。"""

    def __init__(self, log_dir):
        self._log_dir = log_dir
        self.calls: list[str] = []
        self.metadata: dict | None = None
        self.run_context: dict | None = None
        self.steps: list[dict] = []
        self.end_reason: str | None = None

    def set_run_context(self, **kwargs):
        self.calls.append("set_run_context")
        self.run_context = kwargs

    def write_metadata(self, metadata):
        self.calls.append("write_metadata")
        self.metadata = dict(metadata)

    def log_step(self, **kwargs):
        self.calls.append("log_step")
        self.steps.append(kwargs)

    def flush_summary(self):
        self.calls.append("flush_summary")

    def set_end_reason(self, reason):
        self.calls.append("set_end_reason")
        self.end_reason = reason

    def freeze_terminal(self):
        self.calls.append("freeze_terminal")

    def get_log_dir(self):
        return self._log_dir

    def close(self):
        self.calls.append("close")


class _FakeCoordinator:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def start(self):
        return None

    async def submit_task(self, description):
        self.task_description = description
        await asyncio.Event().wait()  # 保持 in-flight，直到终态取消

    def clear_sessions(self):
        pass

    async def stop(self):
        pass


class _FakeWorker:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def clear_sessions(self):
        pass

    def stop(self):
        pass


class _RecordingHooks(AssemblyHooks):
    def __init__(self):
        self.events: list[str] = []

    def on_environment_ready(self, state):
        self.events.append("env_ready")

    def on_coordinator_started(self, state):
        self.events.append("coord_started")

    def step_metrics(self, state, *, step_num, step_log):
        self.events.append(f"metrics:{step_num}")
        return {"map_recall": 0.5, "freshness": 0.25}

    def on_step(self, state, *, step_num, step_log, drained_logs):
        self.events.append(f"step:{step_num}")

    def on_poll(self, state, *, metrics, drained_logs):
        self.events.append(f"poll:{metrics['steps']}")

    def finalize_artifacts(self, state, *, final_metrics, end_reason):
        self.events.append(f"finalize:{end_reason}")
        return {"frozen": True}

    def run_terminal_evaluations(self, state, *, final_metrics):
        self.events.append("terminal")
        return {"terminal_eval": "ok"}


async def _no_sleep(_seconds):
    return None


@pytest.fixture()
def allow_no_sleep(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def _run(spec: AssemblySpec):
    return asyncio.run(run_assembly(spec))


def test_generic_assembly_runs_fake_envpack_end_to_end(monkeypatch, tmp_path, allow_no_sleep):
    """fake EnvPack + 全 fake 服务：装配全流程、注入面与终态判定。"""
    pack = _FakePack()
    hooks = _RecordingHooks()
    logger = _FakeLogger(str(tmp_path / "run"))
    coordinator_kwargs: dict = {}
    worker_kwargs: dict = {}

    def coordinator_factory(**kwargs):
        coordinator_kwargs.update(kwargs)
        return _FakeCoordinator(**kwargs)

    def worker_factory(**kwargs):
        worker_kwargs.update(kwargs)
        return _FakeWorker(**kwargs)

    spec = AssemblySpec(
        env_pack=pack,
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=7,
        run_id="fake-run-1",
        max_steps=2,
        agent_base_port=8300,
        coordinator_port=8410,
        task_description="do the thing",
        env_params={"scene": 9},
        metadata={"k": "v"},
        logger_factory=lambda _d: logger,
        coordinator_factory=coordinator_factory,
        worker_factory=worker_factory,
        hooks=hooks,
    )
    metrics = _run(spec)

    # barrier 工厂：EnvPack 被真实消费，env_params 透传
    assert pack.barrier_calls == [{"num_agents": 1, "seed": 7, "scene": 9}]

    # coordinator / worker 构造面（端口选择 + 目录接线 + env_pack 透传）
    assert coordinator_kwargs["env_pack"] is pack
    assert coordinator_kwargs["port"] == 8410
    assert coordinator_kwargs["a2a_port"] == 8411
    assert coordinator_kwargs["log_dir"] == str(tmp_path / "run" / "coordinator")
    assert coordinator_kwargs["supervision_dir"] == str(tmp_path / "run" / "supervision")
    assert coordinator_kwargs["run_id"] == "fake-run-1"
    assert coordinator_kwargs["max_steps"] == 2
    assert worker_kwargs["env_pack"] is pack
    assert worker_kwargs["a2a_port"] == 8300
    assert worker_kwargs["coordinator_url"] == "ws://localhost:8410"
    assert worker_kwargs["log_dir"] == str(tmp_path / "run" / "workers" / "P1")

    # 日志接线：run context / metadata / 逐回合落盘 / 终态
    assert logger.run_context == {
        "run_id": "fake-run-1",
        "model": spec.model,
        "prompt_version": "baseline",
    }
    assert logger.metadata == {"k": "v"}
    assert [row["step_num"] for row in logger.steps] == [1, 2]
    assert logger.steps[0]["max_steps"] == 2
    assert logger.steps[0]["run_id"] == "fake-run-1"
    assert logger.steps[0]["map_recall"] == 0.5
    assert logger.steps[0]["freshness"] == 0.25
    assert logger.end_reason == "max_steps_reached"
    assert logger.calls[-1] == "close"

    # 钩子生命周期顺序（on_poll 的 metrics 为 drain 前的 poll 观测值，
    # 与迁移前 experiment.py 的滚动触发 fallback 口径一致：poll:0 → poll:1）
    assert hooks.events == [
        "env_ready",
        "coord_started",
        "metrics:1",
        "step:1",
        "poll:0",
        "metrics:2",
        "step:2",
        "poll:1",
        "finalize:max_steps_reached",
        "terminal",
    ]

    # 终态判定与返回（含钩子产物并入）
    assert metrics["end_reason"] == "max_steps_reached"
    assert metrics["steps"] == 2
    assert metrics["run_id"] == "fake-run-1"
    assert metrics["max_steps"] == 2
    assert metrics["frozen"] is True
    assert metrics["terminal_eval"] == "ok"
    assert metrics["log_dir"] == str(tmp_path / "run")


def test_assembly_success_end_reason_and_teardown_order(monkeypatch, tmp_path, allow_no_sleep):
    """barrier 在步数内收官 → end_reason=success；收尾顺序 barrier→workers→coordinator。"""
    cleanup: list[str] = []

    class _Barrier(_SteppingBarrier):
        def stop(self):
            cleanup.append("barrier")

    class _Pack(_FakePack):
        def build_barrier(self, *, num_agents, seed, **env_params):
            return _Barrier(num_agents=num_agents, seed=seed, finish_after=1, **env_params)

    class _Coordinator(_FakeCoordinator):
        async def stop(self):
            cleanup.append("coordinator")

    class _Worker(_FakeWorker):
        def stop(self):
            cleanup.append("worker")

    spec = AssemblySpec(
        env_pack=_Pack(),
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=1,
        max_steps=5,
        logger_factory=lambda d: _FakeLogger(d),
        coordinator_factory=_Coordinator,
        worker_factory=_Worker,
    )
    metrics = _run(spec)

    assert metrics["end_reason"] == "success"
    assert metrics["finished"] is True
    assert cleanup == ["barrier", "worker", "coordinator"]


def test_assembly_prompts_dir_missing_fails_fast(tmp_path, allow_no_sleep):
    """prompts 目录显式声明却不存在 → FileNotFoundError（不静默回退）。"""
    missing = tmp_path / "nope"
    spec = AssemblySpec(
        env_pack=_FakePack(coordinator_prompts_dir=str(missing)),
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=1,
        max_steps=1,
    )
    with pytest.raises(FileNotFoundError):
        _run(spec)

    spec = AssemblySpec(
        env_pack=_FakePack(worker_prompts_dir=str(missing)),
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=1,
        max_steps=1,
    )
    with pytest.raises(FileNotFoundError):
        _run(spec)


def test_assembly_default_hooks_and_null_logger_are_noop(monkeypatch, tmp_path, allow_no_sleep):
    """缺省钩子（无注入）与缺省 logger（NullExperimentLogger）可跑通最小装配。"""
    spec = AssemblySpec(
        env_pack=_FakePack(),
        log_dir=str(tmp_path / "run"),
        num_agents=1,
        agent_names=["P1"],
        seed=1,
        max_steps=1,
        coordinator_factory=_FakeCoordinator,
        worker_factory=_FakeWorker,
    )
    metrics = _run(spec)
    assert metrics["end_reason"] == "max_steps_reached"
    assert metrics["log_dir"] == str(tmp_path / "run")


def test_classify_end_reason_reexport_identity():
    """``classify_end_reason`` 实现迁至装配层；SAR 薄壳 re-export identity 不变。"""
    assert sar_experiment.classify_end_reason is classify_end_reason

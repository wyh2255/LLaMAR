"""worker 侧装配窗口跨线程隔离 —— RP2 跨 worker 状态外溢回归（P0）。

背景（A100 真机 RP2 归因）：
- 装配窗口**跨线程**：``src/orchestration/worker.py`` 的 ``start()`` 在*调用者
  线程*（assembly 主线程，逐 worker 顺序）先调 ``build_worker_state_provider
  (ctx)``（:456）；``run()`` 在 *worker 自身线程*随后调 ``build_session_factory
  (role='worker')``（:504）。env_pack 实例为全部 worker 共享
  （``src/orchestration/assembly.py`` 循环建 worker 传同一实例）。
- 旧实现把 worker 侧 ctx / provider 记在**实例级单槽**：后建者（Bob，
  agent_idx=1）覆盖先建者（Alice，agent_idx=0），两名 worker 的 session 工厂
  闭包捕获**同一个** provider → 两侧 Environment State 逐字相同（都标
  Agent1），动作真值与渲染视图错位。
- 修复：worker 侧改「agent_idx → provider 注册表 + 线程局部 ctx 锚点」；
  session 工厂只解析**本线程**锚点，未绑定 → fail-fast（绝不静默借用他
  worker 的视图）。

本文件覆盖：
1. 复现/回归：主线程先建 A、再建 B（即旧实现的覆盖窗口），A 的 worker 线程
   走 tools → factory，必须产出 **A 的** provider（Agent0 视图）；
2. 双 worker 线程并发各自 tools+factory / 各自 provider+factory，互不串；
3. 未绑定线程调用工厂 → RuntimeError（fail-fast，不借用其他 worker 视图）；
4. 单线程顺序用法（provider → factory 同线程）与既有契约逐字兼容。

红线：只读 ``tests/fakes.py``（并行卡 FA 领地在改），本文件不修改任何既有
测试文件。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

from ai2thor_orch.env_pack import Ai2ThorEnvPack

#: 线程内装配步骤 + join 的等待上限（防 CI 死等；超时会在断言里显式暴露）。
_THREAD_TIMEOUT = 15.0


# ── 夹具构造（fake 模式、公共工厂面）────────────────────────────────────────


def _build_pack() -> Ai2ThorEnvPack:
    return Ai2ThorEnvPack(
        task_id="3_transport_groceries", scene="FloorPlan1", mode="fake"
    )


def _worker_ctx(barrier: Any, *, agent_idx: int, agent_name: str) -> Any:
    """构造 ``WorkerEnv``（字段与 ``orchestration/worker.py`` 装配快照同构）。"""
    from orchestration.env_pack import WorkerEnv

    return WorkerEnv(
        worker_id=f"worker-{agent_idx}",
        agent_name=agent_name,
        agent_idx=agent_idx,
        barrier=barrier,
        log_dir=None,
        model="test-model",
        api_base="http://localhost:1",
        api_key_env="TEST_KEY",
        memory_read_mode="legacy",
        exp_logger=None,
        coordinator_url="ws://localhost:8080",
        http_url="http://localhost:8080",
        coordinator_secret=None,
        prune_policy="count_window",
    )


def _start_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """起一个线程执行 ``fn``；结果/异常收进 box，由主线程断言（异常原样回抛）。"""
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 回抛给断言线程
            box["exc"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


def _join(thread: threading.Thread, box: dict[str, Any]) -> Any:
    thread.join(timeout=_THREAD_TIMEOUT)
    assert not thread.is_alive(), f"线程未在 {_THREAD_TIMEOUT}s 内结束"
    if "exc" in box:
        raise box["exc"]
    return box["value"]


def _run_in_thread(fn: Callable[[], Any]) -> Any:
    """在独立线程执行 ``fn`` 并同步取回结果（异常回抛调用线程）。"""
    thread, box = _start_thread(fn)
    return _join(thread, box)


def _worker_run_body(pack: Ai2ThorEnvPack, ctx: Any) -> Any:
    """模拟 ``worker.run()`` 内的装配顺序（同线程）：先工具、后 session 工厂。"""
    asyncio.run(pack.build_worker_tools(ctx))
    factory = pack.build_session_factory(role="worker")
    return factory()


# ── 1/2/3. 跨 worker 隔离 ───────────────────────────────────────────────────


class TestCrossWorkerIsolation:
    """实例级单槽覆盖后，各 worker 的 session 必须仍解析自己的 provider。"""

    def test_factory_resolves_own_worker_after_slot_overwrite(self):
        """RP2 核心复现：主线程先建 A 再建 B（覆盖窗口），A 线程取工厂 → A 视图。

        旧实现此处 session 的 state provider 是 B（Bob）的 → Agent1 视图（RED）。
        """
        pack = _build_pack()
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=10)
        ctx_a = _worker_ctx(barrier, agent_idx=0, agent_name="Alice")
        ctx_b = _worker_ctx(barrier, agent_idx=1, agent_name="Bob")

        # assembly 主线程顺序：A 先、B 后（B 覆盖了旧实现的实例级单槽）。
        provider_a = pack.build_worker_state_provider(ctx_a)
        provider_b = pack.build_worker_state_provider(ctx_b)

        session_a = _run_in_thread(lambda: _worker_run_body(pack, ctx_a))

        assert session_a._state_provider is provider_a
        assert session_a._state_provider is not provider_b
        assert session_a._state_provider._agent_idx == 0
        # 渲染面（Environment State 的 "You are Agent{N}" 来源）。
        assert session_a._state_provider.snapshot().payload["agent_name"] == "Agent0"

    def test_two_worker_threads_stay_isolated(self):
        """双 worker 线程并发 tools+factory：A → Agent0、B → Agent1，互不串。"""
        pack = _build_pack()
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=10)
        ctx_a = _worker_ctx(barrier, agent_idx=0, agent_name="Alice")
        ctx_b = _worker_ctx(barrier, agent_idx=1, agent_name="Bob")
        provider_a = pack.build_worker_state_provider(ctx_a)
        provider_b = pack.build_worker_state_provider(ctx_b)

        gate = threading.Barrier(2, timeout=_THREAD_TIMEOUT)

        def _body(ctx: Any) -> Any:
            gate.wait()
            return _worker_run_body(pack, ctx)

        thread_a, box_a = _start_thread(lambda: _body(ctx_a))
        thread_b, box_b = _start_thread(lambda: _body(ctx_b))
        session_a = _join(thread_a, box_a)
        session_b = _join(thread_b, box_b)

        assert session_a._state_provider is provider_a
        assert session_b._state_provider is provider_b
        assert session_a._state_provider is not session_b._state_provider
        assert session_a._state_provider.snapshot().payload["agent_name"] == "Agent0"
        assert session_b._state_provider.snapshot().payload["agent_name"] == "Agent1"

    def test_two_threads_each_build_provider_then_factory(self):
        """每线程各自 provider → factory（单线程用法的并发推广）不互相污染。"""
        pack = _build_pack()
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=10)
        ctx_a = _worker_ctx(barrier, agent_idx=0, agent_name="Alice")
        ctx_b = _worker_ctx(barrier, agent_idx=1, agent_name="Bob")

        gate = threading.Barrier(2, timeout=_THREAD_TIMEOUT)

        def _body(ctx: Any) -> tuple[Any, Any]:
            gate.wait()
            provider = pack.build_worker_state_provider(ctx)
            factory = pack.build_session_factory(role="worker")
            return provider, factory()

        thread_a, box_a = _start_thread(lambda: _body(ctx_a))
        thread_b, box_b = _start_thread(lambda: _body(ctx_b))
        provider_a, session_a = _join(thread_a, box_a)
        provider_b, session_b = _join(thread_b, box_b)

        assert session_a._state_provider is provider_a
        assert session_b._state_provider is provider_b
        assert session_a._state_provider._agent_idx == 0
        assert session_b._state_provider._agent_idx == 1


# ── 4/5. fail-fast 与既有无回归边界 ─────────────────────────────────────────


class TestFailFastAndBackwardCompat:
    def test_unbound_thread_does_not_borrow_other_worker_view(self):
        """未绑定任何 worker 的线程调用工厂 → RuntimeError（不静默借用 A 视图）。

        旧实现（实例级单槽）会静默把主线程刚建的 A provider 交给任意线程；修复
        后必须 fail-fast：session 工厂只解析**本线程**锚点。
        """
        pack = _build_pack()
        barrier = pack.build_barrier(num_agents=1, seed=42, max_steps=5)
        ctx_a = _worker_ctx(barrier, agent_idx=0, agent_name="Alice")
        pack.build_worker_state_provider(ctx_a)  # 主线程绑定 A（供同线程用法）

        with pytest.raises(RuntimeError, match="build_worker_state_provider"):
            _run_in_thread(lambda: pack.build_session_factory(role="worker"))

    def test_worker_role_requires_provider_built_first(self):
        """fresh pack、本线程从未建 provider → RuntimeError（既有契约保留）。"""
        pack = _build_pack()
        with pytest.raises(RuntimeError, match="build_worker_state_provider"):
            pack.build_session_factory(role="worker")

    def test_single_thread_provider_then_factory_still_works(self):
        """单线程顺序用法（provider → factory 同线程）与既有契约逐字兼容。"""
        pack = _build_pack()
        barrier = pack.build_barrier(num_agents=1, seed=42, max_steps=5)
        ctx = _worker_ctx(barrier, agent_idx=0, agent_name="Alice")

        provider = pack.build_worker_state_provider(ctx)
        factory = pack.build_session_factory(role="worker")
        session = factory()

        assert session._state_provider is provider
        assert session.config.state_mode == "semantic"
        assert session.config.memory_read_mode == "legacy"

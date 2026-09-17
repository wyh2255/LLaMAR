"""Tests for the F-vlm vision layer (``ai2thor_orch/vision_hooks.py``).

覆盖范围：

1. ``VisionHooks.upgrade_env_block``——parts 形状（OpenAI 多模态口径）、
   JPEG 可解码、RGB→BGR 通道正确性、agent 隔离；
2. 新鲜度契约——帧轮次 == step 附 parts / 不匹配 = 纯文本 + 注记 /
   无帧 = 原样 / step 缺失 = 不附图；
3. 降级与 inert——开关未激活、内容非纯文本、编码失败（fail-open）；
4. ``VisionWorkerContextManager.assemble`` 注入面——只升级本次新追加的
   Environment State 块；历史消息零触碰；raw 策略 / 未闭合 tool call 不注入；
5. env_pack 接线——unity ∧ VLM=1 ∧ FRAMES=1 换用视觉 session 并注入帧；
   缺配响亮警告；开关关 = 零差异；
6. run metadata 三字段——``resolve_vision_config`` 矩阵 + ``build_run_metadata``
   + ``run_experiment`` 端到端；
7. 依赖方向——``ai2thor_orch.vision_hooks`` 不引入 ``sar_orch``。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from Agent.worker_agent.context import ContextConfig
from Agent.worker_agent.schema import FunctionCall, Message, ToolCall
from ai2thor_orch.frames.frame_store import FrameStore
from ai2thor_orch.state.context import AI2ThorWorkerContextManager
from ai2thor_orch.vision_hooks import (
    JPEG_QUALITY_DEFAULT,
    OUTCOME_ATTACHED,
    OUTCOME_DISABLED,
    OUTCOME_ERROR,
    OUTCOME_NO_FRAME,
    OUTCOME_NOT_TEXT,
    OUTCOME_STALE,
    VISION_NOTE_PREFIX,
    VLM_SWITCH_ENV,
    VisionHooks,
    VisionWorkerContextManager,
    frame_resolution,
    resolve_vision_config,
    vision_channel_active,
    vision_switches_ready,
    vlm_enabled,
    warn_misconfigured_vision,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Helpers ─────────────────────────────────────────────────────────────


def _rb_frame(height: int = 24, width: int = 32) -> np.ndarray:
    """左半红、右半蓝的 RGB 帧（颜色探针：检出 RGB/BGR 互换）。"""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, : width // 2] = (255, 0, 0)
    frame[:, width // 2 :] = (0, 0, 255)
    return frame


def _solid_frame(
    color: tuple[int, int, int], height: int = 16, width: int = 16
) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = color
    return frame


def _decode_data_url(url: str) -> np.ndarray:
    import cv2

    assert url.startswith("data:image/jpeg;base64,"), url[:60]
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"  # JPEG SOI
    return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)


def _make_hooks(
    store: FrameStore | None = None, *, agent_idx: int = 0, enabled: bool = True
) -> VisionHooks:
    return VisionHooks(frame_store=store, agent_idx=agent_idx, enabled=enabled)


class _ListHandler(logging.Handler):
    """记录列表 handler（自包含捕获，不依赖 root 传播 / caplog 全局状态）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _capture_vision_logs(level: int = logging.INFO) -> Iterator[_ListHandler]:
    """直挂 vision logger 的日志捕获（对套件级 logging 状态零依赖）。"""
    log = logging.getLogger("ai2thor_orch.vision_hooks")
    handler = _ListHandler()
    handler.setLevel(level)
    old_level, old_disabled = log.level, log.disabled
    log.addHandler(handler)
    log.setLevel(level)
    log.disabled = False
    try:
        yield handler
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.disabled = old_disabled


def _messages(handler: _ListHandler, level: int) -> list[str]:
    return [r.getMessage() for r in handler.records if r.levelno == level]


def _barrier_with_objects(num_agents: int = 2, max_steps: int = 50):
    """2 agent + 已知物体的 FakeController barrier（沿用 test_context 形态）。"""
    from ai2thor_orch.barrier.ai2thor_barrier import AI2ThorBarrier
    from ai2thor_orch.executor.controller_executor import ControllerExecutor
    from ai2thor_orch.tests.fakes import FakeController, make_default_metadata
    from ai2thor_orch.visibility import AliasRegistry

    metadata = make_default_metadata(
        scene="FloorPlan1", num_agents=num_agents, has_objects=True
    )
    controller = FakeController(metadata_override=metadata)
    return AI2ThorBarrier(
        num_agents=num_agents,
        executor=ControllerExecutor(controller),
        max_steps=max_steps,
        step_timeout=5.0,
        alias_registry=AliasRegistry(),
    )


def _advance_one_round(barrier: Any, num_agents: int = 2) -> None:
    """一轮真实动作推进（两 agent 并发提交）。

    串行 ``await`` 会让第二槽按 ``step_timeout`` 超时注 NoOp 并白耗一圈；
    ``gather`` 并发提交命中 barrier 的 all-submitted 快路径，一轮真实动作
    立即执行（无超时等待）。
    """

    async def _run() -> None:
        await asyncio.gather(
            *(barrier.submit_action(idx, "MoveAhead") for idx in range(num_agents))
        )

    asyncio.run(_run())


def _vision_ctx(
    barrier: Any,
    *,
    agent_idx: int = 0,
    hooks: VisionHooks | None,
    strategy: str = "hybrid",
) -> tuple[VisionWorkerContextManager, Any]:
    """真 barrier + 真 provider 的视觉 session（内核装配同参形态）。"""
    from ai2thor_orch.state.worker_state_provider import AI2ThorWorkerStateProvider

    provider = AI2ThorWorkerStateProvider(barrier=barrier, agent_idx=agent_idx)
    config = ContextConfig(
        strategy=strategy, memory_read_mode="legacy", prune_policy="count_window"
    )
    ctx = VisionWorkerContextManager(
        config, 80000, None, state_provider=provider, vision_hooks=hooks
    )
    return ctx, provider


def _worker_env_ctx(barrier: Any, *, agent_idx: int = 0) -> Any:
    """``WorkerEnv`` 实例（env_pack 工厂的 ctx 输入，与 test_env_pack 同构）。"""
    from orchestration.env_pack import WorkerEnv

    return WorkerEnv(
        worker_id="worker-0",
        agent_name="Alice",
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


# ── 1. VisionHooks.upgrade_env_block（部件形状 / 畅通路径）────────────────


class TestUpgradePartsShape:
    def test_fresh_frame_attaches_vision_parts(self) -> None:
        """帧轮次 == step → text+image_url 两段式 parts（C1 口径）。"""
        store = FrameStore()
        frame = _rb_frame()
        store.record(0, 3, frame, "pickup_ok")
        hooks = _make_hooks(store)
        msg = Message(role="user", content="ES BLOCK")

        outcome = hooks.upgrade_env_block(msg, step=3)

        assert outcome == OUTCOME_ATTACHED
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        text_part, image_part = msg.content
        assert text_part == {"type": "text", "text": "ES BLOCK"}
        assert image_part["type"] == "image_url"
        url = image_part["image_url"]["url"]

        decoded = _decode_data_url(url)
        assert decoded.shape == frame.shape
        # 通道正确性：源 RGB 左红右蓝 → 解码 BGR 左 [2] 高、右 [0] 高。
        h, w = frame.shape[:2]
        left = decoded[h // 2, w // 4]
        right = decoded[h // 2, (3 * w) // 4]
        assert int(left[2]) > 150 and int(left[0]) < 100
        assert int(right[0]) > 150 and int(right[2]) < 100

    def test_text_part_preserves_block_byte_for_byte(self) -> None:
        store = FrameStore()
        store.record(0, 0, _solid_frame((10, 20, 30)), "init")
        hooks = _make_hooks(store)
        block = "---\n## Environment State\n---\n### Environment\nStep: 0/5"
        msg = Message(role="user", content=block)

        assert hooks.upgrade_env_block(msg, step=0) == OUTCOME_ATTACHED
        assert msg.content[0]["text"] == block

    def test_agent_isolation(self) -> None:
        """每个 worker 只消费自己槽位的帧。"""
        store = FrameStore()
        store.record(0, 1, _solid_frame((255, 0, 0)), "pickup_ok")  # 红
        store.record(1, 1, _solid_frame((0, 255, 0)), "pickup_ok")  # 绿
        hooks = _make_hooks(store, agent_idx=1)

        msg = Message(role="user", content="ES")
        assert hooks.upgrade_env_block(msg, step=1) == OUTCOME_ATTACHED
        decoded = _decode_data_url(msg.content[1]["image_url"]["url"])
        pixel = decoded[8, 8]
        assert int(pixel[1]) > 150 and int(pixel[0]) < 100 and int(pixel[2]) < 100

    def test_first_attach_logs_once(self) -> None:
        store = FrameStore()
        hooks = _make_hooks(store)
        with _capture_vision_logs(logging.INFO) as logs:
            store.record(0, 1, _solid_frame((1, 2, 3)), "pickup_ok")
            assert (
                hooks.upgrade_env_block(Message(role="user", content="a"), step=1)
                == OUTCOME_ATTACHED
            )
            store.record(0, 2, _solid_frame((1, 2, 3)), "pickup_ok")
            assert (
                hooks.upgrade_env_block(Message(role="user", content="b"), step=2)
                == OUTCOME_ATTACHED
            )
        infos = [r for r in logs.records if r.levelno == logging.INFO]
        assert len(infos) == 1  # 首帧日志闩：只记一次，不刷屏

    def test_rgba_frame_encodes(self) -> None:
        """4 通道帧走 RGBA→BGRA 分支（与 FrameStore 落盘同口径）。"""
        store = FrameStore()
        frame = np.zeros((8, 8, 4), dtype=np.uint8)
        frame[:, :] = (200, 10, 10, 255)
        store.record(0, 1, frame, "pickup_ok")
        hooks = _make_hooks(store)

        msg = Message(role="user", content="ES")
        assert hooks.upgrade_env_block(msg, step=1) == OUTCOME_ATTACHED
        decoded = _decode_data_url(msg.content[1]["image_url"]["url"])
        assert decoded.ndim == 3 and decoded.shape[2] == 3


# ── 2. 新鲜度契约 / 3. 降级 ───────────────────────────────────────────────


class TestFreshnessAndDegradation:
    def test_stale_frame_note_only(self) -> None:
        store = FrameStore()
        store.record(0, 2, _solid_frame((1, 2, 3)), "pickup_ok")
        hooks = _make_hooks(store)
        msg = Message(role="user", content="ES BLOCK")

        outcome = hooks.upgrade_env_block(msg, step=5)

        assert outcome == OUTCOME_STALE
        assert isinstance(msg.content, str)
        assert msg.content.startswith("ES BLOCK")
        assert VISION_NOTE_PREFIX in msg.content
        assert "round 2" in msg.content and "current step is 5" in msg.content
        assert "image_url" not in msg.content  # 绝不附旧帧

    def test_no_frame_plain_text_untouched(self) -> None:
        hooks = _make_hooks(FrameStore())
        msg = Message(role="user", content="ES BLOCK")

        assert hooks.upgrade_env_block(msg, step=0) == OUTCOME_NO_FRAME
        assert msg.content == "ES BLOCK"  # 原样，连注记也不加

    def test_step_none_never_attaches(self) -> None:
        store = FrameStore()
        store.record(0, 7, _solid_frame((1, 2, 3)), "pickup_ok")
        hooks = _make_hooks(store)
        msg = Message(role="user", content="ES BLOCK")

        assert hooks.upgrade_env_block(msg, step=None) == OUTCOME_STALE
        assert (
            isinstance(msg.content, str) and "current step unavailable" in msg.content
        )

    def test_disabled_is_inert(self) -> None:
        store = FrameStore()
        store.record(0, 1, _solid_frame((1, 2, 3)), "pickup_ok")
        hooks = _make_hooks(store, enabled=False)
        msg = Message(role="user", content="ES BLOCK")

        assert hooks.upgrade_env_block(msg, step=1) == OUTCOME_DISABLED
        assert msg.content == "ES BLOCK"

    def test_non_text_content_skipped(self) -> None:
        store = FrameStore()
        store.record(0, 1, _solid_frame((1, 2, 3)), "pickup_ok")
        hooks = _make_hooks(store)
        existing = [{"type": "text", "text": "already parts"}]
        msg = Message(role="user", content=existing)

        assert hooks.upgrade_env_block(msg, step=1) == OUTCOME_NOT_TEXT
        # pydantic v2 校验会复制 list，这里断言"内容未被改动"（值相等即不动）。
        assert msg.content == existing

    def test_encode_failure_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """编码失败 → 纯文本原样（fail-open，不炸本轮请求）。"""
        import cv2

        store = FrameStore()
        store.record(0, 1, _solid_frame((1, 2, 3)), "pickup_ok")
        hooks = _make_hooks(store)
        monkeypatch.setattr(cv2, "imencode", lambda *a, **k: (False, None))
        msg = Message(role="user", content="ES BLOCK")

        assert hooks.upgrade_env_block(msg, step=1) == OUTCOME_ERROR
        assert msg.content == "ES BLOCK"


# ── 4. VisionWorkerContextManager.assemble 注入面 ─────────────────────────


class TestVisionContextAssemble:
    def test_assemble_upgrades_appended_env_block_only(self) -> None:
        """只升级本次新追加的 Environment State 块；历史消息零触碰。"""
        barrier = _barrier_with_objects()
        store = FrameStore()
        hooks = _make_hooks(store)
        ctx, _ = _vision_ctx(barrier, hooks=hooks)

        _advance_one_round(barrier)
        ctx.refresh_runtime_state()
        step = int(ctx._runtime_state.get("step"))
        store.record(0, step, _rb_frame(), "pickup_ok")

        system = Message(role="system", content="ORIGINAL SYSTEM")
        history_user = Message(role="user", content="do the thing")
        result = ctx.assemble("SYSTEM PROMPT TEXT", [system, history_user])

        # 历史消息对象身份透传、内容零改动。
        assert result[1] is history_user
        assert history_user.content == "do the thing"
        # 尾消息是新建的 Environment State 块（非历史对象）且已升级为 parts。
        tail = result[-1]
        assert tail is not history_user
        assert isinstance(tail.content, list), tail.content
        assert tail.content[0]["type"] == "text"
        assert "Environment State" in tail.content[0]["text"]
        assert tail.content[1]["type"] == "image_url"

    def test_assemble_stale_frame_note_in_env_block_only(self) -> None:
        barrier = _barrier_with_objects()
        store = FrameStore()
        hooks = _make_hooks(store)
        ctx, _ = _vision_ctx(barrier, hooks=hooks)

        _advance_one_round(barrier)
        ctx.refresh_runtime_state()
        store.record(0, 0, _rb_frame(), "init")  # 旧帧（round 0 != 当前 step）

        history_user = Message(role="user", content="do the thing")
        result = ctx.assemble("SP", [Message(role="system", content="s"), history_user])

        tail = result[-1]
        assert isinstance(tail.content, str)
        assert VISION_NOTE_PREFIX in tail.content
        assert history_user.content == "do the thing"  # 注记只落在环境块

    def test_assemble_raw_strategy_never_upgrades_history(self) -> None:
        barrier = _barrier_with_objects()
        store = FrameStore()
        hooks = _make_hooks(store)
        ctx, _ = _vision_ctx(barrier, hooks=hooks, strategy="raw")

        _advance_one_round(barrier)
        ctx.refresh_runtime_state()
        step = int(ctx._runtime_state.get("step"))
        store.record(0, step, _rb_frame(), "pickup_ok")  # 即使帧是最新的

        history_user = Message(role="user", content="do the thing")
        result = ctx.assemble("SP", [Message(role="system", content="s"), history_user])

        assert result[-1] is history_user  # raw 无环境块 → 不触碰历史
        assert history_user.content == "do the thing"

    def test_assemble_unclosed_tool_call_no_upgrade(self) -> None:
        """未闭合 tool call → 环境块不追加 → 不触碰历史 assistant 消息。"""
        barrier = _barrier_with_objects()
        store = FrameStore()
        hooks = _make_hooks(store)
        ctx, _ = _vision_ctx(barrier, hooks=hooks)

        _advance_one_round(barrier)
        ctx.refresh_runtime_state()
        step = int(ctx._runtime_state.get("step"))
        store.record(0, step, _rb_frame(), "pickup_ok")

        assistant = Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="tc-1",
                    type="function",
                    function=FunctionCall(name="noop", arguments={}),
                )
            ],
        )
        result = ctx.assemble("SP", [Message(role="system", content="s"), assistant])

        assert result[-1] is assistant
        assert assistant.content == ""

    def test_no_hooks_assembles_like_plain_subclass(self) -> None:
        """视 hooks 未注入（None）→ assemble 与基类逐字节一致。"""
        barrier = _barrier_with_objects()
        plain, _ = _vision_ctx(barrier, hooks=None)
        _advance_one_round(barrier)
        plain.refresh_runtime_state()
        messages = [
            Message(role="system", content="s"),
            Message(role="user", content="h"),
        ]

        result = plain.assemble("SP", messages)

        assert [m.role for m in result] == ["system", "user", "user"]
        assert all(isinstance(m.content, str) for m in result)


# ── 5. env_pack 接线 ──────────────────────────────────────────────────────


class _UnityStub:
    """UnityController 替身：记录构造 kwargs（不起真机）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = dict(kwargs)


class TestEnvPackVisionWiring:
    def test_unity_vision_wires_session_and_injects(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """VLM=1 ∧ FRAMES=1 ∧ unity → 视觉 session + 端到端注入 parts。"""
        import ai2thor_orch.env_pack as env_pack_mod
        from ai2thor_orch.executor import unity_controller as uc_mod

        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)

        pack = env_pack_mod.Ai2ThorEnvPack(
            run_dir=tmp_path, agent_names=["Alice", "Bob"]
        )
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=5, mode="unity")
        store = pack.frame_store
        assert isinstance(store, FrameStore)

        provider = pack.build_worker_state_provider(
            _worker_env_ctx(barrier, agent_idx=0)
        )
        factory = pack.build_session_factory(role="worker")
        session = factory()

        assert type(session) is VisionWorkerContextManager
        assert isinstance(session, AI2ThorWorkerContextManager)
        assert session._state_provider is provider
        hooks = session._vision_hooks
        assert isinstance(hooks, VisionHooks)
        assert hooks._frame_store is store and hooks.enabled

        # 工厂可多次调用：session 新实例、hooks 共享（每 worker 一个闭包）。
        assert factory()._vision_hooks is hooks

        # 端到端：step 0 记帧对齐 → assemble 出 parts（真轮次推进的完整性由
        # TestVisionContextAssemble 用 FakeController 覆盖；此处 stub 控制器
        # 不执行 Unity 轮次，故不推进环境。）
        session.refresh_runtime_state()
        store.record(0, 0, _rb_frame(), "pickup_ok")
        result = session.assemble(
            "SP",
            [Message(role="system", content="s"), Message(role="user", content="h")],
        )
        assert isinstance(result[-1].content, list)
        assert result[-1].content[1]["type"] == "image_url"

    def test_switches_off_session_class_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """缺省关：session 类型 / 帧存储零差异。"""
        import ai2thor_orch.env_pack as env_pack_mod
        from ai2thor_orch.executor import unity_controller as uc_mod

        monkeypatch.setattr(uc_mod, "UnityController", _UnityStub)
        pack = env_pack_mod.Ai2ThorEnvPack(
            run_dir=tmp_path, agent_names=["Alice", "Bob"]
        )
        barrier = pack.build_barrier(num_agents=2, seed=42, max_steps=5, mode="unity")

        assert pack.frame_store is None
        pack.build_worker_state_provider(_worker_env_ctx(barrier, agent_idx=0))
        session = pack.build_session_factory(role="worker")()
        assert type(session) is AI2ThorWorkerContextManager
        assert session._vision_hooks if hasattr(session, "_vision_hooks") else True

    def test_vlm_without_frames_warns_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """VLM=1 但 FRAMES 未开：响亮警告 + 通道不激活（不炸）。"""
        import ai2thor_orch.env_pack as env_pack_mod

        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        with _capture_vision_logs(logging.WARNING) as logs:
            pack = env_pack_mod.Ai2ThorEnvPack(run_dir=tmp_path)
            barrier = pack.build_barrier(num_agents=1, seed=1, max_steps=3)

        assert pack.frame_store is None
        warnings = _messages(logs, logging.WARNING)
        assert any("FRAMES" in m for m in warnings)
        pack.build_worker_state_provider(_worker_env_ctx(barrier, agent_idx=0))
        session = pack.build_session_factory(role="worker")()
        assert type(session) is AI2ThorWorkerContextManager

    def test_vision_on_fake_warns_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """VLM=1 ∧ FRAMES=1 但 mode=fake：响亮警告 + 不接线。"""
        import ai2thor_orch.env_pack as env_pack_mod

        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        with _capture_vision_logs(logging.WARNING) as logs:
            pack = env_pack_mod.Ai2ThorEnvPack(run_dir=tmp_path)
            pack.build_barrier(num_agents=1, seed=1, max_steps=3)

        assert pack.frame_store is None
        warnings = _messages(logs, logging.WARNING)
        assert any("fake" in m for m in warnings)

    def test_frames_only_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """FRAMES=1 但 VLM 未开：纯采集 run，不警告。"""
        import ai2thor_orch.env_pack as env_pack_mod

        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        with _capture_vision_logs(logging.WARNING) as logs:
            pack = env_pack_mod.Ai2ThorEnvPack(run_dir=tmp_path)
            pack.build_barrier(num_agents=1, seed=1, max_steps=3)

        assert not [r for r in logs.records if r.levelno == logging.WARNING]


# ── 5b. warn_misconfigured_vision 单元（含防御分支）───────────────────────


class TestWarnMisconfigured:
    def test_vlm_off_silent(self) -> None:
        with _capture_vision_logs(logging.DEBUG) as logs:
            warn_misconfigured_vision(mode="unity", frame_store=None)
        assert not logs.records

    def test_unity_store_missing_defensive_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        with _capture_vision_logs(logging.WARNING) as logs:
            warn_misconfigured_vision(mode="unity", frame_store=None)
        assert any("帧存储未接线" in m for m in _messages(logs, logging.WARNING))

    def test_all_ready_logs_activation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEIGHT", "480")
        with _capture_vision_logs(logging.INFO) as logs:
            warn_misconfigured_vision(mode="unity", frame_store=object())
        infos = _messages(logs, logging.INFO)
        assert any("640x480" in m for m in infos)


# ── 6. 开关 / metadata 三字段 ─────────────────────────────────────────────


class TestVisionConfigAndMetadata:
    def test_switches_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 缺省关
        assert not vlm_enabled()
        assert not vision_switches_ready(mode="unity")
        assert not vision_channel_active(mode="unity", frame_store=object())
        # 单开 VLM
        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        assert vlm_enabled() and not vision_switches_ready(mode="unity")
        # VLM + FRAMES，非 unity
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        assert not vision_switches_ready(mode="fake")
        # 全就位
        assert vision_switches_ready(mode="unity")
        assert vision_channel_active(mode="unity", frame_store=object())
        assert not vision_channel_active(mode="unity", frame_store=None)
        # 0/false/no/off 口径与 FRAMES 一致
        monkeypatch.setenv(VLM_SWITCH_ENV, "off")
        assert not vlm_enabled()

    def test_frame_resolution_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert frame_resolution() == (300, 300)
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEIGHT", "480")
        assert frame_resolution() == (640, 480)

    def test_resolve_vision_config_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = resolve_vision_config(mode="unity", model="m")
        assert cfg == {
            "vision_enabled": False,
            "vision_model": None,
            "frame_resolution": None,
        }

    def test_resolve_vision_config_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEIGHT", "480")
        cfg = resolve_vision_config(mode="unity", model="deepseek-flash")
        assert cfg == {
            "vision_enabled": True,
            "vision_model": "deepseek-flash",
            "frame_resolution": [640, 480],
        }
        # 开关全开但 fake：仍不激活（双条件含 unity）。
        assert resolve_vision_config(mode="fake", model="m")["vision_enabled"] is False

    def test_build_run_metadata_includes_vision_fields(self) -> None:
        from ai2thor_orch.contracts.task import load_task
        from ai2thor_orch.experiment.ai2thor_experiment import build_run_metadata

        contract = load_task("3_transport_groceries")

        def _md(**over: Any) -> dict:
            base: dict[str, Any] = {
                "run_id": "r",
                "task_id": "3_transport_groceries",
                "scene": "FloorPlan1",
                "num_agents": 2,
                "agent_names": ["Alice", "Bob"],
                "seed": 42,
                "mode": "fake",
                "max_steps": 10,
                "wall_clock_limit": 60.0,
                "step_timeout": 60.0,
                "model": "m",
                "provider": "openai",
                "api_base": "http://localhost:1",
                "contract": contract,
            }
            base.update(over)
            return build_run_metadata(**base)

        md = _md()
        assert md["vision_enabled"] is False
        assert md["vision_model"] is None
        assert md["frame_resolution"] is None
        md2 = _md(
            vision_enabled=True,
            vision_model="deepseek-flash",
            frame_resolution=[640, 480],
        )
        assert md2["vision_enabled"] is True
        assert md2["vision_model"] == "deepseek-flash"
        assert md2["frame_resolution"] == [640, 480]

    def test_run_experiment_records_vision_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """端到端：双开关 + unity → spec.metadata 三字段落真值。"""
        import ai2thor_orch.experiment.ai2thor_experiment as exp_mod

        monkeypatch.setenv(VLM_SWITCH_ENV, "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_FRAMES", "1")
        monkeypatch.setenv("LLAMAR_AI2THOR_WIDTH", "640")
        monkeypatch.setenv("LLAMAR_AI2THOR_HEIGHT", "480")
        captured: dict[str, Any] = {}

        async def _spy(spec: Any) -> dict:
            captured["spec"] = spec
            return {"run_id": spec.run_id, "steps": 0, "finished": False}

        monkeypatch.setattr(exp_mod, "run_assembly", _spy)
        asyncio.run(
            exp_mod.run_experiment(
                num_agents=2, seed=42, mode="unity", max_steps=4, log_dir=str(tmp_path)
            )
        )

        md = captured["spec"].metadata
        assert md["vision_enabled"] is True
        assert md["vision_model"] == captured["spec"].model
        assert md["frame_resolution"] == [640, 480]

    def test_run_experiment_default_off_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import ai2thor_orch.experiment.ai2thor_experiment as exp_mod

        captured: dict[str, Any] = {}

        async def _spy(spec: Any) -> dict:
            captured["spec"] = spec
            return {"run_id": spec.run_id, "steps": 0, "finished": False}

        monkeypatch.setattr(exp_mod, "run_assembly", _spy)
        asyncio.run(
            exp_mod.run_experiment(
                num_agents=2, seed=42, mode="fake", max_steps=4, log_dir=str(tmp_path)
            )
        )

        md = captured["spec"].metadata
        assert md["vision_enabled"] is False
        assert md["vision_model"] is None
        assert md["frame_resolution"] is None


# ── 7. 依赖方向探针（不 import sar_orch）───────────────────────────────────


class TestImportIndependence:
    def test_import_vision_hooks_without_sar_orch(self) -> None:
        code = textwrap.dedent(
            """
            import sys

            import ai2thor_orch.vision_hooks  # noqa: F401

            leaked = sorted(
                m for m in sys.modules
                if m == 'sar_orch' or m.startswith('sar_orch.')
            )
            print('LEAKED=' + repr(leaked))
            sys.exit(1 if leaked else 0)
            """
        ).strip()
        env = dict(os.environ)
        env["PYTHONPATH"] = "src" + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_jpeg_quality_default_is_stable() -> None:
    """JPEG 质量缺省口径（C1 smoke 同源；改动须同步设计文档）。"""
    assert JPEG_QUALITY_DEFAULT == 80

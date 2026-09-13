"""Phase 5（P5）防回归：coordinator-only long-term read-port 注入。

覆盖（主方案 Phase 5）：
- #1/#2：``MemoryReadPort.long_term_memory()`` published-only 读取；
- #3：同 env_step 下长期 revision 变化 → ``freshness["long_term_revision"]``
  反映（可观测 metadata，不依赖 env step）；
- #5：shadow 模式 provider 不注入长期段、渲染器不渲染；
- 双保险：context ``_render_read_port_block`` 透传 provider.long_term_mode；
- G2-3：``publish_memory`` 支持可选 (scope_id, event_id, source_revision,
  event_digest) 四元组落库；二元组保持 NULL 合法（可选列语义）。

新建本文件；test_long_term_environment_state.py 的 SECTION_PRIORITY[:4]
断言随 P5 冻结顺序更新（父侧裁决，见 G3 审查包披露）。
"""

from __future__ import annotations

import pytest

from a2a.coordinator.memory.contracts import MemoryConfig
from a2a.coordinator.memory.ingestor import MemoryScopeFactory
from a2a.coordinator.memory.long_term import LongTermMemoryStore
from a2a.coordinator.memory.store import MemoryStore
from Agent.environment_state import (
    EnvironmentStateQuery,
    Freshness,
    render_environment_state_view,
)
from sar_orch.environment_state_provider import (
    ControlPlaneReadPort,
    EnvironmentStateProvider,
    MemoryReadPort,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope_factory(tmp_path):
    return MemoryScopeFactory(MemoryConfig(experiment_id="run-1", memory_root=tmp_path))


@pytest.fixture
def lt_store(tmp_path):
    return LongTermMemoryStore(tmp_path / "long_term" / "long_term.sqlite3").open()


class FakeRuntime:
    """Control-plane double（与既有 ACL 测试同构）。"""

    def __init__(self):
        self._dispatches = {}

    @property
    def dispatches(self):
        return self._dispatches

    def control_revision_of(self, dispatch_id):
        return 0


def _provider(
    store,
    scope_id,
    *,
    lt_store=None,
    long_term_mode="read",
    viewer_role="coordinator",
    viewer_id="system",
):
    return EnvironmentStateProvider(
        MemoryReadPort(
            store,
            scope_id,
            long_term_store=lt_store,
            long_term_mode=long_term_mode,
        ),
        ControlPlaneReadPort(FakeRuntime()),
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        long_term_mode=long_term_mode,
        long_term_store=lt_store,
    )


def _query(scope_id, *, viewer_role="coordinator", viewer_id="system", budget=100000):
    return EnvironmentStateQuery(
        scope_id=scope_id,
        viewer_role=viewer_role,
        viewer_id=viewer_id,
        token_budget=budget,
    )


def _publish(lt_store, scope_id, key, statement, kind="lesson", confidence=0.9):
    lt_store.publish_memory(
        project_id="llamar",
        scope_id=scope_id,
        memory_key=key,
        statement=statement,
        kind=kind,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Phase 5 #3 —— 同 env_step 下长期 revision 变化反映到 freshness
# ---------------------------------------------------------------------------


def test_long_term_revision_reflected_in_freshness_same_env_step(
    store, scope_factory, lt_store
):
    """长期 revision 是 freshness 可观测 metadata：不依赖 env_step 推进，
    每次 publish 递增。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    provider = _provider(store, scope_id, lt_store=lt_store, long_term_mode="read")

    v0 = provider.query_environment_state(_query(scope_id))
    assert v0.sections["freshness"]["long_term_revision"] == 0

    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    v1 = provider.query_environment_state(_query(scope_id))
    assert v1.sections["freshness"]["long_term_revision"] == 1

    _publish(lt_store, scope_id, "k2", "keep water for chemical fires")
    v2 = provider.query_environment_state(_query(scope_id))
    assert v2.sections["freshness"]["long_term_revision"] == 2


def test_read_port_long_term_memory_published_only(store, scope_factory, lt_store):
    """``long_term_memory()`` 只返回 published 行：supersede 后旧行不再出现。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    port = MemoryReadPort(
        store, scope_id, long_term_store=lt_store, long_term_mode="read"
    )
    assert port.long_term_memory() == []

    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    _publish(lt_store, scope_id, "k1", "coordinate at fires and stay back")

    entries = port.long_term_memory()
    assert len(entries) == 1  # superseded 旧行被排除
    assert entries[0]["memory_key"] == "k1"
    assert entries[0]["status"] == "published"
    assert entries[0]["statement"] == "coordinate at fires and stay back"
    assert entries[0]["kind"] == "lesson"
    assert entries[0]["confidence"] == 0.9


def test_read_port_long_term_memory_requires_read_mode_and_store(
    store, scope_factory, lt_store
):
    """未接线 store 或非 read 模式 → []，绝不报错。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _publish(lt_store, scope_id, "k1", "coordinate at fires")

    bare = MemoryReadPort(store, scope_id)
    assert bare.long_term_memory() == []

    shadow = MemoryReadPort(
        store, scope_id, long_term_store=lt_store, long_term_mode="shadow"
    )
    assert shadow.long_term_memory() == []


def test_worker_view_never_gets_long_term_section_even_in_read_mode(
    store, scope_factory, lt_store
):
    """ACL 防回归：即使 provider 处于 read 模式且有长期条目，worker 视图
    也绝不包含长期段（Phase 5 #2 coordinator-only）。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    provider = _provider(
        store,
        scope_id,
        lt_store=lt_store,
        long_term_mode="read",
        viewer_role="worker",
        viewer_id="Alice",
    )
    view = provider.query_environment_state(
        _query(scope_id, viewer_role="worker", viewer_id="Alice")
    )
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" not in view.sections
    assert "long_term_revision" not in view.sections["freshness"]


# ---------------------------------------------------------------------------
# Phase 5 #5 —— shadow 模式不注入、不渲染
# ---------------------------------------------------------------------------


def test_shadow_mode_provider_injects_no_long_term_section(
    store, scope_factory, lt_store
):
    """shadow 模式（coordinator）不注入长期段；freshness 仍携带长期 revision
    （可观测 metadata），但渲染器在 shadow 下也不输出长期标题。"""
    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    provider = _provider(store, scope_id, lt_store=lt_store, long_term_mode="shadow")
    view = provider.query_environment_state(_query(scope_id))
    assert view.freshness is Freshness.FRESH
    assert "long_term_memory" not in view.sections
    assert view.sections["freshness"]["long_term_revision"] == 1

    rendered = render_environment_state_view(view, long_term_mode="shadow")
    assert "Long-term Memory" not in rendered


# ---------------------------------------------------------------------------
# 双保险 —— context._render_read_port_block 透传 provider.long_term_mode
# ---------------------------------------------------------------------------


def test_context_read_port_block_passes_provider_long_term_mode():
    """read_port 渲染路径把 provider 的 long_term_mode 透传给渲染器：
    read → 输出长期段；shadow → 不输出。"""
    from Agent.environment_state import EnvironmentStateView, Freshness
    from Agent.router_agent.context import ContextConfig, CoordinatorContextManager

    def _make_provider(lt_mode):
        class FakeProvider:
            scope_id = "scope-r"
            viewer_role = "coordinator"
            viewer_id = "system"
            current_dispatch_id = None
            long_term_mode = lt_mode

            def query_environment_state(self, query):
                return EnvironmentStateView(
                    Freshness.FRESH,
                    source_revision=1,
                    sections={
                        "long_term_memory": {
                            "k1": {
                                "kind": "lesson",
                                "statement": "coordinate at fires",
                                "confidence": 0.9,
                            }
                        },
                        "freshness": {
                            "scope_id": self.scope_id,
                            "memory_revision": 1,
                        },
                        "next_cursor": 1,
                    },
                )

        return FakeProvider()

    ctx_read = CoordinatorContextManager(
        config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=_make_provider("read"),
    )
    read_text = ctx_read._render_read_port_block()
    assert "### Long-term Memory" in read_text
    assert "coordinate at fires" in read_text

    ctx_shadow = CoordinatorContextManager(
        config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=_make_provider("shadow"),
    )
    shadow_text = ctx_shadow._render_read_port_block()
    assert "Long-term Memory" not in shadow_text


# ---------------------------------------------------------------------------
# G2-3 —— long_term_support 可选列（source_revision / event_digest）
# ---------------------------------------------------------------------------


def test_publish_memory_support_ref_four_tuple_backfills_optional_columns(
    tmp_path,
):
    """四元组 (scope_id, event_id, source_revision, event_digest) 落库：
    source_revision / event_digest 正确写入。"""
    store = LongTermMemoryStore(tmp_path / "long_term" / "long_term.sqlite3")
    store.open()
    store.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="s1",
        source_refs=[("scope-a", "evt-1", 7, "digest-abc")],
    )
    rows = store.list_support_rows(
        project_id="llamar", scope_id="run-a", memory_key="k1"
    )
    assert len(rows) == 1
    assert rows[0]["source_scope_id"] == "scope-a"
    assert rows[0]["source_event_id"] == "evt-1"
    assert rows[0]["source_revision"] == 7
    assert rows[0]["event_digest"] == "digest-abc"
    # 既有 2-tuple 读取形状不受影响
    assert ("scope-a", "evt-1") in store.list_support_refs(
        project_id="llamar", scope_id="run-a", memory_key="k1"
    )


def test_publish_memory_support_ref_two_tuple_keeps_optional_columns_null(
    tmp_path,
):
    """二元组保持 source_revision / event_digest 为 NULL（可选列语义，合法）。"""
    store = LongTermMemoryStore(tmp_path / "long_term" / "long_term.sqlite3")
    store.open()
    store.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="s1",
        source_refs=[("scope-a", "evt-1")],
    )
    rows = store.list_support_rows(
        project_id="llamar", scope_id="run-a", memory_key="k1"
    )
    assert len(rows) == 1
    assert rows[0]["source_revision"] is None
    assert rows[0]["event_digest"] is None


def test_publish_memory_support_ref_mixed_shapes_and_invalid_length_rejected(
    tmp_path,
):
    """同一 publish 可混合二元组与四元组；非法长度（3 元组）必须被拒。"""
    from a2a.coordinator.memory.contracts import MemoryContractError

    store = LongTermMemoryStore(tmp_path / "long_term" / "long_term.sqlite3")
    store.open()
    store.publish_memory(
        project_id="llamar",
        scope_id="run-a",
        memory_key="k1",
        statement="s1",
        source_refs=[("scope-a", "evt-1"), ("scope-a", "evt-2", 7, "digest-abc")],
    )
    rows = store.list_support_rows(
        project_id="llamar", scope_id="run-a", memory_key="k1"
    )
    by_event = {r["source_event_id"]: r for r in rows}
    assert by_event["evt-1"]["source_revision"] is None
    assert by_event["evt-1"]["event_digest"] is None
    assert by_event["evt-2"]["source_revision"] == 7
    assert by_event["evt-2"]["event_digest"] == "digest-abc"

    with pytest.raises(MemoryContractError):
        store.publish_memory(
            project_id="llamar",
            scope_id="run-a",
            memory_key="k2",
            statement="s2",
            source_refs=[("scope-a", "evt-3", 1)],  # 3 元组非法
        )


# ---------------------------------------------------------------------------
# P5 review M-1 —— shadow + read 组合 fail closed（构造期交叉校验）
# ---------------------------------------------------------------------------


def test_shadow_read_mode_combo_rejected_fail_closed():
    """``memory_read_mode="shadow"`` + ``long_term_mode="read"`` 必须被拒：
    H2 shadow compare 会把长期读注入当作非 allowlist diff（.long_term_memory）
    污染 rollout audit。P5 review M-1 父侧裁决：构造期交叉校验，typed
    ``MemoryConfigError``（code=invalid_mode_combo）。"""
    from a2a.coordinator.memory.contracts import MemoryConfigError
    from sar_orch.coordinator import _validate_long_term_mode_combo

    with pytest.raises(MemoryConfigError) as exc:
        _validate_long_term_mode_combo("shadow", "read")
    assert exc.value.code == "invalid_mode_combo"


def test_long_term_mode_combo_allowed_combinations():
    """合法组合不 raise：read_port+read（G3 目标场景）、shadow+shadow/off、
    read_port+shadow/off。"""
    from sar_orch.coordinator import _validate_long_term_mode_combo

    _validate_long_term_mode_combo("read_port", "read")
    _validate_long_term_mode_combo("shadow", "shadow")
    _validate_long_term_mode_combo("shadow", "off")
    _validate_long_term_mode_combo("read_port", "shadow")
    _validate_long_term_mode_combo("read_port", "off")


def test_sar_coordinator_init_rejects_shadow_read_combo():
    """SARCoordinator 构造即 fail closed：shadow+read 在 memory auth 检查之前
    就被 combo 校验拦截（无需 secret/log_dir 即可复现）。"""
    from a2a.coordinator.memory.contracts import MemoryConfigError
    from sar_orch.coordinator import SARCoordinator

    with pytest.raises(MemoryConfigError) as exc:
        SARCoordinator(
            host="localhost",
            port=8080,
            a2a_port=8081,
            barrier=None,
            memory_read_mode="shadow",
            long_term_mode="read",
        )
    assert exc.value.code == "invalid_mode_combo"


# ---------------------------------------------------------------------------
# P5 review m5 —— budget 边界（长期段阈值 3）
# ---------------------------------------------------------------------------


def test_budget_below_long_term_threshold_drops_section_and_truncates(
    store, scope_factory, lt_store
):
    """budget=2（低于长期段阈值 3）：coordinator read 视图 TRUNCATED=True
    且无 long_term_memory 段（不能静默消失）。"""
    from Agent.environment_state import TRUNCATED_KEY

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    provider = _provider(store, scope_id, lt_store=lt_store, long_term_mode="read")
    view = provider.query_environment_state(_query(scope_id, budget=2))
    assert view.sections.get(TRUNCATED_KEY) is True
    assert "long_term_memory" not in view.sections


def test_budget_at_long_term_threshold_keeps_section(
    store, scope_factory, lt_store
):
    """budget=3（等于长期段阈值 3）：长期段保留，TRUNCATED 不为 True。"""
    from Agent.environment_state import TRUNCATED_KEY

    scope_id = scope_factory.resolve("ctx-1", 0).scope_id
    _publish(lt_store, scope_id, "k1", "coordinate at fires")
    provider = _provider(store, scope_id, lt_store=lt_store, long_term_mode="read")
    view = provider.query_environment_state(_query(scope_id, budget=3))
    assert "long_term_memory" in view.sections
    assert view.sections.get(TRUNCATED_KEY) is not True

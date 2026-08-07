"""Tests for SARWorkerStateProvider and Worker Context auto-refresh."""

import asyncio
import time
from unittest.mock import MagicMock

import httpx

from Agent.router_agent.state_provider import RuntimeState
from Agent.worker_agent.context import (
    ContextConfig,
    WorkerContextManager,
    WorkerPinnedState,
)
from Agent.worker_agent.schema import Message
from sar_orch.worker_state_provider import SARWorkerStateProvider


class MockBarrier:
    def __init__(self, step=0, finished=False):
        self._step_counter = step
        self._finished = finished
        self.env = MagicMock()
        self.env.controller = MagicMock()
        self.env.controller.get = MagicMock()
        self.env.controller.get_inventory = MagicMock()

    def is_finished(self):
        return self._finished

    def get_current_obs(self, agent_idx):
        return "Fire at (1,2,0) intensity high\nPerson at (3,4,0) trapped"


def _make_mock_barrier_agent(pos=(2, 3, 0), inventory=None, step=0, finished=False):
    barrier = MockBarrier(step=step, finished=finished)
    agent = MagicMock()
    agent.get_position.return_value = pos
    barrier.env.controller.get.return_value = agent
    barrier.env.controller.get_inventory.return_value = inventory or []
    return barrier


def test_provider_returns_runtime_state_payload():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), inventory=["Water"], step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    state = provider.snapshot()

    assert isinstance(state, RuntimeState)
    assert state.version == (5, 0, ("", -1, False))
    assert state.env_step == 5
    assert not state.stale
    assert state.refresh_error == ""
    assert state.get("position") == (2, 3, 0)
    assert state.get("inventory") == ["Water"]
    assert state.get("step") == 5
    assert state.get("mission_status") == "in_progress"


def test_provider_version_caching():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    s2 = provider.snapshot()

    assert s1 is s2
    assert s1.version == (5, 0, ("", -1, False))


def test_provider_version_change_triggers_new_snapshot():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    assert s1.version == (5, 0, ("", -1, False))

    barrier._step_counter = 6
    s2 = provider.snapshot()
    assert s2.version == (6, 0, ("", -1, False))
    assert s2 is not s1


def test_provider_stale_fallback_on_error():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)
    s1 = provider.snapshot()
    assert not s1.stale

    # Force an outer-level exception (not caught by inner position/inventory try/except)
    barrier._step_counter = 6  # force new version
    barrier.is_finished = MagicMock(side_effect=RuntimeError("connection lost"))
    s2 = provider.snapshot()
    assert s2.stale
    assert "connection lost" in s2.refresh_error
    assert s2.get("position") == (2, 3, 0)  # previous data preserved


def test_provider_no_barrier():
    provider = SARWorkerStateProvider(barrier=None, agent_idx=0)
    state = provider.snapshot()
    assert state.version == (0, 0, ("", -1, False))
    assert state.get("position") is None
    assert state.get("step") == 0
    assert state.get("mission_status") == "in_progress"


def test_provider_empty_stale_on_first_failure_no_prior():
    provider = SARWorkerStateProvider(barrier=None, agent_idx=0)
    provider._barrier = MagicMock()  # broken barrier
    provider._barrier._step_counter = 0
    provider._barrier.is_finished.side_effect = RuntimeError("no env")

    state = provider.snapshot()
    assert state.stale
    assert state.version == 0
    assert state.payload == {}


def test_provider_observes_age_ms():
    barrier = _make_mock_barrier_agent(pos=(2, 3, 0), step=5)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    s1 = provider.snapshot()
    # First snapshot has age_ms=0 (no prior snapshot)
    assert s1.get("age_ms", -1) == 0.0

    # Advance the env step so a new snapshot is created
    barrier._step_counter = 6
    barrier.env.controller.get.return_value.get_position.return_value = (2, 3, 0)

    time.sleep(0.01)
    s2 = provider.snapshot()
    # Second snapshot has positive age_ms (time since s1 was created)
    assert s2.get("age_ms", -1) > 0


def test_worker_context_manager_accepts_state_provider():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )

    result = ctx.refresh_runtime_state()
    assert result is not None
    assert isinstance(result, RuntimeState)
    assert ctx._runtime_state is not None
    assert ctx._runtime_state.get("position") == (4, 5, 0)
    assert ctx._runtime_state.get("inventory") == ["Sand"]


def test_worker_context_manager_no_provider():
    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid"),
        state_provider=None,
    )
    result = ctx.refresh_runtime_state()
    assert result is None


def test_worker_context_manager_projects_to_pinned_state():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    ps = ctx._pinned_state
    assert isinstance(ps, WorkerPinnedState)
    assert ps.position == (4, 5, 0)
    assert ps.inventory == ["Sand"]
    assert ps.step == 3
    assert ps.mission_status == "in_progress"
    assert ps.state_mode == "semantic"


def test_context_renders_injected_state():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), inventory=["Sand"], step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    sys_prompt = "Test system prompt"
    messages = [Message(role="system", content=sys_prompt)]

    assembled = ctx.assemble(sys_prompt, messages)
    mem_text = assembled[-1].content if len(assembled) > 1 else ""

    assert "### Current State" in mem_text
    assert "Position:" in mem_text
    assert "(4, 5, 0)" in mem_text
    assert "Inventory:" in mem_text
    assert "Sand" in mem_text
    assert "Step: 3" in mem_text


def test_context_renders_without_provider():
    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=None,
    )

    sys_prompt = "Test"
    messages = [Message(role="system", content=sys_prompt)]
    assembled = ctx.assemble(sys_prompt, messages)

    mem_text = assembled[-1].content if len(assembled) > 1 else ""
    assert "### Current State" in mem_text


def test_extract_pinned_complements_auto_inject():
    barrier = _make_mock_barrier_agent(pos=(4, 5, 0), step=3)
    provider = SARWorkerStateProvider(barrier=barrier, agent_idx=0)

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", pinned_enabled=True),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    assert ctx._pinned_state.position == (4, 5, 0)

    # Simulate tool result extract (NavigateTo changes position)
    tool_content = "[GPS] Position: (7, 8, 0) | Inventory: ['Water'] | Step: 4"
    extracted = ctx._extract_pinned("navigate_to", tool_content, True)
    assert extracted is not None
    assert extracted["position"] == (7, 8, 0)

    # Apply extraction
    ctx._pinned_state.position = extracted["position"]
    ctx._pinned_state.inventory = extracted["inventory"]
    ctx._pinned_state.step = extracted["step"]
    ctx.pinned.update(extracted)

    assert ctx._pinned_state.position == (7, 8, 0)

    # Next refresh restores auto-injected value
    barrier.env.controller.get.return_value.get_position.return_value = (4, 5, 0)
    ctx.refresh_runtime_state()
    assert ctx._pinned_state.position == (4, 5, 0)


# ---------------------------------------------------------------------------
# Phase 4: authenticated read-port provider (no global-map direct read)
# ---------------------------------------------------------------------------


def test_worker_read_port_uses_injected_environment_state_client():
    """In read_port mode the worker uses an authenticated /environment-state
    client instead of constructing a global-map direct-read view."""
    from Agent.environment_state import (
        FRESHNESS_SECTION,
        EnvironmentStateView,
        Freshness,
    )

    captured = {}

    class FakeClient:
        async def fetch(self, query_viewer):
            captured["viewer"] = query_viewer
            return EnvironmentStateView(
                Freshness.FRESH,
                source_revision=7,
                sections={
                    "embodied_state": {
                        "Alice": {
                            "entity_type": "agent",
                            "fields": {"position": {"value": [3, 4, 0]}},
                        }
                    },
                    FRESHNESS_SECTION: {"scope_id": "scope-1", "memory_revision": 7},
                    "next_cursor": 3,
                },
            )

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
    )
    provider._agent_name = "Alice"
    provider.set_environment_state_client(FakeClient())

    import asyncio

    asyncio.run(provider.fetch_environment_state_async())

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()
    assembled = ctx.assemble("system", [Message(role="system", content="system")])
    assert captured.get("viewer") == "Alice"
    assert assembled[-1].role == "user"
    assert "## Environment State" in assembled[-1].content
    assert (
        "Spatial State" not in assembled[-1].content
        or "Embodied State" in assembled[-1].content
    )


def test_worker_read_port_unavailable_without_fetch():
    """A missing authenticated projection renders UNAVAILABLE, never stale reuse."""
    from Agent.environment_state import Freshness

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
    )
    provider._agent_name = "Alice"
    view = provider.query_environment_state(
        MagicMock(scope_id="s", viewer_id="Alice", viewer_role="worker")
    )
    assert view.freshness == Freshness.UNAVAILABLE


# ---------------------------------------------------------------------------
# Phase 4 (H2): worker read_port -> legacy rollback latch + redacted audit
# ---------------------------------------------------------------------------

_SECRET = b"0123456789abcdef0123456789abcdef"


class _HttpxCapture:
    """Factory replacing ``httpx.AsyncClient`` with a capture client.

    ``monkeypatch.setattr(httpx, "AsyncClient", _HttpxCapture.for_config(cfg))`` —
    the returned object is a callable class, so ``httpx.AsyncClient(timeout=...)``
    constructs a per-call capture client.
    """

    @staticmethod
    def for_config(captured, *, status=200, payload=None, raise_error=None):
        payload = payload if payload is not None else {}

        class _CaptureClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json):
                captured["url"] = url
                captured["payload"] = json
                if raise_error is not None:
                    raise raise_error

                class _FakeResponse:
                    def raise_for_status(self):
                        if status >= 400:
                            raise RuntimeError(f"HTTP {status} forbidden")

                    def json(self):
                        return payload

                return _FakeResponse()

        return _CaptureClient


def _read_port_provider(*, tmp_path=None, token_limit=80000, **kwargs):
    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        coordinator_secret=_SECRET,
        environment_state_url="http://coordinator:8080",
        log_dir=str(tmp_path) if tmp_path is not None else None,
        token_limit=token_limit,
        **kwargs,
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id("task-alice-1")
    return provider


def _audit_rollback_lines(tmp_path):
    audit = tmp_path / "memory_rollout_audit.ndjson"
    if not audit.exists():
        return []
    return [
        line
        for line in audit.read_text(encoding="utf-8").strip().splitlines()
        if "read_port_to_legacy_rollback" in line
    ]


def test_worker_read_port_http_failure_latches_rollback_single_audit(
    monkeypatch, tmp_path
):
    """A production HTTP/ACL failure latches the worker read_port→legacy
    rollback exactly once and writes a redacted audit, matching the
    coordinator behavior."""
    from Agent.environment_state import Freshness

    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            raise_error=RuntimeError("403 Forbidden: environment_state_unauthorized"),
        ),
    )

    provider = _read_port_provider(tmp_path=tmp_path)
    asyncio.run(provider.fetch_environment_state_async())

    assert provider.rollout_rolled_back() is True
    assert provider.rollout_active() is False
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )
    assert len(_audit_rollback_lines(tmp_path)) == 1
    assert "http_error" in _audit_rollback_lines(tmp_path)[0]

    # Subsequent fetch attempts stay latched → still exactly one audit line.
    asyncio.run(provider.fetch_environment_state_async())
    assert len(_audit_rollback_lines(tmp_path)) == 1


def test_worker_read_port_http_sends_nonzero_budget_and_fresh_sections(monkeypatch):
    """The production HTTP fetch sends a nonzero token_budget derived from the
    context token budget so the canonical view is served non-truncated."""
    from Agent.environment_state import FRESHNESS_SECTION, TRUNCATED_KEY, Freshness

    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            payload={
                "freshness": "FRESH",
                "source_revision": 9,
                "sections": {
                    "spatial_state": {
                        "FireA": {
                            "entity_type": "fire",
                            "fields": {"intensity": {"value": "High"}},
                        }
                    },
                    "embodied_state": {
                        "Alice": {
                            "entity_type": "agent",
                            "fields": {"position": {"value": [3, 4, 0]}},
                        }
                    },
                    "task_execution_state": [],
                    "relevant_events": [],
                    FRESHNESS_SECTION: {
                        "scope_id": "scope-1",
                        "memory_revision": 9,
                        "view_revision": 9,
                    },
                    "next_cursor": 9,
                },
            },
        ),
    )

    provider = _read_port_provider(token_limit=80000)
    asyncio.run(provider.fetch_environment_state_async())

    assert captured["url"].endswith("/environment-state")
    assert captured["payload"]["token_budget"] == 80000 - 1024
    assert captured["payload"]["token_budget"] > 0
    assert provider.rollout_rolled_back() is False

    view = provider.query_environment_state(MagicMock())
    assert view.freshness == Freshness.FRESH
    sections = view.sections
    assert sections.get(TRUNCATED_KEY) is not True
    assert sections["spatial_state"]  # non-truncated canonical section
    assert sections["embodied_state"]["Alice"]
    assert sections[FRESHNESS_SECTION]["scope_id"] == "scope-1"


def test_worker_read_port_unavailable_response_rolls_back(monkeypatch, tmp_path):
    """A non-fresh (UNAVAILABLE) server response latches the rollback + audit."""
    from Agent.environment_state import Freshness

    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            payload={
                "freshness": "UNAVAILABLE",
                "reason": "environment_state_scope_mismatch",
                "sections": {},
            },
        ),
    )

    provider = _read_port_provider(tmp_path=tmp_path)
    asyncio.run(provider.fetch_environment_state_async())

    assert provider.rollout_rolled_back() is True
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )
    lines = _audit_rollback_lines(tmp_path)
    assert len(lines) == 1
    assert "environment_state_not_fresh" in lines[0]


def test_worker_read_port_rollback_audit_redacts_secret_in_reason(
    monkeypatch, tmp_path
):
    """The rollback audit reason is redacted so failure details never leak."""
    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            raise_error=RuntimeError(
                "credential=SECRET_TOKEN123 mailbox_body=PRIVATE999"
            ),
        ),
    )

    provider = _read_port_provider(tmp_path=tmp_path)
    asyncio.run(provider.fetch_environment_state_async())

    raw = (tmp_path / "memory_rollout_audit.ndjson").read_text(encoding="utf-8")
    assert "SECRET_TOKEN123" not in raw
    assert "PRIVATE999" not in raw
    assert "REDACTED" in raw


def test_worker_context_read_port_unavailable_renders_legacy_not_mixed(tmp_path):
    """A worker read-port provider in an unavailable state renders the legacy
    pinned block and latches the rollback — canonical and legacy truth are
    never mixed in one request."""
    from Agent.environment_state import Freshness

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        log_dir=str(tmp_path),
    )
    provider._agent_name = "Alice"

    ctx = WorkerContextManager(
        config=ContextConfig(strategy="hybrid", memory_read_mode="read_port"),
        state_provider=provider,
    )
    ctx.refresh_runtime_state()

    assembled = ctx.assemble("system", [Message(role="system", content="system")])
    block = assembled[-1].content if len(assembled) > 1 else ""

    assert provider.rollout_rolled_back() is True
    assert provider.rollout_active() is False
    # Legacy pinned render path, NOT the canonical read-port projection.
    assert "### Current State" in block
    assert "Spatial State" not in block
    assert "Embodied State" not in block

    # Exactly one rollback audit line; a second render stays legacy + one line.
    assert len(_audit_rollback_lines(tmp_path)) == 1
    assembled2 = ctx.assemble("system", [Message(role="system", content="system")])
    block2 = assembled2[-1].content if len(assembled2) > 1 else ""
    assert "Spatial State" not in block2
    assert len(_audit_rollback_lines(tmp_path)) == 1
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Phase 4 (H2): worker read-port hardening — fail-closed + pre-LLM safety
# ---------------------------------------------------------------------------


def test_worker_read_port_missing_freshness_fails_closed(monkeypatch, tmp_path):
    """A response whose freshness is missing/null is never cached as fresh
    truth — it latches the rollback and writes a redacted audit."""
    from Agent.environment_state import Freshness

    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            payload={
                "freshness": None,
                "reason": "no freshness header",
                "sections": {
                    "spatial_state": {"FireA": {"entity_type": "fire", "fields": {}}},
                },
            },
        ),
    )

    provider = _read_port_provider(tmp_path=tmp_path)
    asyncio.run(provider.fetch_environment_state_async())

    assert provider.rollout_rolled_back() is True
    assert provider.rollout_active() is False
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )
    lines = _audit_rollback_lines(tmp_path)
    assert len(lines) == 1
    assert "environment_state_not_fresh" in lines[0]
    assert "missing" in lines[0]


def test_worker_read_port_injected_client_error_never_breaks_pre_llm(tmp_path):
    """An injected /environment-state client that raises must never propagate
    through pre_llm — the failure latches the rollback (once) with an audit."""
    from Agent.environment_state import Freshness

    class _BoomClient:
        async def fetch(self, query_viewer):
            raise RuntimeError("injected client exploded")

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        coordinator_secret=_SECRET,
        log_dir=str(tmp_path),
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id("task-alice-1")
    provider.set_environment_state_client(_BoomClient())

    asyncio.run(provider.fetch_environment_state_async())  # must not raise

    assert provider.rollout_rolled_back() is True
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )
    lines = _audit_rollback_lines(tmp_path)
    assert len(lines) == 1
    assert "injected_client_error" in lines[0]


def test_worker_read_port_injected_client_none_fails_closed(tmp_path):
    """An injected client returning no view is a failure (never stale reuse) —
    it latches the rollback with an audit."""
    from Agent.environment_state import Freshness

    class _NoneClient:
        async def fetch(self, query_viewer):
            return None

    provider = SARWorkerStateProvider(
        barrier=None,
        agent_idx=0,
        memory_read_mode="read_port",
        coordinator_secret=_SECRET,
        log_dir=str(tmp_path),
    )
    provider._agent_name = "Alice"
    provider.set_worker_task_id("task-alice-1")
    provider.set_environment_state_client(_NoneClient())

    asyncio.run(provider.fetch_environment_state_async())

    assert provider.rollout_rolled_back() is True
    assert (
        provider.query_environment_state(MagicMock()).freshness == Freshness.UNAVAILABLE
    )
    lines = _audit_rollback_lines(tmp_path)
    assert len(lines) == 1
    assert "environment_state_not_fetched" in lines[0]


def test_worker_read_port_rollback_audit_has_correlation_and_binds_secret(
    monkeypatch, tmp_path
):
    """The first rollback audit carries useful non-secret correlation
    (worker_task_id / agent) and the audit redactor is bound to the
    coordinator secret so a leaked secret in the failure reason is redacted."""
    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _HttpxCapture.for_config(
            captured,
            raise_error=RuntimeError(f"unauthorized {_SECRET.decode()} leak"),
        ),
    )

    provider = _read_port_provider(tmp_path=tmp_path)
    asyncio.run(provider.fetch_environment_state_async())

    lines = _audit_rollback_lines(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    assert "http_error" in line
    assert '"actor_id": "Alice"' in line
    assert '"worker_task_id": "task-alice-1"' in line

    raw = (tmp_path / "memory_rollout_audit.ndjson").read_text(encoding="utf-8")
    assert _SECRET.decode() not in raw
    assert "[REDACTED:" in raw

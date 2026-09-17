"""FC 回归测试：LLM 客户端显式 timeout / max_retries（两构造点）+ 错误归因沿用既有 taxonomy。

背景（FC 卡）：`AsyncOpenAI(**client_kwargs)` 此前只传 ``{api_key, base_url}``
（+ opencode 网关时的 default_headers），吃 OpenAI SDK 缺省
（httpx read/write/pool=600s、connect=5s、max_retries=2 → 单次 generate()
最坏 3×600s 静默等待）。上游劣化时表现为悬挂（RP3 seed44：coordinator
llm_request 后 17+ 分钟无响应、无日志、无中止）与久拖后整 run
framework_error（RP4 attempt1 step113：Connection error → 重试耗尽）。

覆盖两份独立拷贝（router_agent / worker_agent，AGENTS.md 契约：同步改动）：
- AsyncOpenAI 构造 kwargs 显式携带 timeout / max_retries（缺省严于 SDK 缺省）
- OPENAI_TIMEOUT_S / OPENAI_MAX_RETRIES 环境变量覆盖；非法值（含非有限 nan/inf）回落缺省并告警
- opencode 网关 default_headers 注入行为保持不变
- SDK 超时/连接异常经框架重试后仍归入既有 error taxonomy 词汇（无野分类）

数值口径（缺省 240s / max_retries 2）：
- 实测 14232 次真实 LLM 调用（本地全部 run traces：SAR baseline + ai2thor
  RP4）：p50=4.5s、p90=14.2s、p99=53.4s；>120s 仅 0.11%、>180s 0.06%，
  且长尾均为多次重试聚合而非单请求。240s ≈ 4.5×p99，给非流式 thinking
  长输出留足余量（约 10k thinking tokens @ ~50 tok/s）。
- SDK 缺省 read=600s → 240s 使最坏等待缩短 2.5×，且 connect 保持 5s
  快速失败（连接建立慢 = 上游不可达）；max_retries 保持 SDK 缺省 2
  （单请求总尝试 = 1 + max_retries = 3），不改动重试次数、不重构框架层
  async_retry（RetryConfig.max_retries=3 保持不动）。
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

PKGS = ["Agent.router_agent", "Agent.worker_agent"]

# 缺省值锚点（两份拷贝必须一致；改动需同步本文件与 FC 数值论证）
EXPECTED_TIMEOUT_S = 240.0
EXPECTED_CONNECT_TIMEOUT_S = 5.0
EXPECTED_MAX_RETRIES = 2

REQUEST = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


_RECORDED: list[dict] = []


def _load(pkg: str):
    """Import the openai_client module of one agent package copy."""
    return importlib.import_module(f"{pkg}.llm.openai_client")


class _FakeAsyncOpenAI:
    """Stand-in for ``openai.AsyncOpenAI`` recording constructor kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = None
        _RECORDED.append(kwargs)


def _construct(pkg, monkeypatch, api_base="https://api.deepseek.com/v1", **kwargs):
    module = _load(pkg)
    _RECORDED.clear()
    monkeypatch.setattr(module, "AsyncOpenAI", _FakeAsyncOpenAI)
    module.OpenAIClient(
        api_key="test-key", api_base=api_base, model="test-model", **kwargs
    )
    return module, _RECORDED[0]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate each test from ambient OpenAI/opencode env configuration."""
    for var in (
        "OPENAI_TIMEOUT_S",
        "OPENAI_MAX_RETRIES",
        "OPENCODE_SESSION_ID",
        "OPENAI_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(var, raising=False)


# ═══════════════════════════════════════════════════════════════════════
# 构造点参数透传（fake AsyncOpenAI 断言 kwargs）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("pkg", PKGS)
def test_async_openai_receives_explicit_timeout_and_max_retries(pkg, monkeypatch):
    """两构造点必须显式传 timeout/max_retries（不再吃 SDK 缺省）。"""
    module, kwargs = _construct(pkg, monkeypatch)

    assert set(kwargs) >= {"api_key", "base_url", "timeout", "max_retries"}
    assert kwargs["api_key"] == "test-key"
    assert kwargs["base_url"] == "https://api.deepseek.com/v1"

    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout), (
        f"expected httpx.Timeout, got {timeout!r}"
    )
    assert timeout.read == EXPECTED_TIMEOUT_S
    assert timeout.write == EXPECTED_TIMEOUT_S
    assert timeout.pool == EXPECTED_TIMEOUT_S
    assert timeout.connect == EXPECTED_CONNECT_TIMEOUT_S
    assert kwargs["max_retries"] == EXPECTED_MAX_RETRIES
    # 模块常量与断言锚点一致（防止只改常量、漏改测试或反之）
    assert module.DEFAULT_TIMEOUT_S == EXPECTED_TIMEOUT_S
    assert module.DEFAULT_CONNECT_TIMEOUT_S == EXPECTED_CONNECT_TIMEOUT_S
    assert module.DEFAULT_MAX_RETRIES == EXPECTED_MAX_RETRIES


@pytest.mark.parametrize("pkg", PKGS)
def test_defaults_are_strictly_faster_than_sdk_defaults(pkg):
    """缺省必须比 SDK 缺省更快失败（FC 约束：缺省取保守值）。"""
    module = _load(pkg)
    assert openai.DEFAULT_TIMEOUT.read == 600.0  # SDK 缺省锚点（本测试的立论前提）
    assert module.DEFAULT_TIMEOUT_S < openai.DEFAULT_TIMEOUT.read
    assert module.DEFAULT_CONNECT_TIMEOUT_S <= openai.DEFAULT_TIMEOUT.connect
    assert module.DEFAULT_MAX_RETRIES <= openai.DEFAULT_MAX_RETRIES


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_real_sdk_instance_carries_the_bounds(pkg):
    """真实 AsyncOpenAI 实例（不发请求）确认参数被 SDK 采纳、未被改写。"""
    module = _load(pkg)
    client = module.OpenAIClient(
        api_key="test-key", api_base="https://api.deepseek.com/v1", model="test-model"
    )
    sdk = client.client
    try:
        assert sdk.timeout.read == EXPECTED_TIMEOUT_S
        assert sdk.timeout.write == EXPECTED_TIMEOUT_S
        assert sdk.timeout.pool == EXPECTED_TIMEOUT_S
        assert sdk.timeout.connect == EXPECTED_CONNECT_TIMEOUT_S
        assert sdk.max_retries == EXPECTED_MAX_RETRIES
    finally:
        await sdk.close()


# ═══════════════════════════════════════════════════════════════════════
# 环境变量覆盖
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("pkg", PKGS)
def test_env_overrides_apply(pkg, monkeypatch):
    """OPENAI_TIMEOUT_S / OPENAI_MAX_RETRIES 覆盖生效。"""
    monkeypatch.setenv("OPENAI_TIMEOUT_S", "45.5")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "4")

    _, kwargs = _construct(pkg, monkeypatch)

    assert kwargs["timeout"].read == 45.5
    assert kwargs["timeout"].connect == EXPECTED_CONNECT_TIMEOUT_S  # connect 不受影响
    assert kwargs["max_retries"] == 4


@pytest.mark.parametrize("pkg", PKGS)
def test_env_timeout_below_connect_bound_clamps_connect(pkg, monkeypatch):
    """OPENAI_TIMEOUT_S 小于 connect 上限时，connect 跟随收窄（不出现 connect > read）。"""
    monkeypatch.setenv("OPENAI_TIMEOUT_S", "3")

    _, kwargs = _construct(pkg, monkeypatch)

    assert kwargs["timeout"].read == 3
    assert kwargs["timeout"].connect == 3


@pytest.mark.parametrize(
    ("bad_timeout", "bad_retries"),
    [("not-a-number", "2.5"), ("0", "-1"), ("-5", ""), ("nan", "2"), ("inf", "2")],
)
@pytest.mark.parametrize("pkg", PKGS)
def test_invalid_env_values_fall_back_to_defaults_with_warning(
    pkg, monkeypatch, caplog, bad_timeout, bad_retries
):
    """非法环境变量（非数值 / ≤0 / 负数 / 非有限值 nan/inf）回落缺省并写告警，不抛异常。"""
    monkeypatch.setenv("OPENAI_TIMEOUT_S", bad_timeout)
    monkeypatch.setenv("OPENAI_MAX_RETRIES", bad_retries)

    with caplog.at_level(logging.WARNING):
        _, kwargs = _construct(pkg, monkeypatch)

    assert kwargs["timeout"].read == EXPECTED_TIMEOUT_S
    assert kwargs["max_retries"] == EXPECTED_MAX_RETRIES
    assert "OPENAI_TIMEOUT_S" in caplog.text or "OPENAI_MAX_RETRIES" in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# 既有 opencode 网关行为不回退
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("pkg", PKGS)
def test_opencode_gateway_session_header_preserved(pkg, monkeypatch):
    """opencode 网关仍注入 x-opencode-session，且显式 timeout/max_retries 同在上。"""
    monkeypatch.setenv("OPENCODE_SESSION_ID", "sid-fixed")

    _, kwargs = _construct(pkg, monkeypatch, api_base="https://opencode.example.com/v1")

    assert kwargs["default_headers"] == {"x-opencode-session": "sid-fixed"}
    assert kwargs["timeout"].read == EXPECTED_TIMEOUT_S
    assert kwargs["max_retries"] == EXPECTED_MAX_RETRIES


@pytest.mark.parametrize("pkg", PKGS)
def test_env_custom_headers_still_win(pkg, monkeypatch):
    """OPENAI_CUSTOM_HEADERS 自带 x-opencode-session 时不重复注入（env 优先）。"""
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "x-opencode-session: from-env")

    _, kwargs = _construct(pkg, monkeypatch, api_base="https://opencode.example.com/v1")

    assert "default_headers" not in kwargs


# ═══════════════════════════════════════════════════════════════════════
# 失败路径：SDK 超时 → 框架重试耗尽（不静默、不悬挂）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("pkg", PKGS)
@pytest.mark.asyncio
async def test_sdk_timeout_surfaces_as_retry_exhaustion(pkg, monkeypatch):
    """SDK 层 APITimeoutError 在框架重试耗尽后抛出（与真机日志同形），不吞错。"""
    module = _load(pkg)
    retry_mod = importlib.import_module(f"{pkg}.retry")
    schema = importlib.import_module(f"{pkg}.schema")

    calls = {"n": 0}

    class _Completions:
        async def create(self, **_params):
            calls["n"] += 1
            raise openai.APITimeoutError(request=REQUEST)

    class _RaisingAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(module, "AsyncOpenAI", _RaisingAsyncOpenAI)
    client = module.OpenAIClient(
        api_key="test-key",
        api_base="https://api.deepseek.com/v1",
        model="test-model",
        retry_config=retry_mod.RetryConfig(max_retries=2, initial_delay=0),
    )

    with pytest.raises(retry_mod.RetryExhaustedError) as excinfo:
        await client.generate([schema.Message(role="user", content="hi")])

    assert calls["n"] == 3  # 1 + max_retries（框架层）
    assert isinstance(excinfo.value.last_exception, openai.APITimeoutError)
    # 真机（baseline seed_0_fail7）同形文案：LLM call failed after 4 retries / Request timed out.
    assert "Request timed out" in str(excinfo.value)


# ═══════════════════════════════════════════════════════════════════════
# 错误归因：沿用既有 taxonomy 词汇，不新增野分类
# ═══════════════════════════════════════════════════════════════════════


def test_timeout_connection_errors_stay_inside_existing_taxonomy():
    """超时/连接异常（SDK 层与 httpx 层）归入既有词汇，无新分类引入。"""
    from Agent.error_taxonomy import (
        DOMAIN_ERROR_CODES,
        FRAMEWORK_ERROR_CODES,
        MISSING_ERROR_CODE,
        UNCLASSIFIED_TOOL_ERROR,
        exception_error_code,
    )

    existing = (
        FRAMEWORK_ERROR_CODES
        | DOMAIN_ERROR_CODES
        | {UNCLASSIFIED_TOOL_ERROR, MISSING_ERROR_CODE}
    )

    # SDK 层（openai 模块）包裹异常：现状映射 = 既有通用哨兵 unclassified_tool_error
    # （taxonomy 按 exception 类型名/module 归类；openai 模块名不在 network 名单内）。
    assert (
        exception_error_code(openai.APITimeoutError(request=REQUEST))
        == UNCLASSIFIED_TOOL_ERROR
    )
    assert (
        exception_error_code(openai.APIConnectionError(request=REQUEST))
        == UNCLASSIFIED_TOOL_ERROR
    )

    # httpx 层异常（若直接上抛）：既有 network_error（名字在 _NETWORK_EXCEPTION_NAMES）。
    assert (
        exception_error_code(httpx.ConnectError("boom", request=REQUEST))
        == "network_error"
    )
    assert (
        exception_error_code(httpx.ReadTimeout("boom", request=REQUEST))
        == "network_error"
    )

    # 框架重试耗尽包装器（两拷贝）：同样只落既有词汇。
    for pkg in PKGS:
        retry_mod = importlib.import_module(f"{pkg}.retry")
        for cause in (
            openai.APITimeoutError(request=REQUEST),
            openai.APIConnectionError(request=REQUEST),
            httpx.ConnectError("boom", request=REQUEST),
        ):
            wrapped = retry_mod.RetryExhaustedError(cause, 4)
            code = exception_error_code(wrapped)
            assert code in existing, f"{pkg}: wild classification {code!r}"

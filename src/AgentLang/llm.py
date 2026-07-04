"""LLM builder — replaces src/Agent LLMClient with LangChain chat models.

复刻 LLMClient 的 provider 路由：openai→ChatOpenAI(base_url)，anthropic→ChatAnthropic。
worker 变体的 reasoning_split=True 经 ChatOpenAI extra_body 传递，对齐
worker_agent/llm/openai_client.py 的 "extra_body": {"reasoning_split": True}。
MiniMax 域名自动追加 /anthropic 或 /v1 后缀的行为一并保留（对齐 llm_wrapper.py）。
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

# 与 LLMClient.MINIMAX_DOMAINS 一致
_MINIMAX_DOMAINS = ("api.minimax.io", "api.minimaxi.com")


def _normalize_api_base(api_base: str, provider: str) -> str:
    """复刻 LLMClient 的 MiniMax 后缀逻辑（第三方 API 原样使用）。"""
    api_base = (api_base or "").rstrip("/")
    if not any(d in api_base for d in _MINIMAX_DOMAINS):
        return api_base
    api_base = api_base.replace("/anthropic", "").replace("/v1", "")
    p = (provider or "openai").lower()
    if p == "anthropic":
        return f"{api_base}/anthropic"
    if p == "openai":
        return f"{api_base}/v1"
    raise ValueError(f"Unsupported provider: {provider}")


def build_llm(
    provider: str = "openai",
    api_base: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
    api_key: str | None = None,
    temperature: float = 0.7,
    reasoning_split: bool = False,
    api_key_env: str = "OPENAI_API_KEY",
    **kwargs,
) -> BaseChatModel:
    """构建 LangChain chat model。

    Args:
        provider: "openai" | "anthropic"
        api_base: API base URL（MiniMax 域名自动加后缀，其余原样使用）
        model: 模型名
        api_key: 显式 key；None 则从 api_key_env 环境变量取
        temperature: 采样温度
        reasoning_split: worker 变体为 True，openai provider 下传 extra_body
        api_key_env: 环境变量名
        **kwargs: 透传给底层 chat model
    """
    key = api_key or os.environ.get(api_key_env, "")
    base = _normalize_api_base(api_base, provider)
    p = (provider or "openai").lower()

    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic

        init = dict(model=model, temperature=temperature, api_key=key, **kwargs)
        if base:
            init["base_url"] = base
        return ChatAnthropic(**init)

    # openai 兼容（DeepSeek / MiniMax / SiliconFlow 等）
    from langchain_openai import ChatOpenAI

    init: dict = dict(model=model, temperature=temperature, api_key=key, **kwargs)
    if base:
        init["base_url"] = base
    if reasoning_split:
        # 与 worker_agent/openai_client.py 一致：分离 thinking
        extra = dict(init.get("extra_body") or {})
        extra["reasoning_split"] = True
        init["extra_body"] = extra
    return ChatOpenAI(**init)

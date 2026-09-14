"""Judge model client for ``sar_orch.eval.judge`` — config, calls, parsing.

Everything about talking to the judge model lives here:

- ``.env`` key resolution reusing the existing keys (``reflection_*`` first,
  generic keys as fallback — same source as the reflection model port, see
  ``docs/system_docs/memory.md`` §7).  No new provider/model configuration is
  introduced by this package.
- ``LLMJudgeClient`` — a synchronous, bounded wrapper around the repo's
  ``LLMClient`` (same call stack as ``ReflectionModelPort``: fresh client per
  call, ``asyncio.run`` + ``asyncio.wait_for``).  Client-level network retry
  is disabled so the per-sample retry budget below is exact and auditable
  (one attempt == one HTTP call).
- ``run_judge_json`` — the fail-closed retry loop: call → tolerant JSON
  extraction → caller-supplied normalisation; at most 3 total attempts
  (1 initial + <=2 retries, per the card's "retry <=2").  A sample that
  still fails is reported as ``judge_error`` by the metric modules; nothing
  is guessed or partially accepted.
- ``extract_json_object`` — tolerant extraction of one JSON object from raw
  model output (fenced blocks, prose around the object, nested braces inside
  strings).  Failures raise :class:`JudgeParseError` (retryable), mirroring
  the fail-closed spirit of the reflection validator: invalid output is
  never accepted.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

# Bootstrap `src` so the package works as a CLI (python -m sar_orch.eval.judge)
# without requiring PYTHONPATH; mirrors tests/conftest.py — the venv `.pth`
# appends `src` *after* site-packages, where the `a2a-sdk` package shadows
# the project `a2a`, so `src` must be moved to the front unconditionally.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = str(_REPO_ROOT / "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)

from a2a.shared.env_loader import load_env_file  # noqa: E402
from Agent.worker_agent.llm.llm_wrapper import LLMClient  # noqa: E402
from Agent.worker_agent.retry import RetryConfig  # noqa: E402
from Agent.worker_agent.schema import LLMProvider, Message  # noqa: E402

DEFAULT_ENV_FILE = _REPO_ROOT / ".env"
DEFAULT_JUDGE_TIMEOUT_SEC = 180.0
MAX_JUDGE_ATTEMPTS = 3  # 1 initial attempt + <= 2 retries (card decision #7)

#: Appended to the user prompt on retry attempts after a parse/validation
#: failure, nudging the model back onto the strict JSON contract.
RETRY_SUFFIX = (
    "\n\nNOTE: your previous answer could not be parsed as the required JSON "
    "object. Respond again with ONLY the JSON object — no prose before or "
    "after it, no code fences."
)

_REFLECTION_KEYS = {
    "provider": "reflection_provider",
    "model": "reflection_model",
    "api_key": "reflection_api_key",
    "api_base": "reflection_api_base",
}
_GENERIC_KEYS = {
    "provider": "provider",
    "model": "model",
    "api_key": "api_key",
    "api_base": "api_base",
}


class JudgeCallError(RuntimeError):
    """Raised when a judge model call cannot be completed (fail closed)."""


class JudgeParseError(ValueError):
    """Raised when judge output is not the required JSON object/shape."""


@dataclass(frozen=True)
class JudgeConfig:
    """Resolved judge model configuration (never carries the api_key out)."""

    provider: str
    model: str
    api_key: str
    api_base: str | None
    config_source: str  # "reflection" | "generic"

    def public_info(self) -> dict[str, Any]:
        """Audit-safe view for the output JSON — api_key is never included."""
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "config_source": self.config_source,
        }


@dataclass
class JudgeCompletion:
    """One successful judge model response."""

    content: str
    usage: dict[str, int] | None = None


class JudgeClient(Protocol):
    """Minimal client seam the metric modules depend on (mockable in tests)."""

    def complete(self, *, system_prompt: str, user_prompt: str) -> JudgeCompletion:
        """Run one judge call; raise :class:`JudgeCallError` on failure."""


def load_judge_env(env_file: str | Path | None = None) -> dict[str, str]:
    """Resolve judge-relevant keys from the env file plus the process env.

    The ``.env`` file (repo root by default) is the run's canonical config in
    this project, so its values win; process environment variables fill any
    key the file does not define.
    """
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    env: dict[str, str] = {}
    file_env = load_env_file(path)
    for field_name, key in _REFLECTION_KEYS.items():
        value = file_env.get(key) or os.environ.get(key)
        if value:
            env[key] = value
    for field_name, key in _GENERIC_KEYS.items():
        value = file_env.get(key) or os.environ.get(key)
        if value:
            env[key] = value
    return env


def resolve_judge_config(env: Mapping[str, str]) -> JudgeConfig | None:
    """Build the judge config from ``reflection_*`` keys, then generic keys.

    Returns ``None`` when provider, model or api key is missing — the caller
    degrades to ``judge_unconfigured`` instead of invoking anything.
    """
    if not env:
        return None
    values: dict[str, str | None] = {}
    for field_name in ("provider", "model", "api_key", "api_base"):
        values[field_name] = env.get(_REFLECTION_KEYS[field_name]) or env.get(
            _GENERIC_KEYS[field_name]
        )
    if not values["provider"] or not values["model"] or not values["api_key"]:
        return None
    source = (
        "reflection"
        if env.get(_REFLECTION_KEYS["model"]) == values["model"]
        else "generic"
    )
    return JudgeConfig(
        provider=str(values["provider"]),
        model=str(values["model"]),
        api_key=str(values["api_key"]),
        api_base=str(values["api_base"]) if values["api_base"] else None,
        config_source=source,
    )


def build_judge_client(
    env_file: str | Path | None = None,
) -> tuple["LLMJudgeClient | None", JudgeConfig | None]:
    """Resolve config and build the real judge client (None when unconfigured)."""
    config = resolve_judge_config(load_judge_env(env_file))
    if config is None:
        return None, None
    return LLMJudgeClient(config), config


async def _close_http_client(client: Any) -> None:
    """Close the wrapper's underlying async HTTP client inside the live loop.

    ``LLMClient`` holds an event-loop-bound ``AsyncOpenAI`` / ``AsyncAnthropic``
    pool and exposes no ``close()``; without this, the pool is finalized after
    ``asyncio.run`` closes the loop, producing "Event loop is closed" noise
    (and leaking sockets across the judge's sequential calls).  Best-effort:
    a close failure must never fail the judge call itself.
    """
    http_client = getattr(getattr(client, "_client", None), "client", None)
    if http_client is None:
        return
    try:
        await http_client.close()
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


class LLMJudgeClient:
    """Synchronous bounded judge client on top of the repo's ``LLMClient``.

    One fresh ``LLMClient`` per call (same lifecycle rationale as the
    reflection port: the async client's connection pool must not be reused
    across fresh event loops).  Network-level retry is disabled so the
    ``run_judge_json`` retry budget is the only retry layer.
    """

    def __init__(
        self,
        config: JudgeConfig,
        *,
        timeout_sec: float = DEFAULT_JUDGE_TIMEOUT_SEC,
    ) -> None:
        self.config = config
        self.timeout_sec = timeout_sec

    def complete(self, *, system_prompt: str, user_prompt: str) -> JudgeCompletion:
        if not self.config.api_base:
            raise JudgeCallError("judge api_base not configured")
        try:
            client = LLMClient(
                api_key=self.config.api_key,
                provider=LLMProvider(self.config.provider),
                api_base=self.config.api_base,
                model=self.config.model,
                retry_config=RetryConfig(enabled=False),
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, typed error
            raise JudgeCallError(
                f"judge client init failed: {type(exc).__name__}: {exc}"
            ) from exc

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

        async def _call() -> Any:
            try:
                return await client.generate(messages, None)
            finally:
                await _close_http_client(client)

        try:
            response = asyncio.run(
                asyncio.wait_for(_call(), timeout=self.timeout_sec)
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, typed error
            raise JudgeCallError(
                f"judge call failed: {type(exc).__name__}: {exc}"
            ) from exc

        usage = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage = {
                "prompt_tokens": int(getattr(raw_usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(
                    getattr(raw_usage, "completion_tokens", 0) or 0
                ),
                "total_tokens": int(getattr(raw_usage, "total_tokens", 0) or 0),
                "cache_hit_tokens": int(
                    getattr(raw_usage, "cache_hit_tokens", 0) or 0
                ),
            }
        content = getattr(response, "content", None) or ""
        return JudgeCompletion(content=str(content), usage=usage)


# --------------------------------------------------------------------------
# tolerant JSON extraction
# --------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str | None:
    """Return the content inside the outermost ``` fence, if any."""
    start = text.find("```")
    if start < 0:
        return None
    end = text.rfind("```")
    if end <= start:
        return None
    inner = text[start + 3 : end]
    newline = inner.find("\n")
    if newline >= 0:
        first_line = inner[:newline].strip().lower()
        if first_line in ("", "json") or first_line.startswith("json"):
            inner = inner[newline + 1 :]
    return inner.strip() or None


def _first_balanced_object(text: str) -> str | None:
    """Slice the first balanced ``{...}`` block, string-literal aware."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start : index + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract exactly one JSON object from raw judge output.

    Tolerates markdown fences and surrounding prose; raises
    :class:`JudgeParseError` when no single valid object can be recovered
    (truncated output, arrays, scalars, multiple-object garbage).
    """
    if not isinstance(text, str) or not text.strip():
        raise JudgeParseError("empty judge output")
    stripped = text.strip()
    fenced = _strip_code_fence(stripped)
    candidates: list[str] = [stripped]
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    for candidate in list(candidates):
        balanced = _first_balanced_object(candidate)
        if balanced and balanced not in candidates:
            candidates.append(balanced)
    last_error: str | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = "judge output is not a JSON object"
    raise JudgeParseError(f"no JSON object found in judge output: {last_error}")


# --------------------------------------------------------------------------
# fail-closed call + retry loop
# --------------------------------------------------------------------------


@dataclass
class JudgeCallOutcome:
    """Outcome of one judge task (single sample) across its retry budget."""

    value: Any | None  # normalised verdict; None when every attempt failed
    attempts: int
    error: str | None
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
        }
    )

    @property
    def ok(self) -> bool:
        return self.value is not None


def run_judge_json(
    client: JudgeClient,
    *,
    system_prompt: str,
    user_prompt: str,
    parse: Callable[[dict[str, Any]], Any],
    max_attempts: int = MAX_JUDGE_ATTEMPTS,
) -> JudgeCallOutcome:
    """Call the judge, extract + normalise JSON, retry at most ``max_attempts``.

    ``parse`` receives the extracted JSON object and returns the normalised
    verdict, raising :class:`JudgeParseError` when the payload violates the
    metric's shape contract (treated the same as a parse failure: retry, then
    judge_error).  Usage from every attempt that returned a response — including
    attempts whose payload failed parsing — is accumulated: the judge cost is
    real even when its answer is rejected.
    """
    attempts = max(1, int(max_attempts))
    usage = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
    }
    error: str | None = None
    for attempt in range(1, attempts + 1):
        prompt = user_prompt if attempt == 1 else user_prompt + RETRY_SUFFIX
        try:
            completion = client.complete(
                system_prompt=system_prompt, user_prompt=prompt
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, never crash
            usage["calls"] += 1
            error = f"{type(exc).__name__}: {exc}"
            continue
        usage["calls"] += 1
        if completion.usage:
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cache_hit_tokens",
            ):
                usage[key] += int(completion.usage.get(key, 0) or 0)
        try:
            parsed = extract_json_object(completion.content)
            value = parse(parsed)
        except JudgeParseError as exc:
            error = f"JudgeParseError: {exc}"
            continue
        return JudgeCallOutcome(
            value=value, attempts=attempt, error=None, usage=usage
        )
    return JudgeCallOutcome(
        value=None, attempts=attempts, error=error, usage=usage
    )

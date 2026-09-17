"""Ping the deployment's frozen upstream binding with one 1-token chat call.

The gate's binding is frozen per campaign in ``stack.yaml``: every episode's
model calls and the proposer's own call go through it. A wrong URL, model, or
credential never fails the service boot - episodes just render the ``no-key``
placeholder and 401 one by one - so this probe is the cheap check before a
campaign's first step.

It reads the URL and model out of ``stack.yaml`` and the credential out of the
environment variable the launcher exports, then calls reef's own
:class:`ModelBinding.chat` - the same code path a proposal uses, and the same
endpoint the recipe renders into each episode. Prints the request path, the
served model, the key's fingerprint (never the key), and the usage.

Usage (through the launcher, which exports the key from the main repo .env)::

    reef_sar_adapter/reef-service.sh ping

or directly::

    REEF_SAR_UPSTREAM_API_KEY=... python -m reef_sar_adapter.ping_upstream
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import yaml

__all__ = ["STACK_FILE", "binding_from_stack", "main"]

#: The deployment stack this probe reads the binding from, beside the package.
STACK_FILE = Path(__file__).resolve().with_name("stack.yaml")

#: The environment variable the launcher exports the credential under.
KEY_ENV = "REEF_SAR_UPSTREAM_API_KEY"

PING_TIMEOUT_S = 90.0


def binding_from_stack(path: Path = STACK_FILE):
    """The frozen binding as reef would build it, plus the request path it hits."""
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = config.get("reef") or {}
    url = str(section.get("upstream_url") or "").strip()
    model = str(section.get("upstream_model") or "").strip()
    if not url or not model:
        raise SystemExit(f"ping_upstream: {path} carries no upstream_url/upstream_model")
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        raise SystemExit(
            f"ping_upstream: {KEY_ENV} is unset or empty; start through reef-service.sh "
            "(it exports the main repo .env key) instead of invoking this module bare"
        )
    from reef.harness.episodes.model_binding import ModelBinding

    return ModelBinding(base_url=url, model=model, api_key=key), key


def main() -> int:
    binding, key = binding_from_stack()
    print(f"ping_upstream: POST {binding.base_url}/v1/chat/completions model={binding.model}")
    print(f"ping_upstream: key sha256[:12]={hashlib.sha256(key.encode()).hexdigest()[:12]}")
    try:
        reply = binding.chat([{"role": "user", "content": "ping"}], max_tokens=1, timeout_s=PING_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the probe's whole job is to report the failure
        status = getattr(exc, "status", None)
        print(f"ping_upstream: FAIL{'' if status is None else f' (HTTP {status})'}: {str(exc)[:300]}")
        return 1
    print(f"ping_upstream: OK, reply={reply[:80]!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module invocation
    sys.exit(main())

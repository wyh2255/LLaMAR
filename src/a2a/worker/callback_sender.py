"""Worker-side signed push notification sender (Phase 2).

The push notifications sent to ``/a2a/push-callback`` must carry a
``CallbackProofV1`` bound to the exact serialized HTTP body.  Each attempt
(including retries) re-signs with a fresh nonce while the callback body stays
identical, so the Coordinator idempotency ledger returns the original receipt.
The shared secret is only held in a protected local config object — it never
appears in prompts, A2A messages, context, or logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.server.tasks import BasePushNotificationSender
from a2a.utils.proto_utils import to_stream_response

from a2a.coordinator.memory.callback_auth import (
    MIN_CALLBACK_SECRET_BYTES,
    CallbackProofV1,
    MemoryAuthNotConfiguredError,
)

logger = logging.getLogger(__name__)

__all__ = ["CallbackSigner", "SignedPushNotificationSender"]

HEADER_PROOF = "X-A2A-Callback-Proof"
HEADER_WORKER_ID = "X-A2A-Worker-Id"


class CallbackSigner:
    """Signs serialized callback bodies with a fresh nonce per call.

    Constructing a signer validates the coordinator secret; a missing / empty /
    short secret raises the typed ``memory_auth_not_configured`` error so the
    worker fails closed instead of sending unsigned callbacks.
    """

    def __init__(self, worker_id: str, coordinator_secret: bytes | None) -> None:
        if not coordinator_secret or not isinstance(coordinator_secret, bytes):
            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: worker cannot sign callbacks "
                "(no coordinator secret in protected config)"
            )
        if len(coordinator_secret) < MIN_CALLBACK_SECRET_BYTES:
            raise MemoryAuthNotConfiguredError(
                "memory_auth_not_configured: coordinator secret must be at "
                f"least {MIN_CALLBACK_SECRET_BYTES} bytes"
            )
        self._worker_id = worker_id
        self._secret = coordinator_secret

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def sign(self, body: bytes) -> str:
        """Return a proof binding ``body``; a fresh nonce is generated per call."""
        body_sha256 = hashlib.sha256(body).hexdigest()
        return CallbackProofV1.generate(self._secret, self._worker_id, body_sha256)


class SignedPushNotificationSender(BasePushNotificationSender):
    """PushNotificationSender that signs the exact HTTP body it transmits.

    Falls back to unsigned behaviour only when ``signer`` is None (legacy mode);
    the AgentCard ``capabilities.push_notifications`` is derived from signer
    availability so a Coordinator in secure mode never creates an unsigned push.
    """

    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        config_store: Any,
        signer: CallbackSigner | None = None,
    ) -> None:
        super().__init__(httpx_client=httpx_client, config_store=config_store)
        self._signer = signer

    def signer_available(self) -> bool:
        return self._signer is not None

    @property
    def signer(self) -> CallbackSigner | None:
        return self._signer

    def _serialize_body(self, event: Any) -> tuple[bytes, str]:
        body_dict = MessageToDict(to_stream_response(event))
        body_json = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        return body_json, hashlib.sha256(body_json).hexdigest()

    async def _dispatch_notification(
        self,
        event: Any,
        push_info: Any,
        task_id: str,
    ) -> bool:
        url = push_info.url
        body_bytes, body_sha256 = self._serialize_body(event)
        headers: dict[str, str] = {"content-type": "application/json"}
        if self._signer is not None:
            # A fresh nonce per attempt; the body (and its idempotency key) stays
            # the same so a retry returns the original Coordinator receipt.
            proof = self._signer.sign(body_bytes)
            headers[HEADER_PROOF] = proof
            headers[HEADER_WORKER_ID] = self._signer.worker_id
        if push_info.token:
            headers["X-A2A-Notification-Token"] = push_info.token
        try:
            response = await self._client.post(url, content=body_bytes, headers=headers)
            response.raise_for_status()
            logger.info(
                "Signed push-notification sent for task_id=%s to URL=%s body_sha256=%s",
                task_id,
                url,
                body_sha256[:16],
            )
        except Exception:
            logger.exception(
                "Error sending push-notification for task_id=%s to URL=%s.",
                task_id,
                url,
            )
            return False
        return True

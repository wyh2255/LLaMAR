---
日期: 2026-07-15
文档类型: 技术设计文档
文档概述: Phase 5 worker-to-worker peer mail architecture, protocol, auth matrix, mailbox lifecycle, observability, and known limitations.
---

# Peer Mail -- Phase 5 Design

## Architecture

Peer mail flows directly between workers using the A2A SDK -- the
coordinator is **not** a relay.  The coordinator only installs/revokes
the team roster (which includes endpoints and the shared secret).

```
 Worker (Alice)          A2A SDK send_message     Worker (Bob)
 +---------------+       signed MAIL envelope      +---------------+
 | TeamState     | ------------------------------> | Ingress       |
 | SenderService |                                 | TeamState     |
 | MailboxStore  |                                 | MailboxStore  |
 +---------------+                                 +---------------+
        ^                                                 ^
        | Team roster (configured by coordinator)         | Authorization (HMAC + epoch)
        v                                                 v
 CoordinatorTeamRegistry                          EnvelopeIngress
```

## Protocol Kinds

| Kind | Sender | Recipient | HMAC Key | Use Case |
|------|--------|-----------|----------|----------|
| MAIL | Worker | Worker | Peer team secret | Direct peer-to-peer coordination |
| MAIL | Coordinator | Worker | Coordinator secret | Coordinator directives |
| TASK | Coordinator | Worker | Coordinator secret | Task assignment (existing) |
| TEAM_UPDATE | Coordinator | Worker | Coordinator secret | Team roster install |
| TEAM_REVOKE | Coordinator | Worker | Coordinator secret | Team disband |

## Auth Matrix

| Attempt | Verdict | Where |
|---------|---------|-------|
| Worker sends MAIL to same-team peer | Accepted | Ingress.classify -> WORKER role -> authz rules |
| Worker sends MAIL to self | Rejected (_SELF_SEND) | Synchronous, sender.validate_recipient |
| Worker sends MAIL to non-member | Rejected (_NOT_IN_TEAM) | Synchronous, sender.validate_recipient |
| Worker sends MAIL with wrong epoch | Rejected (_TEAM_MISMATCH) | Ingress.authorize_envelope |
| Worker sends MAIL with wrong secret | Rejected (SignatureInvalid) | Ingress._try_secret -> HMAC fail |
| Worker sends TASK to peer | Rejected (kind not in WORKER authz) | Ingress.authorize_envelope |
| Coordinator sends any kind | Accepted | COORDINATOR role -> all kinds |
| Unsigned (legacy) text with allow_legacy_tasks=False | Rejected | Ingress.classify -> reject |

## Mailbox Lifecycle

```
 Alice                    Bob's Ingress            Bob's MailboxStore
   |  signed MAIL envelope      |                        |
   | -------------------------> |                        |
   |                            | classify() -> "mail"   |
   |                            | --------------------> |
   |                            |   EnvelopeAwareAdapter |
   |                            |   _handle_mail()       |
   |                            | --------------------> |
   |                            |   deliver()            |
   |                            |   [mail_delivered      |
   |                            |    event fires]        |
   |                            |                        |
   |                            |   Worker reads via     |
   |                            |   read_mailbox tool    |
   |                            |   [mail_read event     |
   |                            |    fires]              |
```

## Context Reminder

`SARWorkerStateProvider.snapshot()` includes `mailbox_summary` (unread
count, unique senders, oldest unread time).  The version tuple
`(env_step, mailbox_version, team_generation)` changes when new mail
arrives, which triggers a context refresh -- the worker's next LLM round
sees the pending mail and can decide to read it.

## Team Lifecycle

```
 Coordinator                 Worker A                Worker B
     |                           |                       |
     | ConfigureTeamTool         |                       |
     | (normalises endpoints)    |                       |
     |---- TEAM_UPDATE --------->|                       |
     |---- TEAM_UPDATE -------------------------------->|
     |                           | install()             | install()
     |                           | [team_installed       | [team_installed
     |                           |  event fires]         | event fires]
     |                           |                       |
     | Peer mail active          |                       |
     |<----- MAIL -------------------------------------->|
     |                           |                       |
     | DisbandTeamTool           |                       |
     |---- TEAM_REVOKE --------->|                       |
     |---- TEAM_REVOKE --------------------------------->|
     |                           | revoke()              | revoke()
     |                           | [team_revoked         | [team_revoked
     |                           |  event fires]         | event fires]
```

## Endpoint Normalization

Workers announce their AgentCard URLs using `host="0.0.0.0"`, which
produces endpoints like `http://0.0.0.0:8191/`.  These are not routable.
The coordinator normalizes them to `http://localhost:<port>/` when
building the team roster.

```python
from a2a.shared.endpoint_helpers import normalize_endpoint

normalize_endpoint("http://0.0.0.0:8191/")  # -> "http://localhost:8191/"
```

Uses `urllib.parse` for structured URL handling.  Supports `http` and
`https` schemes.  Preserves path/query.  Rejects embedded credentials.
Applied in `CoordinatorTeamRegistry.configure_for_delivery()` before any
validation or state mutation.

## Enable CLI

Only `experiment.py` supports `--enable-peer-mail`.  The benchmark
runner does not support it yet.

```bash
env no_proxy="localhost,0.0.0.0,127.0.0.1" PYTHONPATH="src:$PYTHONPATH" \
  uv run python sar_orch/experiment.py --scene 1 --agents 2 --seed 42 \
  --enable-peer-mail
```

## Files and Classes

### New files

| File | Class/Function | Purpose |
|------|---------------|---------|
| `src/a2a/shared/endpoint_helpers.py` | `normalize_endpoint`, `normalize_roster_endpoints` | Wildcard host normalization (urllib.parse) |
| `src/a2a/shared/response_parser.py` | `parse_stream_for_terminal` | Shared A2A stream response terminal parser |
| `src/a2a/worker/peer_sender.py` | `WorkerPeerSenderService` | Send signed MAIL envelopes via A2A SDK |
| `src/a2a/worker/tools/send_peer_mail.py` | `A2ASendMailTool` | Worker LLM tool for sending peer mail |
| `tests/test_phase5_peer_mail.py` | (multiple test classes) | Comprehensive test suite |

### Modified files

| File | Change |
|------|--------|
| `src/a2a/worker/team_state.py` | Added `DeliverySnapshot` (frozen, immutables), `delivery_snapshot()`, event callbacks outside lock |
| `src/a2a/worker/mailbox_store.py` | Event callbacks for `mail_delivered`/`mail_read`, fired after lock release |
| `src/a2a/coordinator/sender_service.py` | Uses shared `parse_stream_for_terminal`; removed artifact/message implicit completion |
| `src/a2a/coordinator/team_registry.py` | Added endpoint normalization in `configure_for_delivery` |
| `sar_orch/worker.py` | Added `_peer_sender`; wiring; sender close in `run()` finally; stop() only signals+joins |

## Logs

### Standard logging (Python `logging.Logger`)

| Event | Log Level | Message |
|-------|-----------|---------|
| Mail sent | INFO | `Mail delivered id=<id> from=<sender>` (receiver side) |
| Mail send failure | WARNING | `send_control to <endpoint> failed: <reason>` (sender side) |
| Team installed | INFO | `Team '<id>' installed at epoch <N> (<M> members)` |
| Team revoked | INFO | `Team '<id>' revoked at epoch <N>` |
| Peer send error | WARNING | `a2a_send_mail to '<id>' failed: <reason>` |

### Event callbacks (no body, no subject, no secret, no signature)

| Event Name | Fires From | Data Fields |
|------------|-----------|----------------------------------------|
| `mail_sent` | `WorkerPeerSenderService` (outside lock) | message_id, sender_id, recipient_id, team_id, team_epoch, outcome |
| `mail_delivery_failed` | `WorkerPeerSenderService` | message_id, recipient_id, error, team_id |
| `mail_rejected` | `WorkerPeerSenderService` (sync validation) | recipient_id, reason, team_id |
| `mail_delivered` | `WorkerMailboxStore` (after lock) | message_id, sender_id, recipient_id, team_id, team_epoch, received_at |
| `mail_read` | `WorkerMailboxStore` (after lock) | message_id, sender_id, recipient_id, team_id, team_epoch |
| `team_installed` | `WorkerTeamState` (after lock) | team_id, epoch, member_count, coordinator_id |
| `team_revoked` | `WorkerTeamState` (after lock) | team_id, epoch |

### NDJSON event log

Mailbox events persisted at `<log_dir>/<agent_name>/mailbox.ndjson`:
`deliver` (full record), `read` (message_id + timestamp),
`tombstone` (trimmed records).

## Tests

```bash
# Phase 5 specific
cd /home/wyh/daily_work/LLaMAR-sematic_map
PYTHONPATH="src:$PYTHONPATH" uv run pytest tests/test_phase5_peer_mail.py -v

# All related suites
PYTHONPATH="src:$PYTHONPATH" uv run pytest \
  tests/test_phase5_peer_mail.py \
  tests/test_worker_team_state.py \
  tests/test_worker_mailbox.py \
  tests/test_worker_ingress.py \
  tests/test_envelope_aware_adapter.py \
  tests/test_sender_service.py \
  tests/test_coordinator_send_mail.py \
  tests/test_team_registry.py \
  tests/test_worker_state_provider.py \
  tests/test_message_envelope.py \
  tests/test_phase3_read_mailbox.py \
  tests/test_signed_task_dispatch.py \
  -v
```

## Limitations

### 1. CancelTask is unauthenticated (Security Residual)

The A2A SDK's `CancelTaskRequest` protobuf has fields `{tenant, id,
metadata}` -- there is **no** `sender_id`, no auth token, no signature
field.  Any network client that can reach the worker's A2A endpoint can
invoke `CancelTask` for any task -- there is no mechanism to
authenticate who is requesting cancellation.

**Current mitigations:**
- Workers are **not** given a `cancel_task` tool or
  `CancelTaskTool` in their tool list.
- The `CancelTask` builtin tool is only registered in the
  coordinator's router agent.
- `EnvelopeAwareAdapter.cancel()` (line 432 of
  `agent_adapter.py`) documents this integration seam -- it
  delegates to `AgentAdapter.cancel()` without authentication.

**Risk:** A malicious peer or external client with network access to the
worker's A2A endpoint can cancel tasks.  In local experiments (all
workers on loopback) this is low-risk.

**No integration fix in Phase 5.**  Proper mitigation would require
either (a) an authenticated cancel envelope (signed `CANCEL_TASK` as a
new `MessageEnvelope` kind) or (b) an authorization token in the A2A
request handler.  This is left as a future integration point.

### 2. Shared team secret blast radius

The team shared secret is stored in plaintext at
`<log_dir>/<agent_name>/team_state.json` with `0600` permissions.  Any
process with filesystem read access to a worker's log directory can
extract the secret and forge peer mail.  In production, use a dedicated
secret store (e.g. keyring or TPM-backed vault).

### 3. Team secret in transit (HTTP cleartext)

The team secret is transmitted in hex-encoded plaintext inside the
signed `TEAM_UPDATE` envelope body.  The envelope is HMAC-protected
against tampering, but the secret is readable by any process that can
capture the HTTP request at the transport layer.  On loopback interfaces
(all workers on `localhost`) this is low risk, but a production
deployment **must** use HTTPS for A2A endpoints or an alternative
secure channel for secret delivery.

### 4. No retry mechanism

`WorkerPeerSenderService.send_mail()` makes a single attempt per call.
Retry logic is left for future work (agent-driven via the LLM loop, or
automatic exponential backoff).

### 4. No cross-subject TASK delegation

Workers cannot delegate tasks to peers via the mail system.  The
`a2a_send_mail` tool only creates `MAIL` envelopes, and
`EnvelopeIngress` rejects `TASK` envelopes from worker signers.  This
is by design -- only the coordinator can assign tasks.

### 5. `--enable-peer-mail` only in experiment.py

The benchmark runner (`sar_orch/benchmark.py`) does not support
`--enable-peer-mail`.  Only `experiment.py` supports it.

---
日期: 2026-07-15
文档类型: 技术设计文档
文档概述: Phase 5 worker-to-worker peer mail architecture, protocol, auth matrix, mailbox lifecycle, observability, and known limitations.
校准基线: main@a459481（代码冻结 cb54b06 @2026-08-17）
核对口径: 类/函数名 grep -n，行号以当前工作区实测为准
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
| Worker sends MAIL to self | Rejected (_SELF_SEND) | Recipient-side authorize_envelope (message_envelope.py:458); sender-side `_validate_recipient` (peer_sender.py:264) also blocks with NotInTeamError before network send |
| Worker sends MAIL to non-member | Rejected (_NOT_IN_TEAM) | Recipient-side authorize_envelope (message_envelope.py:467); sender-side `_validate_recipient` (peer_sender.py:266) blocks first with NotInTeamError |
| Worker sends MAIL with wrong epoch | Rejected (_TEAM_MISMATCH) | authorize_envelope (message_envelope.py:465) |
| Worker sends MAIL with wrong secret | Rejected (SignatureInvalid) | Ingress._try_secret -> HMAC fail (ingress.py:163) |
| Worker sends TASK to peer | Rejected (kind not in WORKER authz) | authorize_envelope kind check (message_envelope.py:446) |
| Coordinator sends any kind | Accepted | COORDINATOR role -> all kinds |
| Unsigned (legacy) text with allow_legacy_tasks=False | Rejected | Ingress.classify -> reject (ingress.py:122-127) |

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

## INPUT_REQUIRED Help Chain

除 peer mail 外，worker↔coordinator 还有一条**求助/回复**链路（A2A
`INPUT_REQUIRED` 状态机）。该路径不经过 mailbox，也不在
`images/peer-mail-lifecycle.svg` 中，九步如下（行号 grep -n 实测）：

1. **Worker LLM 调用 `ask_coordinator`**：`AskCoordinatorTool` 由
   `AgentAdapter.execute()` 注入（agent_adapter.py:243-246），
   `execute()` 直接 `raise NeedInputError(question)`
   （src/a2a/worker/tools/ask_coordinator.py:50）。
2. **Agent.run 捕获**：`src/Agent/worker_agent/agent.py:752` 捕获
   `NeedInputError`，为同轮未执行的后续 tool_calls 回填占位 tool 消息
   （:774-785），返回 `RunResult(content=question, need_input=True)`
   （:787-790）。
3. **快照保存**：`Controller.submit()` 检测 `result.need_input and
   task_id` → `ctx.save_snapshot(task_id, agent.messages)`
   （src/Agent/controller/controller.py:274-275）。
4. **置 INPUT_REQUIRED**：`AgentAdapter.execute()` 见 `result.need_input`
   → `updater.requires_input(message=new_text_message(result.content))`
   后 return，让出控制权（agent_adapter.py:258-270）。
5. **Coordinator 收 callback**：push callback 识别
   `TASK_STATE_INPUT_REQUIRED`（server.py:1983/:1987/:2005），提取
   question 文本，`event_store.append(dispatch_id, "help_request",
   text=question)`（:2013-2018）+ `task_watchdog.record_progress
   (source="input_required")`（:2018-2024）。
6. **注入 Coordinator Context**：`_build_task_status_view` 对
   `state == "INPUT_REQUIRED"` 的 dispatch 填 `help_request` 字段
   （coordinator_state_provider.py:763-773），Coordinator LLM 每轮可见。
7. **Coordinator 回复**：`send_message` 工具
   `message_type="reply_to_help"`（send_message.py:127-128 →
   `_handle_reply_to_help` :304）→ `_reply_dispatch_path`（:483，未确认
   的 dispatch 返回 `task_not_routable_yet`）→ `RespondWorkerTool
   .execute(task_id, response)`（respond_worker.py:59）。
8. **Worker 收到恢复消息**：`AgentAdapter.execute()` 对同 task_id
   `ctx.load_snapshot(task_id)` 命中（agent_adapter.py:213-219），
   `[RESUME]` 日志 + `start_work("Resuming after help")`，以
   `initial_messages=snapshot` 重新 submit（:222-238）。
9. **续跑**：`Controller.submit()` 恢复 `agent.messages`，
   `_find_pending_tool_call`（controller.py:126-145）向后扫描找到尚无
   tool 回复的挂起调用（跳过占位消息），把 Coordinator 回复作为该
   tool_call 的 `role="tool"` 消息注入（:245-256），agent 从暂停点继续。

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
| Mail sent | INFO | `Mail delivered id=<id> from=<sender> subj=<subj>` (receiver side, agent_adapter.py:526) |
| Mail send failure | WARNING | `send_control to <endpoint> failed: <reason>` (sender side, peer_sender.py:347) |
| Team installed | INFO | `Team '<id>' installed at epoch <N> (<M> members)` (team_state.py:177) |
| Team revoked | INFO | `Team '<id>' revoked at epoch <N>` (team_state.py:212) |
| Peer send error | WARNING | `a2a_send_mail to '<id>' failed: <reason>` (send_peer_mail.py:107) |

### Event callbacks (no body, no subject, no secret, no signature)

| Event Name | Fires From | Data Fields |
|------------|-----------|----------------------------------------|
| `mail_sent` | `WorkerPeerSenderService` (outside lock) | message_id, sender_id, recipient_id, team_id, team_epoch, outcome |
| `mail_delivery_failed` | `WorkerPeerSenderService` | message_id, recipient_id, error, team_id |
| `mail_rejected` | `WorkerPeerSenderService` (sync validation) | recipient_id, reason ——**无 team_id 字段**（peer_sender.py:157-163 实测：无活动 team 时 snap 尚不存在，载荷只有 recipient_id + reason） |
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
cd /home/wyh/daily_work/LLaMAR
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
- `EnvelopeAwareAdapter.cancel()` (line 489 of
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

### 5. No cross-subject TASK delegation

Workers cannot delegate tasks to peers via the mail system.  The
`a2a_send_mail` tool only creates `MAIL` envelopes, and
`EnvelopeIngress` rejects `TASK` envelopes from worker signers.  This
is by design -- only the coordinator can assign tasks.

### 6. `--enable-peer-mail` only in experiment.py

The benchmark runner (`sar_orch/benchmark.py`) does not support
`--enable-peer-mail`.  Only `experiment.py` supports it.

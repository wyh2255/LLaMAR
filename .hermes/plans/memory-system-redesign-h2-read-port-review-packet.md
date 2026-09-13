---
title: H2 Read-Port Cutover - Human Review Packet
status: PENDING - HUMAN DECISION REQUIRED
target_head: e1a5a01392c814ddcbc33f5e17aa5cad5d9e8fe6
design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
---

# H2 Read-Port Cutover Review Packet

## Decision Requested

Approve or reject enabling `memory_read_mode=read_port` on the reviewed target.
No approval is implied by this packet. Current default remains `legacy`; Phase 4's shadow path keeps legacy as the only LLM Context source.

## Acceptance Evidence

- **Legacy/new shadow comparison**: `memory_read_mode=shadow` compares the same scope and authenticated principal once per environment step. The aligned representative Worker-evidence flow reports `clean_runs=1` and `non_allowlist_diff_count=0`; a true domain mismatch is recorded to `<log_dir>/memory_rollout_audit.ndjson`.
- **ACL negative coverage**: active worker A cannot read active worker B's embodied position/inventory. Identity is resolved server-side from opaque `worker_task_id` to the current non-terminal dispatch; worker/viewer/scope/dispatch claims must match. Unknown task, unknown worker, expired proof, replayed proof, inactive dispatch, and cross-dispatch claim receive typed 403.
- **Freshness and view behavior**: provider tests cover `FRESH`, `STALE`, `UNAVAILABLE`, cursor monotonicity, epoch reset, section-budget priority, control-plane-only task state, pure renderer behavior, and NeedInput tool-call closure.
- **Rollback drill**: in `read_port` mode a provider/error/ACL gate failure latches `read_port -> legacy`, creates one redacted `read_port_to_legacy_rollback` audit record, and leaves canonical event count/revision unchanged. A successful read-port response does not roll back.
- **Online truth boundary**: provider and semantic-mode tests confirm no Barrier/oracle truth feeds canonical Memory or read-port view.

## Verification

```bash
env PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_environment_state_provider.py \
  tests/test_environment_state_acl.py \
  tests/test_environment_state_shadow.py \
  tests/test_environment_state_route.py \
  tests/test_environment_state_rollback.py \
  tests/test_context_protocol_closure.py \
  tests/test_worker_state_provider.py \
  tests/test_context_snapshot.py \
  tests/test_memory_online_truth_boundary.py \
  tests/test_worker_callback_signing.py \
  tests/test_memory_callback_auth.py -q
```

Result: `149 passed`; 42 pre-existing datetime deprecation warnings. `git diff --check` passed.

## Approval Boundary

- An `APPROVE` must bind this target HEAD, the two hashes above, this packet, and a precise rollout allowlist.
- Without that record, do not enable `read_port`; use `legacy` or explicit `shadow` only.
- H3 remains pending. This review does not authorize exporter/recovery/legacy retirement work.

---
title: H2 Read-Port Cutover - Human Review Packet v2
status: PENDING - HUMAN DECISION REQUIRED
gate_id: H2
target_head: 04147016e396429b03c8bb3d19b7165f631f8be2
design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
supersedes_packet: memory-system-redesign-h2-read-port-review-packet.md
superseded_packet_sha256: 8c7e575be3b582476e5a8e115ea27e81f3e10452378dd8d0450015a999a8d089
review_progress_sha256_at_freeze: 784d6ebfc932b4bb24d057438d0f6a1e3eefdcf87254e95ca5c2169f2f5229f5
---

# H2 Read-Port Cutover Review Packet v2

## Decision Requested

Approve or reject enabling `memory_read_mode=read_port` on target commit
`04147016e396429b03c8bb3d19b7165f631f8be2`. No approval is implied by this
packet. Until a separate approval record exists, the default remains `legacy`
and explicit `shadow` remains the only rollout evidence mode.

## Candidate Scope

- Included implementation: the committed changes in `0414701` only.
- Excluded worktree change: pre-existing `AGENTS.md` documentation edit.
- Excluded phases: H3 exporter/recovery, legacy retirement, and any change to
  the frozen H1 truth boundary.

## Rollout Allowlist

- Enable only `memory_read_mode=read_port` after approval, through the existing
  `EnvironmentStateProvider` bridge and authenticated worker route.
- Keep `legacy` as default and retain `shadow` comparison.
- Require the existing `SHADOW_COMPARE_ALLOWLIST` in
  `sar_orch/environment_state_provider.py`; zero non-allowlist differences is
  the gate. Do not add field or entity exceptions as part of rollout.
- Worker views remain restricted to the server-derived
  `worker_task_id -> active PhysicalDispatch -> worker_id` principal.
- Rollback is limited to the existing one-shot `read_port -> legacy` latch;
  canonical Memory and outbox remain unchanged.

## Acceptance Evidence

- Real shadow run:
  `sar_orch/results/shadow_genuine_0414701_20260807_225809`.
  `metadata.json` SHA-256:
  `e68ca540d2f96ada21db6d106d54003de97d202ca37c4f26f4513db4fb55e418`.
  `metadata.code_commit` is `0414701`.
- Live shadow result: no `coordinator/memory_rollout_audit.ndjson` was
  created, so there were zero non-allowlist differences. The direct run
  completed 11 steps and exited normally after coordinator completion.
- Deterministic replay result: `runs=1`, `clean_runs=1`,
  `non_allowlist_diff_count=0`, `last_clean=true`.
- Canonical DB pristine SHA-256:
  `bd56d0a2b81bc87d9d070fd663a744c653f97c45a190d74e2c7b51473b835c3d`.
  Replay opening the DB changes only SQLite schema-DDL bytes; row counts were
  unchanged. The pristine hash is the one bound here.
- Packet verification on this candidate: `171 passed, 46 warnings` using the
  11-file command in the original packet. Warnings are existing datetime
  deprecations.
- ACL, freshness, context closure, truth-boundary, rollback latch, secret
  redaction, canonical immutability, thread safety, and supervision adapter
  regressions are covered by the candidate tests.
- No live `read_port` LLM run or production rollback drill is included. Such a
  rollout is intentionally withheld pending this human gate; the existing
  rollback tests are the pre-cutover evidence.

## Approval Boundary

An approval record must bind this packet path and SHA-256, target HEAD,
design/H1 hashes, the review-progress freeze hash, and the rollout allowlist
above. Without that record, do not enable `read_port`. H3 remains pending.

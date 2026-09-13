---
title: H2 Read-Port Cutover Approval Record
schema_version: 1
gate_id: H2
conclusion: APPROVE
approved_at: 2026-08-08T10:54:17+08:00
reviewed_commit: 04147016e396429b03c8bb3d19b7165f631f8be2
branch: feat/memory-redesign
review_packet: .hermes/plans/memory-system-redesign-h2-read-port-review-packet-v2.md
review_packet_sha256: d9f84e35b8f581c98737e72d0d68582348e5300b30f772180e0a4115c14d1268
review_progress_sha256: 9e70c41fc5ca1108596d087dc00be1b15fe5a8aea9a9da2fae0eb0404163ad1b
design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
---

# H2 Read-Port Cutover Approval Record

## Human Decision

The user approved H2 in this session on 2026-08-08 with the instruction:

> 批准，进行真实实验scene 1 / 2 agents / seed 42 / max_steps 30

This authorizes `memory_read_mode=read_port` only on the reviewed candidate
`04147016e396429b03c8bb3d19b7165f631f8be2` for the requested real experiment
with scene 1, 2 agents, seed 42, and max_steps 30.

## Rollout Allowlist

- Enable `memory_read_mode=read_port` through the existing
  `EnvironmentStateProvider` bridge and authenticated worker route.
- Use only scene 1, 2 agents, seed 42, max_steps 30 for this rollout run.
- Keep `legacy` as the default mode and retain `shadow` as a separate compare
  mode.
- Worker views remain restricted by the server-derived
  `worker_task_id -> active PhysicalDispatch -> worker_id` principal.
- A provider, authentication, ACL, or freshness failure may use only the
  existing one-shot `read_port -> legacy` latch; canonical Memory and outbox
  must remain unchanged.
- No H3 activity is authorized: no exporter/recovery work, no legacy
  retirement, and no removal of legacy consumers.

## Preconditions Verified

- Target HEAD and branch matched the packet at approval time.
- `AGENTS.md` was the only pre-existing worktree modification and remains
  excluded from the reviewed source.
- Design and H1 card hashes match the frozen packet values.
- Packet v2 and review-progress hashes above were recomputed immediately before
  this record.

## Boundaries

This approval does not change the H1 online truth boundary. It does not
authorize Barrier, simulator, oracle, or evaluator truth to initialize,
correct, or backfill online canonical Memory. H3 remains `PENDING`.

---
title: H3 Retirement and Release Approval Record
schema_version: 1
gate_id: H3
conclusion: APPROVE
approved_at: 2026-08-10T12:04:03+08:00
reviewed_commit: a0d6712f26d5e80afdb349e6d65a89703b1514ed
branch: feat/memory-redesign
review_packet: .hermes/plans/memory-system-redesign-h3-retirement-release-review-packet.md
review_packet_sha256: 718a73a27ac6ceebbe64a70defc169c87599331629ec4ce0f0b3d1cae83e2f8b
review_progress_sha256: da7b48dc8b9e36929f5786df72b9a0154ca8342f6360b55442dcebbd677f8a61
review_progress_sha256_current: 11020545735eedb5ec977e98be4f05946b3f5850e5acef11bb2b228a5947b625
design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
design_sha256_current: 113157c968a3f679972a7cdfb18e715d56060f4864d12ce98baee6d02995fa6d
h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
h1_card_sha256_current: 8758d28aca48990576e0234528f9c9c915f88e34ab6ff512ba5b440595004c52
matrix_root: sar_orch/results/memory_acceptance_a0d6712_20260809_153738
---

# H3 Retirement and Release Approval Record

## Human Decision

The user approved H3 in this session on 2026-08-10 after reviewing the full
packet (all evidence numbers independently re-verified against the matrix
artifacts and evaluator source) with the instruction:

> 批准 H3：绑定 a0d6712，进入退役流程

This authorizes canonical Memory to operate as the official path
(`memory_read_mode=read_port`) on the reviewed target
`a0d6712f26d5e80afdb349e6d65a89703b1514ed` and authorizes starting the
legacy-main-path retirement process. The three residual risks were accepted
with the recorded rationale: (1) low Memory precision on broad truth traces
is a measurement-calibration artifact (final-state projection vs
per-step claims; `conflict_precision=0.0` has zero conflicted fields), not a
framework defect; (2) recovery is covered by Phase 5 test-level evidence;
(3) the `read_port -> legacy` rollback path is retained by design.

## Retirement Allowlist

- Canonical Memory (`read_port`) becomes the official path on the reviewed
  target commit; the final 10-run matrix
  `sar_orch/results/memory_acceptance_a0d6712_20260809_153738` is the bound
  evidence root (10/10 acceptance exit 0, 10/10 quality `measured`,
  `evidence_traceability_rate=1.0`, zero rollback audits, zero secrets).
- Legacy main path retirement is a *process*, not a single deletion: no
  legacy consumer is deleted by this approval. Deleting legacy consumers,
  making legacy data unavailable, or enabling automatic retention purge
  requires a separate retirement change reviewed against this record and the
  packet's approval boundary.
- The `read_port -> legacy` rollback latch and legacy data stay available
  (controlled retention) after retirement.
- Worker views remain restricted by the server-derived
  `worker_task_id -> active PhysicalDispatch -> worker_id` principal.
- No automatic retention purge may be enabled without a separate review.

## Preconditions Verified

- Target HEAD `a0d6712f26d5e80afdb349e6d65a89703b1514ed` and branch
  `feat/memory-redesign` matched at approval time; `AGENTS.md` remains the
  only pre-existing worktree modification and is excluded from the reviewed
  source scope.
- Review packet SHA-256 recomputed immediately before this record:
  `718a73a27ac6ceebbe64a70defc169c87599331629ec4ce0f0b3d1cae83e2f8b` — matches
  the frozen packet value.
- Design and H1 card hashes: the packet-front-matter frozen values
  (`cc81485e…`, `9c326b9a…`) differ from the current file hashes
  (`113157c9…`, `8758d28a…`). Cause verified: the 2026-08-10 plans-directory
  rename (semantic slugs, no date prefixes) rewrote path references inside
  those files via `sed`; no semantic content changed. This approval binds the
  frozen values as recorded in the packet and documents the current values
  for future verification.
- Progress doc hash: `review_progress_sha256` (`da7b48dc…`) is the value
  bound at approval time; `review_progress_sha256_current`
  (`11020545…`) is the hash after the 2026-08-13 status-only alignment
  (H3/retirement marked completed; no semantic or design change — see
  changelog in the progress doc).

## Boundaries

This approval does not change the H1 online truth boundary: online Memory
still consumes only authenticated Worker evidence, static registry metadata,
and control/supervision facts; Barrier/simulator/oracle/evaluator truth never
initializes, corrects, or backfills online canonical Memory. Evaluator truth
traces remain evaluator-private, frozen at terminal, and read-only after the
run. A subsequent legacy-retirement change is required before any legacy
consumer is removed.

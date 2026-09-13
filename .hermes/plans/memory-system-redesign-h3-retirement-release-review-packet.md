---
title: H3 Retirement and Release - Human Review Packet
status: PENDING - HUMAN DECISION REQUIRED
gate_id: H3
target_head: a0d6712f26d5e80afdb349e6d65a89703b1514ed
design_sha256: cc81485efa188c70285f0ff8df060a191f95731cd8d4fdbe49e69cc45fc93072
h1_card_sha256: 9c326b9a32c908d81c44c82cd723f9e366671c2b095ae52327659c8222090335
h2_approval_record_sha256: 08c9ffbe285d5bd063ff441c06f0b06ab8f9d958781f7a03cd8c2f9deae74e0c
matrix_root: sar_orch/results/memory_acceptance_a0d6712_20260809_153738
---

# H3 Retirement and Release Review Packet

## Decision Requested

Approve or reject retiring the legacy main path on target commit
`a0d6712f26d5e80afdb349e6d65a89703b1514ed` after the Phase 5 acceptance
evidence below. This packet does not itself retire legacy consumers.

## Scope and Boundary

- Candidate includes Phase 5 exporter/recovery, structured outcome taxonomy,
  terminal-only evaluators, compatibility materialization, evaluator-private
  truth recording, A2A dispatch hardening, and SAR invalid-action isolation.
- H2 `read_port` remains the active rollout mode; legacy is still available
  until this gate is approved and a subsequent retirement change is reviewed.
- No online path reads Barrier/oracle truth into canonical Memory. Truth traces
  are evaluator-private, recorded outside run result directories, frozen at
  terminal, and compared read-only after the run.
- The pre-existing `AGENTS.md` documentation modification is excluded from the
  candidate source scope.

## Required Evidence

- Focused Phase 0-5 verification: 254 passed on the Phase 5 candidate; later
  hardening regressions culminated in 1636 passed, 4 skipped before the final
  matrix candidate. New A2A early-return regression covers the former callback
  admission cycle.
- Final 10-run matrix: `sar_orch/results/memory_acceptance_a0d6712_20260809_153738`.
  All runs use scene 1-5, agents 2/4, seed 42, max_steps 20, semantic mode,
  `memory_read_mode=read_port`, and evaluator-private truth recording.
- Matrix result: 10/10 `memory_acceptance.json` valid with exit 0; coverage and
  transport non-null; `missing_error_code_rows=0`; `worker_busy`,
  `task_not_routable_yet`, and `unknown_task_id` counts all 0.
- Matrix result: 10/10 `memory_projection_quality.json` valid with exit 0 and
  `metric_status=measured`; every evidence traceability rate is 1.0. Each run
  has SHA-256-bound acceptance, quality, and truth-manifest artifacts in the
  matrix `final_report.md`.
- Recovery/export/compatibility: deterministic JSONL materialization and
  manifest digests, temp+fsync+replace, outbox replay, canonical scope fence,
  legacy-unmigrated marking, frozen semantic-map fixture, and render loader
  compatibility are covered by Phase 5 tests.
- Rollout safety: the final matrix has zero callback-auth rejections and zero
  `read_port_to_legacy_rollback` audits. There is no secret, proof, or raw
  evaluator truth in candidate-readable artifacts.

## Residual Risks

- Some runs contain known allowlisted LLM tool outcomes such as
  `participant_busy`, `invalid_plan`, or `node_not_ready`. They are recorded
  structurally, do not violate the three H3 framework-error gates, and do not
  create unknown/sentinel error codes.
- Quality metrics intentionally reveal low Memory precision on broad truth
  traces; H3 asks whether this measured behavior, recovery evidence, and the
  retained rollback path are acceptable for legacy retirement.

## Approval Boundary

An APPROVE must bind this packet path and SHA-256, target HEAD, frozen design
and H1 hashes, the final matrix root/report, and a precise retirement allowlist.
Without a separate H3 approval record, do not delete legacy consumers, make
legacy data unavailable, or enable automatic retention purge.

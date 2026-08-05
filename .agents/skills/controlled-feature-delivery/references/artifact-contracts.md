---
日期: 2026-07-24
文档类型: 机器契约
文档概述: 受控功能交付流程的 JSON 产物、审查和证据约束。
---

# Artifact Contracts

All verdicts are exactly `APPROVE` or `REJECT`. A reject must include a blocker;
an approval has no blockers. Each Major identifies a `target_phase` and a
testable `acceptance` condition.

`change-manifest.json` is the authority for phases, paths, document targets and
exact command specifications. Its digest is SHA-256 of canonical JSON with the
top-level `manifest_sha256` field omitted. Plan review binds the exact plan
bytes, manifest digest, base commit and review baseline fingerprint.

Verification records are written only by `changeflow.py`. They record redacted
argv, check scope, exit status, timeout state, log path, and environment
metadata without environment values. Metadata is limited to cwd, executable,
Python/platform versions, and sorted environment variable names. stdout/stderr
logs and argv redact API keys, access/auth tokens, bearer tokens, passwords,
secrets, and common provider key prefixes. Evidence is stale whenever either
the managed-tree digest or workspace fingerprint changes; missing, malformed,
incomplete, failed, or out-of-scope check records are rejected.

The managed tree includes tracked files, non-ignored untracked files, symlink
targets, deletions, renames, and executable mode. Scope comparison is exact:
every changed path must match the phase allowlist and no forbidden path may
change. A phase verification must bind its exact scope and complete required
check set to the current tree and workspace evidence.

The CLI never creates commits, pushes branches, calls an LLM, or changes global
agent configuration. `human-approve` records an approval reference; it is not
an identity or authorization mechanism.

Review artifacts include an exact `scope` binding. Phase reviews bind the phase,
base commit, and phase-start fingerprint/tree digest. Final review binds the
manifest, base commit, current tree digest, workspace fingerprint, and sealed
brief digest. Any later workspace change invalidates the final review.

Smoke artifacts bind `runner`, `resolved_path`, `skill_sha256`, and
`event_skill_sha256`. Runners that expose metadata bind the event digest
directly; OpenCode's current skill event binds the skill-tool event marker to
the locally resolved tree digest and records `event_proof` explicitly.

The run ledger is split between `state.json` and append-only `history.jsonl`.
`state.json` must contain `schema_version`, `change_id`, `state`, `revision`,
`active_phase`, `resume_state`, `owner`, `updated_at`, and
`history_tail_hash`. The tail hash is the `record_hash` of the final history
record and is updated atomically with every transition. Missing fields,
invalid values, a revision sequence gap, or any state/history tail mismatch is
rejected without repair by truncation or rewriting.

Every mutating command requires `expected_revision`. The run lock serializes
the read-check-write sequence, so concurrent writers presenting the same
revision have one success and all later writers fail closed with a revision
conflict.

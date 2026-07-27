---
name: project-documentation
description: Use when initializing, auditing, writing, or maintaining persistent project documentation, especially in an unfamiliar repository or library. Guides source-grounded repository reconnaissance and provides reusable templates for project, architecture, runbook, ADR, and change documents.
version: 1.0.0
author: LLaMAR Project
license: MIT
metadata:
  hermes:
    tags: [documentation, architecture, runbook, adr, repository, onboarding]
    related_skills: []
---

# Project Documentation

## Overview

This skill turns a repository's scattered notes into a small, reusable set of
persistent documents. It is deliberately platform-neutral: any coding agent can
load the skill and use the templates under `templates/`.

The objective is not to create more Markdown. The objective is to preserve the
minimum knowledge that must survive a chat session, an agent handoff, or a team
change:

1. why the project exists and what its boundary is;
2. how the current system actually works;
3. how to install, run, verify, recover, and operate it;
4. why important design choices were made;
5. what a proposed change intends to do and how it will be proved.

The code, configuration, tests, and executed evidence remain authoritative for
their respective facts. Documentation must not invent behavior that has not
been observed in the repository.

## When to Use

Use this skill when the user asks to:

- initialize documentation for a new or undocumented project;
- understand and document an unfamiliar repository or library;
- create a reusable project documentation template;
- audit duplicated, stale, or conflicting project docs;
- write or update project, architecture, runbook, ADR, or change documents;
- decide whether a code change has durable documentation impact;
- prepare an onboarding package for another human or coding agent.

Do not use it for:

- a one-off scratch note or chat summary;
- copying test output into canonical documentation;
- writing a daily diary when no durable fact changed;
- replacing a product requirements process, issue tracker, or API specification
  when those systems already own the contract;
- documenting an architecture from a README alone without checking the source.

## Operating Contract

Follow these rules on every run:

1. **Read before writing.** Inspect repository instructions, manifests, entry
   points, source, tests, and existing docs before changing any document.
2. **Separate fact from interpretation.** Every load-bearing claim must be
   backed by a source path, symbol, configuration key, test, command, or clearly
   marked runtime observation. Mark unknowns instead of filling them with a
   plausible guess.
3. **Keep one current source of truth per topic.** A historical plan, an Agent
   prompt, and a generated diagram must not silently compete with the current
   architecture or runbook.
4. **Use lifecycle status.** A draft, active plan, current architecture, stale
   runbook, and archived decision are different states. Never present an
   unverified proposal as current behavior.
5. **Do not copy secrets.** Record configuration names and safe placeholders,
   never API keys, tokens, passwords, private URLs, or real `.env` contents.
6. **Prefer minimal edits.** Preserve useful existing material, remove duplicate
   sources only when explicitly requested, and do not modify implementation code
   during a documentation-only task.
7. **Verify the result.** Check links, paths, commands, status metadata, and the
   final worktree scope. Report what was read, what was executed, and what was
   not verified separately.

## Standard Document Model

Use this default model unless the repository has a stronger existing contract:

| Layer | Canonical location | Answers | Lifecycle |
|---|---|---|---|
| Entry | `README.md` | What is this and how do I start in three minutes? | Current |
| Project | `docs/project/project.md` | Why does it exist, for whom, and within what boundary? | Current |
| Architecture | `docs/project/system_docs/architecture.md` | How does the current system work and who owns each state? | Current |
| Flow | `docs/project/system_docs/flow.md` | In which direction do data, events, control signals, and failures move? | Current |
| Operations | `docs/project/runbook.md` | How do I install, run, test, deploy, recover, and roll back? | Current |
| Decisions | `docs/project/decisions/` | Why was a consequential choice made? | Accepted/superseded |
| Changes | `docs/project/changes/` | What is this change trying to do and how will it be accepted? | Draft/active/verified/abandoned |
| Evidence | ignored run directory or CI artifacts | What was actually executed for one run or revision? | Run-bound; stale when inputs change |
| History | `docs/archive/` | What used to be true? | Immutable reference |
| Agent rules | `AGENTS.md`, `CLAUDE.md`, or platform files | How must an Agent operate in this repository? | Current policy, not system truth |

Default durable-document tree:

```text
docs/
├── README.md
└── project/
    ├── README.md
    ├── project.md
    ├── runbook.md
    ├── system_docs/
    │   ├── README.md
    │   ├── architecture.md
    │   └── flow.md
    ├── decisions/
    │   ├── README.md
    │   └── ADR-<NNNN>-<slug>.md
    └── changes/
        ├── README.md
        └── YYYY-MM-DD-<slug>.md
```

For a small project, `README.md` may absorb the project charter and runbook. For
a high-risk project, add `docs/project/contracts/`, `docs/project/security/`, or
a dedicated migration document instead of overloading `system_docs/`.

The reusable files in this skill are:

- `templates/root-readme.md`
- `templates/docs-index.md`
- `templates/project-index.md`
- `templates/system-docs-index.md`
- `templates/changes-index.md`
- `templates/project.md`
- `templates/architecture.md`
- `templates/flow.md`
- `templates/runbook.md`
- `templates/decisions-index.md`
- `templates/adr.md`
- `templates/change.md`
- `templates/repository-fact-inventory.md`
- `references/unfamiliar-repository-initialization.md`

## Using the Templates

1. Read the relevant template from this skill directory; do not paste a
   remembered version from another project.
2. Create only the directories needed by the project, for example
   `docs/project/system_docs/`, `docs/project/decisions/`, and
   `docs/project/changes/`.
3. Write the copied document to the project's canonical path and replace every
   placeholder with source-backed content. Keep `TODO`, `UNKNOWN`, or `NOT_RUN`
   only when the missing fact is explicit and has a next check.
4. Leave a new document in `draft` until its claims and commands are verified;
   then set the appropriate `current` or `accepted` status.
5. Re-read the completed document and run the verification checklist before
   reporting it as initialized.

## Repository Initialization Workflow

Use this workflow when a project has no reliable documentation or when the
repository is unfamiliar. Do not jump directly to writing `architecture.md`.

### Phase 0 — Freeze scope and safety

1. Confirm the repository root and the requested documentation scope.
2. Read all applicable repository instructions before touching files:
   `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `CONTRIBUTING.md`, and nested
   instruction files near the files being documented.
3. Inspect Git status and record pre-existing dirty paths. Do not overwrite
   unrelated work.
4. Decide whether the task is read-only reconnaissance, documentation-only
   initialization, or documentation plus implementation. If the user has not
   asked for implementation, do not change source code.
5. Check for secrets before reading or copying configuration content. Use names,
   types, and safe examples only.

Completion criterion: the repository root, applicable rules, baseline worktree
state, and write boundary are explicit.

### Phase 1 — Build a repository fact map

Search for files by pattern rather than assuming a language or layout. At
minimum inspect:

- manifests: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`,
  `pom.xml`, `build.gradle`, `requirements*.txt`, lock files;
- entry surfaces: console scripts, `__main__`, CLI routers, HTTP routes,
  server factories, worker processes, notebooks, and examples;
- quality gates: test configuration, CI workflows, lint/type/build settings;
- runtime files: Docker/compose, systemd, deployment manifests, migrations,
  schema files, config examples;
- existing docs: README variants, `docs/`, ADRs, plans, changelogs, reports,
  Agent instructions, and generated documentation.

Record findings in `templates/repository-fact-inventory.md` as a temporary
working artifact or in an ignored run directory. Do not treat the inventory as
canonical project documentation unless the user explicitly wants it committed.

For every candidate fact, record:

- claim;
- source path and exact symbol/section or command;
- evidence class: implementation, configuration, test intent, runtime result,
  or recommendation;
- confidence: confirmed, partial, contradicted, or unknown;
- document destination, if any.

Completion criterion: the inventory covers identity, entry points, dependencies,
architecture boundaries, persistence, configuration, verification, operations,
and open uncertainties.

### Phase 2 — Trace the real executable path

For a library or application, trace at least one real path end to end:

```text
package metadata / executable entry
  → parser or route registration
  → handler / dispatcher
  → service or core object
  → storage / provider / external boundary
  → result, side effect, and error path
```

For a library, additionally identify:

- the install/import name versus distribution name;
- public exports and supported entry points;
- the smallest working usage path;
- object or client lifecycle and resource ownership;
- serialization and error contracts;
- version and compatibility constraints;
- focused tests and examples that corroborate the public API.

Do not call a neighboring script, a stale README path, or an unregistered helper
the official entry point. If multiple surfaces exist, trace them separately and
state which kernel they share.

Completion criterion: an engineer can name the real entry symbol, registration
site, handler, core operation, durable side effect, and failure boundary.

### Phase 3 — Classify existing documents

Classify every relevant document into exactly one primary role:

- current project truth;
- current architecture or contract;
- operational instruction;
- accepted decision;
- change intent or plan;
- run evidence;
- historical archive;
- Agent/platform instruction;
- generated/derived output;
- scratch or obsolete material.

Build a source-of-truth table. If two documents claim the same topic, choose the
current authority based on executable source and explicit project policy. Keep
historical traceability, but label stale or superseded content instead of
silently merging incompatible claims.

Completion criterion: each retained document has a role, authority, status, and
migration or archival destination.

### Phase 4 — Write the canonical set in dependency order

Use the templates and write in this order:

1. `docs/README.md` — global documentation navigation;
2. `docs/project/README.md` — project-documentation navigation and authority;
3. `docs/project/project.md` — purpose, scope, constraints, success criteria;
4. `docs/project/system_docs/README.md` and `architecture.md` — current
   components, state ownership, contracts, persistence, and boundaries;
5. `docs/project/system_docs/flow.md` — directional data/event/control flow,
   transformations, correlation fields, persistence boundaries, and
   failure/feedback paths;
6. `docs/project/runbook.md` — setup, commands, configuration, verification,
   operations, recovery, and rollback;
7. `docs/project/decisions/README.md` and individual ADRs for accepted choices;
8. `docs/project/changes/README.md` and
   `docs/project/changes/<date>-<slug>.md` only when an active or proposed
   change exists.

A document can contain a short summary, but it must link to the authority for
its detailed claim. Do not turn `docs/README.md` into a duplicate architecture
or status report.

Completion criterion: a new maintainer can navigate from `README.md` through
`docs/project/` to the current project, system, flow, runbook, and decision
sources without guessing.

### Phase 5 — Verify and report honestly

Before declaring documentation initialized:

- parse front matter and check required fields;
- resolve every relative Markdown link and referenced path;
- search for placeholders that were not intentionally retained as `TODO` or
  `UNKNOWN`;
- confirm commands against the manifest and, when safe, execute the smallest
  deterministic smoke check;
- label commands that were read but not executed;
- check that no secret values were copied;
- check that every current fact has a source or explicit uncertainty marker;
- confirm generated artifacts are not presented as the source of truth;
- re-run Git status and confirm only the requested documentation paths changed.

Completion criterion: the final report distinguishes implemented/current,
planned, verified, not run, blocked, stale, and unknown states.

## How to Write Each Document

### `README.md`

Keep it short and action-oriented:

1. one-sentence identity;
2. problem and primary capability;
3. quickstart;
4. smallest verification command;
5. high-level repository map;
6. links to canonical docs;
7. limitations and support boundary.

Do not put a long historical narrative or every environment variable here.

### `docs/project/project.md`

Write the project charter:

- purpose and intended outcome;
- users, actors, or consumers;
- in-scope and out-of-scope behavior;
- constraints and assumptions;
- success criteria and non-goals;
- dependencies and ownership boundaries;
- stable glossary;
- current status only at milestone level, with an `as_of` date.

Do not use it as a daily progress log.

### `docs/project/system_docs/architecture.md`

Write current behavior, not desired architecture:

- system boundary and external actors;
- components and responsibility/ownership table;
- real entry points and representative call chains;
- high-level data-flow boundary and state transitions; link to sibling
  `flow.md` for directional edge-by-edge detail;
- logical versus physical/opaque ID namespaces;
- persistence and migration ownership;
- public contracts and error semantics;
- concurrency, timeout, retry, cancellation, and recovery behavior;
- observability and important output artifacts;
- known limitations and verified uncertainties.

Every diagram must have a textual explanation and a source reference. Generated
HTML/SVG is derived output, not the only authority.

### `docs/project/system_docs/flow.md`

Create this as a standalone document for multi-component, event-driven,
asynchronous, queued, data-pipeline, or multi-agent systems. A small project
may keep a compact flow section inside `architecture.md` instead.

For a new documentation baseline, place this file under
`docs/project/system_docs/`. If an existing repository already centralizes
system references under `docs/system_docs/`, retain that existing path as the
authority unless migration is explicitly requested. Do not create both paths as
independent authorities.

The flow document must make direction executable and reviewable:

- one explicit producer and consumer per edge;
- trigger and payload/schema on every edge;
- transformation, validation, and normalization between nodes;
- synchronous versus asynchronous transport;
- state owner and persistence/commit boundary;
- request/task/event/version/correlation fields;
- return, feedback, cancellation, timeout, retry, and error paths;
- positive and negative tests or source evidence for important edges.

Do not use an unlabeled `A <-> B` arrow. Split it into `A → B` and `B → A`
with separate semantics. Keep control signals distinct from business data, and
keep an overview diagram plus an edge table so the prose remains auditable.

### `docs/project/runbook.md`

Write executable instructions:

- prerequisites and supported environments;
- installation and dependency setup;
- configuration keys with safe placeholders;
- development, test, lint, build, and run commands;
- deployment and service startup order;
- logs, health checks, and diagnostic locations;
- backup, migration, recovery, and rollback;
- common failure symptoms and root-cause checks;
- security boundaries and cleanup.

Separate commands that were verified from commands that are only documented.

### ADRs

Create one ADR per consequential decision. Use `templates/adr.md` and include
context, alternatives, decision, consequences, migration/reversal, and a
revisit trigger. An ADR records why; it does not replace the current
architecture or runbook.

### Change documents

Use `templates/change.md` for changes that cross files, modules, APIs, state,
persistence, security, concurrency, migrations, or multiple agents. Include
invariants, phase gates, negative tests, rollback, documentation impact, and
final evidence. A plan is not approved merely because it exists; an independent
review or human gate, when required by the repository, must be explicit.

## Documentation Impact Rules

Update canonical documentation when a change affects:

| Change | Update |
|---|---|
| project purpose, users, scope, constraints | `docs/project/project.md` |
| component boundary, state ownership, public behavior | `docs/project/system_docs/architecture.md` |
| producer/consumer direction, event/data transformations, correlation, feedback, failure path | `docs/project/system_docs/flow.md` or existing `docs/system_docs/flow.md` |
| commands, config, deployment, recovery, troubleshooting | `docs/project/runbook.md` |
| a consequential design choice or trade-off | an ADR |
| a proposed multi-file or high-risk change | `docs/project/changes/` |
| only local implementation details with no contract impact | usually no durable doc |
| test output for one run | run evidence, not current docs |

When a change affects no canonical document, record `doc-impact: none` in the
change evidence if the project has a change ledger. When it does affect one,
name the exact target and verify it after implementation.

## Common Pitfalls

1. **Writing from the README alone.** A README is a navigation hint, not proof
   of the runtime call chain.
2. **Confusing a plan with current behavior.** Keep proposed and implemented
   states separate and use explicit status values.
3. **Duplicating facts in Agent instructions.** Put behavior rules in
   `AGENTS.md`/`CLAUDE.md`; link to project docs for architecture and commands.
4. **Creating a giant `key_facts.md`.** Split by purpose and maintain one
   authority per topic.
5. **Treating a generated diagram or report as canonical.** Keep the source
   Markdown and code/config references authoritative.
6. **Claiming a command passed because it appears in a document.** A command is
   `verified` only after execution in the relevant environment.
7. **Hiding uncertainty.** Use `UNKNOWN`, `PARTIAL`, or `NOT_RUN` with a next
   check instead of inventing a clean story.
8. **Copying secrets from config files.** Document key names and redacted shapes,
   never values.
9. **Archiving too early.** Archive only when a replacement current source is
   identified and links preserve traceability.
10. **Modifying implementation during reconnaissance.** First finish the fact
    map; separate documentation gaps from code defects.

## Verification Checklist

- [ ] Applicable repository instructions were read.
- [ ] Git baseline and requested write scope were recorded.
- [ ] Manifests, entry points, source, tests, runtime config, and existing docs were inspected.
- [ ] At least one representative executable path was traced end to end.
- [ ] Existing docs were classified by role and authority.
- [ ] Current project, system architecture, flow, and runbook docs have status, owner, date, and authority metadata.
- [ ] Multi-component or event-driven projects have a directional flow document, or an explicit reason why the architecture flow section is sufficient.
- [ ] Every important flow edge names producer, consumer, trigger, payload/schema, sync mode, owner, and failure behavior.
- [ ] Return, feedback, cancellation, retry, and error paths are represented separately from the forward data flow.
- [ ] ADRs are one-decision-per-file and have alternatives and revisit triggers.
- [ ] Active changes are separate from current system truth.
- [ ] Every load-bearing claim has evidence or an explicit uncertainty marker.
- [ ] Commands are labeled verified, not run, blocked, or stale.
- [ ] Markdown links and referenced paths resolve.
- [ ] No secrets or generated outputs were copied as source truth.
- [ ] Final Git status confirms only the requested documentation scope changed.

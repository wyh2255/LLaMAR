# Initializing Documentation for an Unfamiliar Repository or Library

This reference is the detailed procedure behind the `project-documentation`
Skill. It covers both meanings of initialization:

1. initializing a reliable documentation baseline for a codebase you do not yet
   understand;
2. initializing the local runtime environment safely enough to perform a
   deterministic smoke check.

The first is mandatory. The second is performed only when the user requests it
or when a safe verification command is needed.

## 1. Establish the repository boundary

Record:

- absolute repository root;
- current branch and revision;
- pre-existing dirty and untracked paths;
- applicable `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `CONTRIBUTING.md`, and
  nested instruction files;
- requested documentation scope and non-goals.

Never infer the root from a README link. Resolve it from the filesystem and Git.
Never overwrite a dirty file merely because it looks like the natural target.

## 2. Discover the project shape

Use repository file search to locate, without assuming a language:

| Area | Candidate files |
|---|---|
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `uv.lock`, `poetry.lock` |
| JavaScript/TypeScript | `package.json`, lock files, `tsconfig.json`, workspace files |
| Rust | `Cargo.toml`, `Cargo.lock` |
| Go | `go.mod`, `go.sum` |
| JVM | `pom.xml`, `build.gradle`, `settings.gradle` |
| Build/deploy | `Makefile`, `Taskfile.yml`, `Dockerfile`, compose files, Helm, Terraform, CI workflows |
| Runtime/data | migrations, schema files, config examples, service units, scripts |
| Docs and agent contracts | README variants, `docs/`, ADRs, plans, `AGENTS.md`, `CLAUDE.md`, `.agents/` |

Read the smallest useful section of each manifest first:

- project/package name and version;
- runtime version constraint;
- dependencies and optional groups;
- console scripts or package exports;
- test, lint, build, and format configuration;
- workspace or local path dependencies.

Do not install dependencies before understanding the declared environment. Do
not read real credential files when a manifest or example is enough.

## 3. Find the real entry points

Trace each relevant surface separately:

### Library surface

```text
distribution metadata
  → import package
  → package __init__ / public export
  → public class or function
  → provider/storage/resource boundary
```

Confirm the import name, exported names, constructor requirements, lifecycle,
return shape, exceptions, and cleanup behavior from source and focused tests.

### CLI surface

```text
console script / module entry
  → parser builder
  → subcommand registration
  → handler
  → service/core call
  → exit status and side effects
```

Distinguish parser registration from the handler and from any background worker.

### HTTP/API surface

```text
server factory / application object
  → router mount
  → route handler
  → service/provider
  → serialization and error response
```

Record the externally mounted prefix, authentication boundary, request/response
schema, and startup/shutdown ownership.

### Worker or job surface

```text
launcher / scheduler
  → worker entry
  → queue/claim gate
  → task handler
  → persistence/result/artifact write
```

Record retry, timeout, cancellation, lease/claim, and cleanup semantics.

Completion criterion for each surface: a reader can follow one invocation from
entry to result and name the first durable or externally visible side effect.

## 4. Trace persistence and state ownership

If the project stores data, inspect together:

- path resolution and default location;
- connection/session initialization;
- schema or model definitions;
- migrations/version markers;
- transaction boundaries and locking;
- primary identifiers and external identifiers;
- state/status values and allowed transitions;
- audit/event records;
- cleanup and retention.

For a library without local persistence, inspect resource ownership instead:
clients, sockets, files, subprocesses, caches, threads, and event loops.

Do not call a cache, vector index, generated file, or log the canonical store
without source evidence. Record the canonical owner in
`docs/project/system_docs/architecture.md`.

## 5. Read tests as contracts, not runtime proof

Focused tests reveal intended behavior and edge cases. Classify them as:

- test intent read;
- test executed and passed;
- test executed and failed;
- test not runnable due to environment;
- test coverage unknown.

A test file is not evidence that the test currently passes. If execution is
allowed, run the smallest deterministic test or smoke path first, then expand
only when the result is useful and safe.

## 6. Safely initialize the runtime environment

Only perform this section when needed. Prefer the repository's declared package
manager and documented command:

1. create or use an isolated environment;
2. install declared dependencies without reading or printing secrets;
3. configure only safe environment variables or placeholders;
4. run a `--help`, import, version, compile, or offline unit test smoke check;
5. capture command, working directory, exit status, and whether network/external
   services were involved;
6. clean temporary outputs outside the repository or in an ignored directory.

Do not silently launch production services, send external requests, mutate real
databases, or create credentials during documentation initialization. If the
smallest smoke check cannot run, document the blocker and continue the source
reconnaissance; do not fabricate runtime evidence.

## 7. Build the fact inventory

Use `templates/repository-fact-inventory.md`. Every row should have this shape:

| Claim | Evidence | Class | Confidence | Destination |
|---|---|---|---|---|
| exact claim | `path:line`, symbol, command, or runtime record | implementation/config/test/runtime/recommendation | confirmed/partial/contradicted/unknown | project/architecture/runbook/ADR/none |

High-value rows include:

- project identity and intended users;
- supported execution surfaces;
- source tree boundaries;
- data/state owners;
- producer/consumer direction for the main data and event flows;
- transformations, correlation/version fields, persistence boundaries, and
  feedback/error edges;
- configuration precedence;
- public interfaces and error semantics;
- test/lint/build commands;
- deployment and recovery;
- known mismatches between docs and code;
- open questions that block a confident statement.

Do not write the final architecture document until the high-value rows are
confirmed or explicitly marked unknown.

## 8. Convert the inventory into canonical documents

Use the templates in this order:

1. `docs/README.md` — global source-of-truth map and read paths;
2. `docs/project/README.md` — project-documentation navigation;
3. `docs/project/project.md` — stable charter;
4. `docs/project/system_docs/README.md` and `architecture.md` — verified
   current system;
5. `docs/project/system_docs/flow.md` — directional data/event/control flow
   and failure paths when the system has multiple components or asynchronous
   boundaries;
6. `docs/project/runbook.md` — runnable operations;
7. `docs/project/decisions/README.md` and ADRs — accepted choices;
8. `docs/project/changes/README.md` and `docs/project/changes/` — only when
   active/proposed work exists.

Move facts to one authority only. A short navigation link is acceptable; a full
second copy is not.

## 9. Close the initialization gate

The initialization is complete only when:

- each canonical document has front matter and a stated owner;
- all `CURRENT` claims have source evidence or an explicit verification note;
- all `UNKNOWN`/`NOT_RUN` items name the next check;
- every important flow edge names its producer, consumer, trigger,
  payload/schema, transport mode, owner, and failure behavior;
- return, feedback, cancellation, retry, and error flows are separated from
  the forward flow;
- quickstart and verification commands agree with the manifest;
- no broken relative links remain;
- no credentials were copied;
- no implementation files changed unless separately authorized;
- the final report lists paths created/modified and tests or probes actually run.

---
doc_id: architecture
doc_type: current-architecture
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "source-code-and-tests"
audience: [maintainers, reviewers]
---

# <Project Name> Architecture

## Authority and scope

- Describes: <components, runtime, or subsystem covered>
- Does not describe: <out-of-scope systems>
- Source revision or verification date: `<revision or YYYY-MM-DD>`
- Canonical code/config roots: `<paths>`

## System boundary

```text
<external actor or input> → <project boundary> → <external dependency/output>
```

Explain the boundary in prose. Name external systems, trust boundaries, and
which component owns each durable state.

## Real entry points

| Surface | Entry symbol/path | Registration or mount | Handler/core path | Visible result |
|---|---|---|---|---|
| CLI/API/library/worker | `<path:symbol>` | `<path:symbol>` | `<path:symbol>` | `<result>` |

Representative call chain:

```text
<entry> → <registration/router> → <handler> → <service/core>
  → <storage/provider> → <result/side effect>
```

## Components and ownership

| Component | Responsibility | Owns state | Depends on | Source |
|---|---|---|---|---|
| `<component>` | `<responsibility>` | `<state or NONE>` | `<dependency>` | `<path:symbol>` |

## High-level data-flow boundary

See [`flow.md`](flow.md) for the directional edge-by-edge flow contract.

1. `<input or event>` enters through `<entry>`.
2. `<normalization/validation>` is owned by `<component>`.
3. `<dispatch/service operation>` reaches `<component>`.
4. `<persistence/external call>` is owned by `<component>`.
5. `<output/event/artifact>` leaves through `<boundary>`.

Keep this section as a boundary summary. State the owner and link to the flow
document instead of duplicating every edge, payload, and retry path here.

## State and contracts

### States and transitions

```text
<STATE_A> → <STATE_B> → <TERMINAL_STATE>
```

| State or transition | Preconditions | Owner | Failure outcome | Evidence |
|---|---|---|---|---|
| `<transition>` | `<precondition>` | `<owner>` | `<rollback/degrade/retry>` | `<path:test>` |

### Identifiers and versions

| Namespace | Meaning | Created by | Persisted where | Must not be confused with |
|---|---|---|---|---|
| `<logical-id>` | `<meaning>` | `<owner>` | `<store>` | `<other namespace>` |

### Public contracts

| Contract | Producer | Consumer | Shape/version | Error semantics |
|---|---|---|---|---|
| `<API/event/file>` | `<path>` | `<path>` | `<schema>` | `<behavior>` |

## Persistence and lifecycle

- Canonical store: `<database/files/service or NONE>`
- Schema/migration owner: `<owner or NONE>`
- Transaction/locking model: `<description>`
- Cache/index/generated outputs: `<derived layers and rebuild rule>`
- Resource cleanup: `<files/clients/threads/processes/event loops>`

## Concurrency, timeout, retry, and recovery

- Concurrency model: `<threads/processes/event loops/none>`
- Timeout boundary: `<where and what happens>`
- Retry/idempotency: `<rule>`
- Cancellation/shutdown: `<rule>`
- Partial failure/recovery: `<rule>`

## Observability

| Signal or artifact | Producer | Location/schema | Diagnostic use |
|---|---|---|---|
| `<log/metric/event>` | `<path>` | `<path or endpoint>` | `<use>` |

## Known limitations and uncertainties

- `CONFIRMED`: <fact with source>
- `NOT_RUN`: <command/probe not executed and why>
- `UNKNOWN`: <open question and next check>
- `STALE`: <claim or document that must not be used as current truth>

## Source index

- `<path:symbol or section>` — <claim supported>
- `<path:test>` — <contract corroborated>

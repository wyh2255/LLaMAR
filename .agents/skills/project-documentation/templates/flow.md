---
doc_id: flow
doc_type: directional-flow
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "source-code-and-tests"
audience: [maintainers, reviewers, operators]
---

# <Project Name> Flow

## Scope and reading rules

- Covered flow: `<request/event/data pipeline name>`
- Entry point: `<producer, route, command, event, or scheduled trigger>`
- Terminal output: `<response, state change, artifact, or external effect>`
- Source revision or verification date: `<revision or YYYY-MM-DD>`

This document is directional. Every arrow must have one source and one
immediate destination. Do not use an unlabeled bidirectional arrow to hide two
different flows. Represent return, feedback, retry, and error paths as separate
labeled edges.

Use this edge notation:

```text
[Producer] --(trigger | payload/schema | sync/async)--> [Consumer]
                  | transform/validation: <operation>
                  | owner/persistence: <owner or NONE>
                  | failure/retry: <behavior>
```

## Flow map

```text
[<source>] --(<request/event>)--> [<transport/router>]
[<transport/router>] --(<normalized payload>)--> [<service/worker>]
[<service/worker>] --(<state mutation/result>)--> [<store/external system>]
[<store/external system>] --(<response/event>)--> [<consumer>]
```

The diagram is only an overview. The table below is the executable flow
contract.

## Forward data/event flow

| Step | From | To | Trigger | Payload/schema | Transform/validation | Sync or async | State owner/persistence | Failure/retry | Evidence |
|---:|---|---|---|---|---|---|---|---|---|
| 1 | `<producer>` | `<consumer>` | `<request/event/clock>` | `<shape/version>` | `<operation>` | sync/async | `<owner/store or NONE>` | `<behavior>` | `<path:symbol/test>` |

## Control and feedback flows

Control flow is not the same as business data flow. Record cancellation,
acknowledgement, scheduling, backpressure, supervision, and replanning here.

| Flow ID | From | To | Signal | When emitted | Consumer action | Terminal or feedback | Evidence |
|---|---|---|---|---|---|---|---|
| F-CTRL-01 | `<component>` | `<component>` | `<cancel/ack/retry/heartbeat>` | `<condition>` | `<action>` | terminal/feedback | `<path:test>` |

## Return, error, and retry flows

| Flow ID | Origin step | Failure condition | Error shape/state | Destination | Retry/idempotency | Cleanup or recovery |
|---|---:|---|---|---|---|---|
| F-ERR-01 | `<step>` | `<condition>` | `<exception/event/status>` | `<handler/store/actor>` | `<rule>` | `<rule>` |

## Identity, version, and correlation fields

| Field | Produced at | Carried through | Consumed at | Purpose | Missing/mismatch behavior |
|---|---|---|---|---|---|
| `<request_id/task_id/event_id/version>` | `<source>` | `<edges or envelope>` | `<consumer>` | `<correlation/ordering/auth>` | `<reject/retry/degrade>` |

## Durable boundaries and side effects

| Boundary | Write/read | Owner | Transaction or commit point | Rebuild/replay rule | Evidence |
|---|---|---|---|---|---|
| `<database/file/queue/external API>` | read/write | `<owner>` | `<point>` | `<rule or NONE>` | `<path:symbol>` |

## Flow invariants

- Every accepted `<input/event>` has exactly one declared owner for its next
  state or terminal outcome.
- `<ordering/idempotency/authorization/invariant>`.
- `<timeout/cancellation/backpressure invariant>`.
- `<privacy/security/data-retention invariant>`.

## Unknowns and verification status

- `CONFIRMED`: `<flow claim with source>`
- `NOT_RUN`: `<runtime flow not executed and why>`
- `UNKNOWN`: `<missing producer/consumer/edge and next check>`
- `STALE`: `<old flow document or diagram that must not be used>`

## Source index

- `<path:symbol>` — `<producer/consumer/transform or failure claim>`
- `<path:test>` — `<positive/negative flow assertion>`

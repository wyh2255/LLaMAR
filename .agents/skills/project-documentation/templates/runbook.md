---
doc_id: runbook
doc_type: operations
status: draft
owner: "<team-or-person>"
updated: "YYYY-MM-DD"
authority: "verified-commands-and-operations"
audience: [developers, operators]
---

# <Project Name> Runbook

## Supported environment

| Item | Supported value or constraint | Evidence/status |
|---|---|---|
| OS/runtime | `<value>` | `<path or verified/not_run>` |
| Package manager | `<command/tool>` | `<manifest>` |
| External services | `<service or NONE>` | `<config/source>` |

## Prerequisites

- `<runtime/tool>` `<version or range>`
- `<service/credential/config>` — describe the key name and safe placeholder;
  never commit the real value.

## Installation

```bash
<exact install command>
```

Status: `verified | not_run | blocked` — `<date and evidence>`

## Configuration

| Key | Required | Safe example | Precedence/source | Secret? |
|---|---:|---|---|---:|
| `<KEY>` | yes/no | `<placeholder>` | `<CLI > env > file>` | yes/no |

## Development commands

### Test

```bash
<focused deterministic test command>
```

Status: `verified | not_run | blocked` — `<result/evidence>`

### Lint/type/build

```bash
<lint command>
<type-check command>
<build command>
```

Status: `verified | not_run | blocked` — `<result/evidence>`

## Run locally

```bash
<start command>
```

Startup order, ports, health checks, and shutdown behavior:

1. `<step>`
2. `<step>`
3. `<health check or expected output>`

## Deploy and operate

- Deployment entry: `<command/service/CI job>`
- Persistent data: `<location and owner>`
- Logs and metrics: `<locations/endpoints>`
- Health/readiness check: `<command or endpoint>`
- Backup/retention: `<rule>`

## Troubleshooting

| Symptom | First checks | Likely cause | Recovery | Evidence |
|---|---|---|---|---|
| `<symptom>` | `<commands>` | `<cause>` | `<safe action>` | `<path/issue>` |

## Migration and rollback

- Migration command: `<command or NONE>`
- Precondition/backup: `<rule>`
- Rollback command or procedure: `<procedure or NONE>`
- Irreversible operation: `<explicitly state>`

## Cleanup and security

- Temporary output cleanup: `<procedure>`
- Credential handling: `<safe rule>`
- Data deletion/privacy boundary: `<rule>`
- Never run: `<dangerous command or production-only action>`

## Verification record

Commands in this document are not automatically verified because they are
written here. Record actual execution separately with command, working
 directory, exit status, and artifact path.

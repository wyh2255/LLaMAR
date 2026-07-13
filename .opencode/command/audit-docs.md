---
description: Audit project code against all documentation files in docs/system_docs/.
---

You are a **documentation audit agent**. Your job is to verify whether the actual code in this project matches the descriptions in `docs/system_docs/`.

## Process

1. **Discover all documents** — list all `*.md` files under `docs/system_docs/`.

2. **Launch one subagent per document in parallel** (use `task` with `subagent_type: "general"`). Give each subagent:
   - The document file path (so it reads the doc itself)
   - The universal verification criteria below

   DO NOT read any documents yourself — let each subagent read its assigned file.

3. **Wait for all subagents to complete**, then synthesize a final report.

### Universal verification criteria (apply to every document)

- **Paths & files**: every directory and file path referenced in the document actually exists on disk
- **Classes & modules**: every class, function, or module mentioned exists at the documented location with the documented signature
- **Architecture claims**: threading model, communication patterns, data flow, and design decisions match the actual implementation
- **Line numbers**: every `file:line` reference points to the correct line in the current code (flag outdated line numbers)
- **Command examples**: all shell commands are runnable (correct flags, env vars, required `no_proxy` if applicable)
- **Stated constraints**: any claimed limits (timeout values, caps, skip conditions, etc.) match the code constants
- **Completeness**: flag any major component or flow that exists in the code but is undocumented

## Output format

```markdown
# Documentation Audit Report

## <filename>.md — [PASS|MINOR|MAJOR]
- ✅ / ❌ item 1: description
- ⚠️ item 2: description (minor)
```

For each issue, include the file path and line number where the discrepancy was found. Do NOT modify any files — only report.

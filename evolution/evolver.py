#!/usr/bin/env python3
"""
LLaMAR Agent Evolver — Hermes-native prompt evolution engine.

Sends a self-contained analysis prompt to Hermes (via hermes chat -q),
pointing it to the experiment data so Hermes can read files itself.
Parses the structured response to extract improved prompts.

Usage:
  python evolver.py
    --results <path/to/experiment_results>
    --prompt-dir <path/to/current/prompts>
    --gen <generation_number>
    --target <coordinator|worker|all>
"""

import argparse
import json
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone


# ─── CLI ────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True)
    p.add_argument("--prompt-dir", required=True)
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--target", default="coordinator", choices=["coordinator", "worker", "all"])
    return p.parse_args()


# ─── Build analysis prompt ──────────────────────────────────────────────
def build_prompt(results_dir: str, prompt_dir: str, target: str) -> str:
    target_label = {
        "coordinator": "Coordinator",
        "worker": "Worker",
        "all": "Coordinator and Worker",
    }[target]

    return f"""You are an AI agent evolution system. Your task is to analyze SAR (Search & Rescue) multi-agent experiment results and generate improved system prompts.

## Data

The experiment results are at: {results_dir}
The current system prompts are at: {prompt_dir}

### Files to read:
- {results_dir}/trajectory.csv — per-step metrics (coverage, transport_rate, actions, successes)
- {results_dir}/events.ndjson — full event log (dispatch, tool call, callback, barrier, env step)
- {results_dir}/summary.tsv (or summary.csv) — aggregate metrics
- {results_dir}/metadata.json — experiment config (scene, agents, seed, model, prompt_version)
- {results_dir}/subtasks.csv — subtask completion timeline
- {results_dir}/agent_interactions.csv — per-agent tool calls with args and LLM output
- {results_dir}/router_interactions.csv — coordinator dispatch history
- {results_dir}/token_usage.csv — prompt/completion/cache tokens per LLM call
- {prompt_dir}/coordinator/system.md — CURRENT system prompt for the Coordinator
- {prompt_dir}/worker/system.md — CURRENT system prompt for the Worker

## What to analyze

1. **Failure patterns**: What went wrong? Look at coverage/transport rates, timeout agents, event errors, tool call failures, and end_reason.
2. **Prompt weaknesses**: Where did the current prompts fail? Look for:
   - Repeated actions → missing state memory guidance
   - Invalid tool calls → weak tool schema instructions
   - Premature NoOp/Finish → missing completion conditions
   - Idle agents → poor dispatch strategy guidance
   - Step budget mismanagement → missing time awareness
3. **Improvement opportunities**: What specific prompt changes would address these patterns?

## Output format — YOU MUST FOLLOW THIS EXACTLY

First, read ALL the data files listed above. Then respond with:

Analysis Summary
- Top failure pattern: ...
- Root cause: ...
- Specific metrics: ...

Improved Prompt for {target_label}

[Write the COMPLETE improved system prompt for the {target_label} here — a full replacement for the current prompt, not amendments.]
IMPORTANT: Include the FULL prompt text inline above. Do NOT write it to a file using tools — just output it in your response text. The system reads it from your response, not from disk.

Changes Summary
- Change 1: [what changed and why]
- Change 2: [what changed and why]
- ...

## Evolution Guidelines

- Preserve the existing prompt structure and tone
- Only add constraints that address observed failures
- Keep prompts concise — don't add fluff
- Add specific, actionable rules (not vague suggestions)
- Reference specific experiment observations to justify each change

## Anti-Cheating Rules — READ CAREFULLY

You are generating a **general-purpose** system prompt that must work across ALL scenes (1-5), agent counts (1-6), seeds, and environment configurations — not just the experiment you analyzed.

**STRICTLY FORBIDDEN:**
- Never hardcode entity names like 'ReservoirUtah', 'ReservoirYork', 'CaldorFire', 'GreatFire' — use generic placeholders '{{ReservoirName}}', '{{FireName_Region_N}}'
- Never hardcode agent names ('Alice should do X') — agents are dynamic
- Never hardcode grid positions, coordinates, or scene-specific numbers
- Never embed experiment-specific step counts, coverage values, or metric thresholds as constants
- Never reference a specific scene number ('In Scene 1, ...')

**ALLOWED:**
- General reasoning patterns ('Before using a tool, verify you're at the correct location')
- General anti-patterns ('Do not dispatch scouting-only tasks')
- General strategy rules ('Always dispatch the longest useful action chain')
- Structural improvements (adding missing sections, clarifying ambiguous rules)

**Rule of thumb**: If the improvement works just as well on a completely different scene with different entity names — it's good. If it relies on specifics from the trajectory you observed — it's cheating."""


# ─── Response parser ────────────────────────────────────────────────────
def parse_response(response: str, target: str) -> dict:
    result = {
        "analysis": "(no analysis extracted)",
        "changes": "(no changes extracted)",
        "coordinator": None,
        "worker": None,
    }

    # Analysis Summary (no ## prefix, may have ### etc)
    m = re.search(
        r"(?:#{0,3}\s*)?Analysis Summary\s*\n([\s\S]*?)(?=\n(?:#{0,3}\s*)?(?:Improved Prompt|Changes Summary))",
        response,
    )
    if m:
        result["analysis"] = m.group(1).strip()

    # Changes Summary
    m = re.search(
        r"(?:#{0,3}\s*)?Changes Summary\s*\n([\s\S]*?)$",
        response,
    )
    if m:
        result["changes"] = m.group(1).strip()

    # Extract a prompt section
    def extract_prompt(header: str):
        pattern = (
            r"(?:#{0,3}\s*)?" + re.escape(header) + r"\s*\n+"
            r"([\s\S]*?)(?=\n(?:#{0,3}\s*)?(?:Changes Summary|Improved Prompt for))"
        )
        m = re.search(pattern, response)
        if not m:
            return None
        prompt = m.group(1).strip()
        # Strip markdown code fences if present
        prompt = re.sub(r"^```\w*\n", "", prompt)
        prompt = re.sub(r"\n```$", "", prompt)
        return prompt.strip() or None

    if target in ("coordinator", "all"):
        result["coordinator"] = extract_prompt("Improved Prompt for Coordinator")
    if target in ("worker", "all"):
        result["worker"] = extract_prompt("Improved Prompt for Worker")

    return result


# ─── Hermes bridge ──────────────────────────────────────────────────────
def call_hermes(prompt: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["hermes", "chat", "-q", prompt, "--quiet"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hermes chat exited with code {result.returncode}\n"
            f"STDERR: {result.stderr[:1000]}"
        )
    # In --quiet mode, output format is:
    #   Warning: ... (optional)
    #   session_id: xxx
    #   <actual response>
    out = result.stdout
    # Strip session_id line and any warnings
    lines = out.split("\n")
    content_lines = [
        line for line in lines
        if line.strip()
        and not line.startswith("session_id:")
        and not line.startswith("Warning:")
        and not line.startswith("Query:")
    ]
    return "\n".join(content_lines)


# ─── Main ───────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    ROOT = Path(__file__).resolve().parent
    OUT_DIR = ROOT / "prompts" / f"gen_{args.gen}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Hermes Agent Evolver  |  gen={args.gen}  target={args.target}")
    print(f"  results: {args.results}")
    print(f"  prompts: {args.prompt_dir}")

    # Build and send prompt
    print("  [1/3] Sending analysis prompt to Hermes...")
    prompt = build_prompt(
        str(Path(args.results).resolve()),
        str(Path(args.prompt_dir).resolve()),
        args.target,
    )

    raw = call_hermes(prompt)
    (OUT_DIR / "raw_response.txt").write_text(raw)
    print(f"  [2/3] Raw response: {OUT_DIR / 'raw_response.txt'} ({len(raw)} chars)")

    # Parse and write outputs
    print("  [3/3] Parsing response...")
    parsed = parse_response(raw, args.target)

    outputs = []
    if parsed["coordinator"]:
        p = OUT_DIR / "coordinator" / "system.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(parsed["coordinator"])
        outputs.append(str(p))
    if parsed["worker"]:
        p = OUT_DIR / "worker" / "system.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(parsed["worker"])
        outputs.append(str(p))

    summary = {
        "generation": args.gen,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_results": args.results,
        "source_prompts": args.prompt_dir,
        "target": args.target,
        "analysis": parsed["analysis"],
        "changes": parsed["changes"],
        "generated_files": outputs,
    }
    (OUT_DIR / "generation_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    outputs.append(str(OUT_DIR / "generation_summary.json"))

    print("\n" + "\n".join(f"  ✓ {o}" for o in outputs))
    print(f"\n✔ Done. generated_files={len([o for o in outputs if 'system.md' in o])}")

    # Structured output for orchestrator
    print("\n---EVOLUTION_RESULT_START---")
    print(json.dumps(summary, ensure_ascii=False))
    print("---EVOLUTION_RESULT_END---")


if __name__ == "__main__":
    main()

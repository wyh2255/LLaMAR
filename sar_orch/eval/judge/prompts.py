"""Judge prompt templates (one prompt per metric — single responsibility).

Design principles (card decision #1, AliExpress Judge Task practice):

1. **Single responsibility** — one prompt judges exactly one metric; the two
   metrics below never share a call.
2. **Reason first, conclude second** — the strict output template places the
   ``reasoning`` field before the verdict field and the system prompt says so
   explicitly.
3. **Negative-example guidance** — every system prompt carries worked
   good/bad (or contradiction true/false) contrast pairs.
4. **Strict JSON output** — the format template is the LAST thing in the
   system prompt; the parser tolerates fences/prose but errs closed and the
   retry loop re-asks with a corrective suffix (see ``client.py``).

Language: English, consistent with the SAR worker/coordinator prompts and the
(all-English) artifact text the judge reads.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# planning_path (L2)
# --------------------------------------------------------------------------

PLANNING_PATH_SYSTEM_PROMPT = """\
You are a senior evaluator for a multi-agent search-and-rescue (SAR) simulation.
Your single job: audit the QUALITY OF THE COORDINATOR'S DISPATCH PATH — how the plan and its dispatches unfolded over time — and list every substantive flaw you can evidence as a deduction. You do NOT score the path: the score is computed deterministically downstream from your deductions. You also do NOT judge mission outcome: success / coverage / transport rates are measured elsewhere, and you do not see them. A well-planned mission that still failed must not be blamed here; a lucky completion with chaotic scheduling must still be flagged.

## Mission rules the coordinator is working with
- Fires: chemical fires need Sand to extinguish; non-chemical fires accept Water or Sand. A fire has several regions; every discovered region needs handling.
- Reservoirs are infinite (Sand and Water), the deposit facility can hold resources, and a trapped person needs 2+ robots carrying simultaneously, followed by drop-off at the deposit.
- Agents discover the scene by exploring; the coordinator only knows what has been reported to it.

## Inputs you receive
1. SCENE — the initial grid layout: fires (with named regions and positions), reservoirs with resource types, the deposit, the trapped person, and agent start positions.
2. DISPATCH TIMELINE — every coordinator tool call in step order: assign_task / update_plan / activate_plan_node / cancel_task / reply_to_help / finish_task and their result rows (including failures such as invalid_plan).
3. SUBTASK LOG — the lifecycle of every dispatch (assigned -> canceled / completed / failed).

## Deduction categories (use exactly one id per deduction)
- missing_dispatch: a discovered critical object or needed work never got dispatched.
- wrong_order: dispatch sequence violates a real dependency (e.g. a firefighting task before the matching supply can be obtained; a rescue activation before the multi-robot team is assembled).
- redundant_cancel: the same work is dispatched and canceled repeatedly (churn), or a just-dispatched task is canceled without a changed reason.
- incomplete_coverage: some agents are left without tasks for long stretches while work remains, or the same work is piled onto one agent while the others idle.
- ignored_help: a worker's request for help or input never gets a reply or follow-up dispatch.

## Rules
- Judge ONLY what the timeline and subtask log show. Never invent dispatches and never assume hidden knowledge.
- Every deduction must cite the step number(s) and the concrete row(s) it is based on.
- A clean path is an EMPTY deductions list; never pad it with harmless imperfections. Report only substantive flaws, one entry per occurrence (if the same category happens again at another step, add another entry with the same category — the downstream scoring applies its own per-category cap).
- Suboptimal-but-harmless choices (one redundant exploration, a short-lived re-plan that changes nothing) are NOT deductions; do not report them.
- Do NOT output a score — scoring is computed deterministically downstream from your deductions. Output only the reasoning and the deductions.

## Examples
GOOD PATH ({"reasoning": "...", "deductions": []}): step 0 dispatches exploration to all agents; once reports arrive the coordinator updates the plan, assigns each agent a fire with the matching reservoir nearby, and activates the person rescue only after the person is located; help requests are answered on the next step — nothing substantive is wrong, so the list stays empty.
BAD PATH ({"reasoning": "...", "deductions": [{"category": "redundant_cancel", "detail": "steps 3-6: same exploration task re-dispatched and canceled four steps in a row for agent Alice (rows ...)"}, {"category": "missing_dispatch", "detail": "step 12 onward: person located but no rescue work was ever dispatched (rows ...)"}, {"category": "ignored_help", "detail": "step 9: Bob's help request got no reply and no follow-up dispatch (rows ...)"}]}): three separate evidenced flaws, each its own entry.

## Output format (STRICT)
Respond with exactly ONE JSON object, nothing else — no code fences, no prose before or after it. Write the reasoning FIRST, then the deductions. There is NO score field. A clean path looks like: {"reasoning": "...", "deductions": []}
{"reasoning": "<step-by-step reasoning: what the plan did, which dependencies held or broke, which flaws you found>", "deductions": [{"category": "<one of the ids above>", "detail": "<what happened, with step numbers and rows>"}]}
"""


def build_planning_path_user_prompt(
    *,
    scene_digest: str,
    dispatch_timeline: str,
    subtask_log: str,
    timeline_truncated: bool,
    subtask_log_truncated: bool,
) -> str:
    """Assemble the planning_path user prompt from the run artifacts."""
    parts = [
        "## SCENE (initial layout, written before step 0)",
        scene_digest.strip() or "(scene digest unavailable)",
        "",
        "## DISPATCH TIMELINE (coordinator tool calls, step order)",
    ]
    if timeline_truncated:
        parts.append(
            "[timeline compacted: rows were sampled uniformly to fit the "
            "input budget — treat the shown order as authoritative]"
        )
    parts.append(dispatch_timeline.strip() or "(no dispatches recorded)")
    parts.append("")
    parts.append("## SUBTASK LOG (dispatch lifecycle)")
    if subtask_log_truncated:
        parts.append(
            "[subtask log compacted: rows were sampled uniformly to fit the "
            "input budget]"
        )
    parts.append(subtask_log.strip() or "(no subtask rows recorded)")
    parts.append("")
    parts.append(
        "Audit the dispatch path quality and list every evidenced deduction; "
        "answer with the single JSON object."
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------
# observation_ignore (L3)
# --------------------------------------------------------------------------

OBSERVATION_IGNORE_SYSTEM_PROMPT = """\
You are a strict evaluator for a search-and-rescue (SAR) robot. Your single job: decide whether the robot's action decision CONTRADICTS the latest observation it had just received.

## What you receive
1. TASK — the instruction the robot is currently executing (from the coordinator).
2. LATEST OBSERVATION — the most recent perception the robot had before deciding:
   - the result(s) of its previous tool call(s), and
   - the auto-injected Environment State snapshot: current positions, inventories, fire regions and intensities, discovered objects, and task execution state. (It is a bounded digest; long framework event feeds were removed.)
3. DECISION — the robot's output for this turn: its message plus the tool call(s) it chose.

## Contradiction definition — be conservative, report only clear conflicts
contradiction = true ONLY when the decision directly conflicts with a fact the latest observation/state clearly states, for example:
- the state shows the robot's inventory has no Water ("Water: 0") but the decision calls use_supply with water on a fire;
- the state marks a fire region as "status: extinguished" yet the decision navigates there to extinguish it;
- the observation shows where a person is (or the task fixes where a drop-off must happen) but the decision carries / drops the person somewhere else;
- the decision declares the task finished while the state shows the task's goal is unmet;
- the decision goes to an object that the observation proves is at a different position or no longer exists.

contradiction = false when any of these hold:
- the decision is merely suboptimal, inefficient, redundant, or uncertain (an extra explore, an extra supply top-up) but nothing it relies on is contradicted;
- the observation does not mention the thing the decision acts on — acting under incomplete information is NOT a contradiction;
- the decision waits / calls no_op / reports observations / queries status and no observation demands immediate action;
- the decision relies on older information that the latest observation has not negated.

## Rules
- Base the verdict ONLY on the text given to you. Never use outside knowledge or the simulator ground truth.
- If the evidence is ambiguous, incomplete or the observation digest looks truncated at the relevant point, choose false.
- Write the reasoning FIRST, then the JSON conclusion.

## Examples
CONTRADICTION TRUE: the Environment State lists the robot's inventory as "Water: 0" while the decision calls use_supply("Water") on a non-chemical fire region — the action needs water it does not have.
CONTRADICTION FALSE: the Environment State shows the rescuer is next to the deposit with a person on board, and the decision calls drop_off_person() — consistent with the observation.

## Output format (STRICT)
Respond with exactly ONE JSON object, nothing else — no code fences, no prose after it:
{"reasoning": "<what the observation says, what the decision does, why it does or does not contradict — written FIRST>", "contradiction": <true or false>}
"""


def build_observation_ignore_user_prompt(
    *,
    agent: str,
    step: int,
    task_instruction: str | None,
    observation_digest: str,
    decision_text: str,
) -> str:
    """Assemble one observation_ignore sample prompt."""
    parts = [
        f"## TASK (agent {agent}, step {step})",
        (task_instruction or "(task instruction not recoverable from the log)").strip(),
        "",
        "## LATEST OBSERVATION (what the robot saw before deciding)",
        observation_digest.strip() or "(no prior observation)",
        "",
        "## DECISION (what the robot did)",
        decision_text.strip() or "(no decision content)",
        "",
        "Does this decision contradict the latest observation? Answer with the "
        "single JSON object.",
    ]
    return "\n".join(parts)

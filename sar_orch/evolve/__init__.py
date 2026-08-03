"""Skill self-evolution: candidate generation -> static gate -> experiment -> fitness.

Rebuilt rather than repaired. The previous `evolution/` had three independent
breaks: candidate prompts never reached the experiment (`prompt_dir` was
discarded), there was no fitness function (the orchestrator only printed), and
generations rotated unconditionally so a worse candidate still got promoted. Those
are design gaps, not bugs, so the old code is archived rather than patched.

What changed structurally:

  * candidates reach the experiment through `--skills-dir`, which is now a real
    parameter end-to-end (verified by an explicit regression test, because the
    same class of defect -- a parameter accepted, recorded in metadata, and never
    in effect -- has appeared three times in this codebase)
  * fitness is `eval/gate.py`, so promotion is decided by the same code that
    guards every other comparison
  * a failed candidate leaves `sar_orch/skills/` byte-identical
  * anti-cheating is enforced mechanically, before any LLM cost is spent
"""

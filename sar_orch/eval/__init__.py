"""SAR orchestration eval package.

Memory System Redesign Phase 5 evaluators live in submodules:
- ``memory_acceptance``         run acceptance artifact (gate on framework
                                error codes / missing error codes)
- ``memory_projection_quality`` terminal-only, read-only projection quality
                                comparison against an evaluator-private truth

Eval-agent dataset / judge tooling (separate submodules under this package):
- ``dataset``                   EpisodeDataset / StepRecord / AgentInteraction
- ``gate`` / ``workflow`` / ``matrix`` / ``rubric_registry`` / ...
"""

from __future__ import annotations

from sar_orch.eval.dataset import EpisodeDataset, StepRecord, AgentInteraction

__all__ = ["EpisodeDataset", "StepRecord", "AgentInteraction"]

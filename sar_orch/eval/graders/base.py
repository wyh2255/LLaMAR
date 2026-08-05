from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sar_orch.eval.dataset import EpisodeDataset


@dataclass
class GradeResult:
    grader: str
    level: str
    passed: bool | None = None
    score: float | None = None
    detail: dict = field(default_factory=dict)
    evidence_ref: str = ""


class Grader(Protocol):
    name: str

    def grade(self, episode: "EpisodeDataset") -> list[GradeResult]: ...

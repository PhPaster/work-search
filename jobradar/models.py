"""Core data structures shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Verdict = Literal["apply", "maybe", "skip"]


@dataclass
class Vacancy:
    """A vacancy normalised to one schema regardless of the source site."""

    source: str  # "djinni" | "workua" | "robotaua"
    source_id: str
    url: str
    title: str
    company: str = ""
    city: str = ""
    remote: bool | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str = ""
    posted_at: str = ""  # ISO-8601 if known
    description: str = ""
    # True when `description` is only a short teaser (e.g. from an email alert)
    description_is_snippet: bool = False
    query: str = ""  # which search produced it (debugging)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)

    def salary_text(self) -> str:
        if self.salary_from is None and self.salary_to is None:
            return ""
        cur = self.currency or ""
        if self.salary_from and self.salary_to and self.salary_from != self.salary_to:
            return f"{self.salary_from}–{self.salary_to} {cur}".strip()
        return f"{self.salary_from or self.salary_to} {cur}".strip()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreResult:
    score: int  # 0..100
    verdict: Verdict
    matched: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    why: str = ""
    cover_hook: str = ""
    scorer: str = ""  # "llm:<model>" | "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreResult":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

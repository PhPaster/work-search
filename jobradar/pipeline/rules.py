"""Cheap deterministic filters applied after the title matched and details were fetched."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..models import Vacancy
from ..text import normalize

# rough conversion for the min-salary rule only
_TO_USD = {"USD": 1.0, "$": 1.0, "EUR": 1.08, "UAH": 1 / 41.5, "": 1.0}


@dataclass
class Rules:
    exclude_keywords: list[str] = field(default_factory=list)  # in title/company/description
    min_salary_usd: int | None = None  # only applied when the salary is known
    max_age_days: int | None = None  # only applied when posted_at is known

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        return cls(
            exclude_keywords=[normalize(k) for k in cfg.get("exclude_keywords") or []],
            min_salary_usd=cfg.get("min_salary_usd"),
            max_age_days=cfg.get("max_age_days"),
        )

    def check(self, v: Vacancy, now: datetime | None = None) -> tuple[bool, str]:
        hay = f" {normalize(v.title)} {normalize(v.company)} {normalize(v.description)} "
        for kw in self.exclude_keywords:
            if kw and f" {kw}" in hay:
                return False, f"keyword: {kw}"

        if self.min_salary_usd and (v.salary_to or v.salary_from):
            top = v.salary_to or v.salary_from or 0
            rate = _TO_USD.get(v.currency.upper(), 1.0)
            if top * rate < self.min_salary_usd:
                return False, f"salary {v.salary_text()} < {self.min_salary_usd} USD"

        if self.max_age_days and v.posted_at:
            try:
                posted = datetime.fromisoformat(v.posted_at)
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
                now = now or datetime.now(timezone.utc)
                if now - posted > timedelta(days=self.max_age_days):
                    return False, f"older than {self.max_age_days} days"
            except ValueError:
                pass
        return True, ""

"""Cross-site duplicate detection: the same job is often posted on 2–3 sites."""

from __future__ import annotations

import re

from ..models import Vacancy
from ..text import normalize

_NOISE = {
    "senior", "middle", "junior", "remote", "віддалено", "удаленно", "full", "time", "fulltime",
    "the", "of", "and", "з", "зі", "та", "і", "по", "в", "у", "of", "for",
    "тов", "пп", "фоп", "llc", "ltd", "inc", "group", "компанія", "компания",
}


def _tokens(s: str) -> list[str]:
    s = re.sub(r"\(.*?\)", " ", s or "")  # drop "(remote)", "(iGaming)" etc.
    return [t for t in normalize(s).split() if t not in _NOISE]


def dedup_key(v: Vacancy) -> str:
    """company|title with noise removed; empty if company is unknown (can't dedup safely)."""
    company = " ".join(_tokens(v.company))
    if not company:
        return ""
    title = " ".join(sorted(_tokens(v.title)))
    return f"{company}|{title}"

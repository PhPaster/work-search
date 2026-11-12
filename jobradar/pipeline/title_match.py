"""Is this title one of the target roles?

Rule: a *role* word (head, director, lead, chief, директор, керівник…) within a short distance
of a *domain* word (marketing, brand, strategy, маркетинг, бренд, стратег…), or an exact
C-level title (CMO, Chief Marketing Officer…). Exclusions (performance, SMM, client service…)
come from config and veto the match.

This is a cheap, generous pre-filter; the LLM makes the real decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..text import normalize

DEFAULT_ROLE_STEMS = [
    "head", "director", "lead", "leader", "chief", "vp", "директор", "керівник",
    "керівниц", "начальник", "руководител", "очільник", "лід",
]
DEFAULT_DOMAIN_STEMS = [
    "marketing", "brand", "strategy", "strategic", "strategist",
    "маркетинг", "бренд", "стратег",
]
DEFAULT_EXACT = [
    "cmo", "chief marketing officer", "chief brand officer", "chief strategy officer",
    "vp marketing", "vp of marketing", "vice president marketing",
    "vice president of marketing", "маркетинг директор", "бренд директор",
]


@dataclass
class TitleMatcher:
    role_stems: list[str] = field(default_factory=lambda: list(DEFAULT_ROLE_STEMS))
    domain_stems: list[str] = field(default_factory=lambda: list(DEFAULT_DOMAIN_STEMS))
    exact: list[str] = field(default_factory=lambda: list(DEFAULT_EXACT))
    exclude: list[str] = field(default_factory=list)
    window: int = 3  # max distance in tokens between role word and domain word

    @classmethod
    def from_config(cls, cfg: dict) -> "TitleMatcher":
        m = cls()
        m.role_stems += cfg.get("extra_role_stems") or []
        m.domain_stems += cfg.get("extra_domain_stems") or []
        m.exact += cfg.get("extra_exact") or []
        m.exclude = [normalize(x) for x in cfg.get("exclude") or []]
        m.window = int(cfg.get("window", m.window))
        return m

    def _is(self, token: str, stems: list[str]) -> bool:
        return any(token.startswith(s) for s in stems)

    def match(self, title: str) -> tuple[bool, str]:
        """Returns (matched, reason)."""
        norm = normalize(title)
        padded = f" {norm} "
        tokens = norm.split()
        for ex in self.exclude:
            if not ex:
                continue
            # multi-word exclusion: phrase match; single word: token prefix match
            hit = f" {ex} " in padded if " " in ex else any(t.startswith(ex) for t in tokens)
            if hit:
                return False, f"excluded: {ex}"
        for ex in self.exact:
            e = normalize(ex)
            if re.search(rf"(?<!\w){re.escape(e)}(?!\w)", norm):
                return True, f"exact: {ex}"
        roles = [i for i, t in enumerate(tokens) if self._is(t, self.role_stems)]
        domains = [i for i, t in enumerate(tokens) if self._is(t, self.domain_stems)]
        for r in roles:
            for d in domains:
                if 0 < abs(r - d) <= self.window:
                    return True, f"role+domain: {tokens[r]} / {tokens[d]}"
        if not roles:
            return False, "no leadership word"
        if not domains:
            return False, "no marketing/brand/strategy word"
        return False, "role and domain words too far apart"

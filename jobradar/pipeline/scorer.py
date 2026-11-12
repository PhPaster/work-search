"""Scoring a vacancy against the owner's profile.

LLMScorer      — Claude via the official Anthropic SDK, strict JSON output (output_config.format).
HeuristicScorer — no-API fallback: keyword overlap with the profile. Used when there is no
                  ANTHROPIC_API_KEY or with `--no-llm`, so the pipeline always works end to end.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

import yaml

from ..models import ScoreResult, Vacancy
from ..text import normalize, truncate

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 fit"},
        "verdict": {"type": "string", "enum": ["apply", "maybe", "skip"]},
        "matched": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "cover_hook": {"type": "string"},
    },
    "required": ["score", "verdict", "matched", "gaps", "red_flags", "why", "cover_hook"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """\
You are a senior executive-search consultant screening job vacancies for one specific candidate.
Decide whether the candidate should apply, and explain it briefly and concretely.

How to judge:
- Seniority and scope first: the candidate targets head/director/C-level roles in marketing,
  brand and strategy. A manager-level or narrow specialist role (SMM, performance, SEO, CRM)
  is a poor fit even if the title says "lead".
- Then substance: brand strategy, positioning, communication and go-to-market work, team
  leadership, budget ownership, agency/client-side experience, the industries in the profile.
- Respect the candidate's deal-breakers strictly: if one clearly applies, verdict is "skip".
- Missing information (no salary, vague description) is a red flag, not a reason to skip.
- If the description is only a short e-mail snippet, judge mostly by title/company and say
  that details must be checked on the page.

Scoring: 80-100 strong fit -> "apply"; 55-79 worth a look -> "maybe"; below 55 -> "skip".

Output language: Ukrainian for `why`, `matched`, `gaps`, `red_flags` and `cover_hook`.
`why`: 2-3 sentences. `cover_hook`: one sentence the candidate could open a cover letter with,
referring to a concrete case or achievement from the profile that fits this vacancy.

<candidate_profile>
{profile}
</candidate_profile>
{calibration}"""


def build_system_prompt(profile: dict, feedback_examples: list[dict] | None = None) -> str:
    calibration = ""
    if feedback_examples:
        lines = [
            f"- {e['title']} @ {e.get('company') or '?'} (your score {e.get('score')}) -> "
            f"candidate {'APPLIED' if e['feedback'] == 'applied' else 'SKIPPED'}"
            for e in feedback_examples
        ]
        calibration = (
            "\n<past_decisions>\nThe candidate's own reactions to earlier recommendations. "
            "Use them to calibrate:\n" + "\n".join(lines) + "\n</past_decisions>\n"
        )
    return SYSTEM_TEMPLATE.format(
        profile=yaml.safe_dump(profile, allow_unicode=True, sort_keys=False).strip(),
        calibration=calibration,
    )


def vacancy_prompt(v: Vacancy, max_chars: int = 12000) -> str:
    facts = {
        "source": v.source,
        "url": v.url,
        "title": v.title,
        "company": v.company or "unknown",
        "location": v.city or "unknown",
        "remote": {True: "yes", False: "no", None: "unknown"}[v.remote],
        "salary": v.salary_text() or "not specified",
        "posted_at": v.posted_at or "unknown",
        "description_kind": "short e-mail snippet" if v.description_is_snippet else "full text",
    }
    head = "\n".join(f"{k}: {val}" for k, val in facts.items())
    desc = truncate(v.description or "(no description)", max_chars)
    return f"<vacancy>\n{head}\n\n<description>\n{desc}\n</description>\n</vacancy>"


def _verdict_for(score: int, thresholds: dict) -> str:
    if score >= thresholds.get("apply", 80):
        return "apply"
    if score >= thresholds.get("maybe", 55):
        return "maybe"
    return "skip"


class Scorer(Protocol):
    name: str

    def score(self, v: Vacancy) -> ScoreResult: ...


class LLMScorer:
    def __init__(
        self,
        profile: dict,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        thresholds: dict | None = None,
        feedback_examples: list[dict] | None = None,
        client=None,
    ):
        import anthropic  # imported lazily so the heuristic path works without a key

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.thresholds = thresholds or {"apply": 80, "maybe": 55}
        self.system = build_system_prompt(profile, feedback_examples)
        self.name = f"llm:{model}"

    def score(self, v: Vacancy) -> ScoreResult:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=[FALLBACK_BETA],
            fallbacks="default",  # server-side fallback model if the request is declined
            system=self.system,
            cache_control={"type": "ephemeral"},  # profile/system prompt is the stable prefix
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
            },
            messages=[{"role": "user", "content": vacancy_prompt(v)}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined to score {v.url}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(f"output truncated for {v.url}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        score = max(0, min(100, int(data["score"])))
        # keep the verdict consistent with our thresholds; the deal-breaker "skip" always wins
        verdict = data["verdict"] if data["verdict"] == "skip" else _verdict_for(score, self.thresholds)
        return ScoreResult(
            score=score,
            verdict=verdict,
            matched=list(data.get("matched") or []),
            gaps=list(data.get("gaps") or []),
            red_flags=list(data.get("red_flags") or []),
            why=data.get("why") or "",
            cover_hook=data.get("cover_hook") or "",
            scorer=f"llm:{resp.model}",
        )


class HeuristicScorer:
    """Keyword overlap between the profile and the vacancy. Crude but free and offline."""

    name = "heuristic"

    def __init__(self, profile: dict, thresholds: dict | None = None):
        self.thresholds = thresholds or {"apply": 80, "maybe": 55}
        kw = profile.get("keywords") or {}
        self.strong = [normalize(k) for k in kw.get("strong") or []]
        self.plus = [normalize(k) for k in kw.get("plus") or []]
        self.deal_breakers = [normalize(k) for k in kw.get("deal_breakers") or []]
        self.senior_title = [normalize(k) for k in profile.get("target_titles") or []]

    def score(self, v: Vacancy) -> ScoreResult:
        title = normalize(v.title)
        hay = f" {title} {normalize(v.company)} {normalize(v.description)} "
        flags = [k for k in self.deal_breakers if k and f" {k}" in hay]
        if flags:
            return ScoreResult(
                score=10, verdict="skip", red_flags=[f"deal-breaker: {f}" for f in flags],
                why="Знайдено стоп-фактор із профілю.", scorer=self.name,
            )
        score = 45
        if any(t and t in title for t in self.senior_title):
            score += 15
        strong = [k for k in self.strong if k and f" {k}" in hay]
        plus = [k for k in self.plus if k and f" {k}" in hay]
        score += min(30, 6 * len(strong)) + min(10, 3 * len(plus))
        red = []
        if not v.salary_text():
            red.append("зарплата не вказана")
        if v.description_is_snippet:
            red.append("лише фрагмент з e-mail — перевір повний опис")
        score = max(0, min(100, score))
        return ScoreResult(
            score=score,
            verdict=_verdict_for(score, self.thresholds),
            matched=strong + plus,
            red_flags=red,
            why="Евристична оцінка (без LLM): збіг ключових слів профілю з описом.",
            scorer=self.name,
        )

"""Scoring a vacancy against the owner's profile.

GeminiScorer    — Google Gemini over REST (free tier available), JSON-schema output.
OllamaScorer    — a local model via Ollama (fully free and offline), JSON-schema output.
LLMScorer       — Claude via the official Anthropic SDK, strict JSON output (output_config.format).
HeuristicScorer — no-API fallback: keyword overlap with the profile. Used with `--no-llm`
                  or when no provider is configured, so the pipeline always works end to end.

All LLM scorers share the same system prompt, schema and result post-processing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import httpx
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
- Follow the candidate's `scoring_guidance` (priorities, penalties) exactly as written.
- Missing information (no salary, vague description) is a red flag, not a reason to skip.
- If the description is only a short e-mail snippet, judge mostly by title/company and say
  that details must be checked on the page.

Scoring: 80-100 strong fit -> "apply"; 55-79 worth a look -> "maybe"; below 55 -> "skip".

Language: write EVERY text value in Ukrainian — `why`, every item of `matched`, `gaps`,
`red_flags`, and `cover_hook` (keep established English terms such as GTM, P&L, SaaS as is).
`matched` / `gaps`: short phrases naming the specific requirement of the vacancy and the
matching (or missing) experience of the candidate, e.g. "GTM на ринки ЄС — досвід TRENDFORMER".
`why`: 2-3 sentences. `cover_hook`: one sentence the candidate could open a cover letter with;
it MUST name a specific company, case or measurable result from the profile (e.g. Effie for
McDonald's at TBWA, Target launch at TRENDFORMER, MilTech clients at TWID) that is the most
relevant to this vacancy — never a generic claim.

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


JSON_REMINDER = (
    "\n\nReturn ONLY a JSON object with keys: score (integer 0-100), verdict "
    '("apply"|"maybe"|"skip"), matched (array of strings), gaps (array of strings), '
    "red_flags (array of strings), why (string), cover_hook (string)."
)


def result_from_data(data: dict, thresholds: dict, scorer: str) -> ScoreResult:
    """Validate model JSON and keep the verdict consistent with our thresholds."""
    if not isinstance(data, dict) or "score" not in data:
        raise ValueError(f"model returned no score: {str(data)[:200]}")
    score = max(0, min(100, int(round(float(data["score"])))))
    raw_verdict = str(data.get("verdict", "")).lower()
    # a deal-breaker "skip" from the model always wins; otherwise thresholds decide
    verdict = "skip" if raw_verdict == "skip" else _verdict_for(score, thresholds)

    def strings(key: str) -> list[str]:
        val = data.get(key) or []
        return [str(x) for x in val] if isinstance(val, list) else [str(val)]

    return ScoreResult(
        score=score,
        verdict=verdict,
        matched=strings("matched"),
        gaps=strings("gaps"),
        red_flags=strings("red_flags"),
        why=str(data.get("why") or ""),
        cover_hook=str(data.get("cover_hook") or ""),
        scorer=scorer,
    )


def _parse_json_text(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):  # tolerate ```json fences from weaker models
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(text[start : end + 1])


class RateLimited(RuntimeError):
    """Provider unavailable: stop scoring for this run, the rest waits for the next run.

    quota=True means the daily quota is used up (HTTP 429 on every model): the runner then
    pauses LLM scoring until the provider's daily reset instead of retrying every run.
    """

    def __init__(self, msg: str, quota: bool = False):
        super().__init__(msg)
        self.quota = quota


class Scorer(Protocol):
    name: str

    def score(self, v: Vacancy) -> ScoreResult: ...


class _Paced:
    """Keeps at least `min_interval` seconds between calls (free tiers have low RPM)."""

    min_interval: float = 0.0
    _last: float = 0.0

    def _pace(self) -> None:
        if self.min_interval and self._last:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()


class GeminiScorer(_Paced):
    """Google Gemini `generateContent` over REST. Free tier: limits are per project, see AI Studio."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(
        self,
        profile: dict,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        thresholds: dict | None = None,
        feedback_examples: list[dict] | None = None,
        min_interval: float = 6.0,
        http: httpx.Client | None = None,
        fallback_models: list[str] | None = None,
        retries: int = 3,
        backoff: float = 5.0,
    ):
        self.api_key = api_key
        self.model = model
        # 503 "high demand" and per-model quotas are common on the free tier:
        # retry with backoff, then try the next model
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        self.retries = max(1, retries)
        self.backoff = backoff
        self.thresholds = thresholds or {"apply": 80, "maybe": 55}
        self.system = build_system_prompt(profile, feedback_examples)
        self.min_interval = min_interval
        self.http = http or httpx.Client(timeout=120)
        self.name = f"gemini:{model}"
        self._schema_mode = True  # flips to plain JSON mode if the API rejects the schema field
        self.requests_made = 0  # every HTTP call counts against the free-tier RPD

    def _body(self, v: Vacancy) -> dict:
        gen: dict = {"responseMimeType": "application/json", "temperature": 0.2}
        if self._schema_mode:
            gen["responseJsonSchema"] = SCORE_SCHEMA
        return {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": [{"role": "user", "parts": [{"text": vacancy_prompt(v) + JSON_REMINDER}]}],
            "generationConfig": gen,
        }

    def _post(self, model: str, v: Vacancy) -> httpx.Response:
        url = self.URL.format(model=model)
        headers = {"x-goog-api-key": self.api_key}
        self.requests_made += 1
        resp = self.http.post(url, headers=headers, json=self._body(v))
        if resp.status_code == 400 and self._schema_mode and "responseJsonSchema" in resp.text:
            log.warning("Gemini rejected responseJsonSchema, falling back to plain JSON mode")
            self._schema_mode = False
            self.requests_made += 1
            resp = self.http.post(url, headers=headers, json=self._body(v))
        return resp

    def score(self, v: Vacancy) -> ScoreResult:
        self._pace()
        problems = []
        quota_models = 0
        for model in self.models:
            for attempt in range(self.retries):
                try:
                    resp = self._post(model, v)
                except httpx.TransportError as e:
                    problems.append(f"{model}: {type(e).__name__}")
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    problems.append(f"{model}: HTTP {resp.status_code}")
                    if attempt + 1 < self.retries:
                        log.info("Gemini %s busy (HTTP %s), retry %d", model, resp.status_code, attempt + 1)
                        time.sleep(self.backoff * (2**attempt))
                    continue
                if resp.status_code == 429:
                    problems.append(f"{model}: quota (429)")
                    quota_models += 1
                    break  # quotas are per model: try the next one
                return self._parse(resp, model)
            if model != self.models[-1]:
                log.warning("Gemini %s unavailable, switching to %s", model, self.models[self.models.index(model) + 1])
        # every model busy or out of quota: stop scoring this run, retry next run
        raise RateLimited(
            "Gemini unavailable: " + "; ".join(problems[-4:]),
            quota=quota_models == len(self.models),
        )

    def _parse(self, resp: httpx.Response, model: str) -> ScoreResult:
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise RuntimeError(f"Gemini blocked the prompt: {data['promptFeedback']}")
        cand = (data.get("candidates") or [{}])[0]
        finish = cand.get("finishReason", "")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if finish not in ("STOP", "") and not text:
            raise RuntimeError(f"Gemini finished with {finish}")
        return result_from_data(_parse_json_text(text), self.thresholds, f"gemini:{model}")


class OllamaScorer(_Paced):
    """A local model through Ollama's /api/chat. Free, offline, needs `ollama pull <model>`."""

    def __init__(
        self,
        profile: dict,
        model: str = "gemma3:4b",
        host: str = "http://localhost:11434",
        thresholds: dict | None = None,
        feedback_examples: list[dict] | None = None,
        num_ctx: int = 8192,
        think: bool | None = None,
        http: httpx.Client | None = None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.thresholds = thresholds or {"apply": 80, "maybe": 55}
        self.system = build_system_prompt(profile, feedback_examples)
        self.num_ctx = num_ctx
        self.think = think
        self.http = http or httpx.Client(timeout=600)  # local models can be slow on a laptop
        self.name = f"ollama:{model}"

    def score(self, v: Vacancy) -> ScoreResult:
        body: dict = {
            "model": self.model,
            "stream": False,
            "format": SCORE_SCHEMA,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": vacancy_prompt(v, max_chars=8000) + JSON_REMINDER},
            ],
        }
        if self.think is not None:
            body["think"] = self.think
        try:
            resp = self.http.post(f"{self.host}/api/chat", json=body)
        except httpx.ConnectError as e:
            raise RuntimeError(
                f"Ollama is not running at {self.host} (start the Ollama app or `ollama serve`)"
            ) from e
        if resp.status_code == 404:
            raise RuntimeError(f"Ollama model {self.model!r} not found – run `ollama pull {self.model}`")
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        content = (resp.json().get("message") or {}).get("content", "")
        return result_from_data(_parse_json_text(content), self.thresholds, self.name)


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
        return result_from_data(json.loads(text), self.thresholds, f"llm:{resp.model}")


class HeuristicScorer:
    """Keyword overlap between the profile and the vacancy. Crude but free and offline."""

    name = "heuristic"

    def __init__(self, profile: dict, thresholds: dict | None = None):
        self.thresholds = thresholds or {"apply": 80, "maybe": 55}
        kw = profile.get("keywords") or {}
        self.strong = [normalize(k) for k in kw.get("strong") or []]
        self.plus = [normalize(k) for k in kw.get("plus") or []]
        self.deal_breakers = [normalize(k) for k in kw.get("deal_breakers") or []]
        self.penalty = [normalize(k) for k in kw.get("penalty") or []]
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
        penalized = [k for k in self.penalty if k and f" {k}" in hay]
        if penalized:
            score -= 15
            red.append("нижчий пріоритет: " + ", ".join(penalized))
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

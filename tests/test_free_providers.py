"""Gemini and Ollama scorers against mocked HTTP, plus provider selection."""

import json
from types import SimpleNamespace

import httpx
import pytest

from jobradar.cli import resolve_provider
from jobradar.db import DB
from jobradar.models import Vacancy
from jobradar.pipeline.rules import Rules
from jobradar.pipeline.scorer import (
    SCORE_SCHEMA,
    GeminiScorer,
    OllamaScorer,
    RateLimited,
    _parse_json_text,
)
from jobradar.pipeline.title_match import TitleMatcher
from jobradar.runner import Runner

RESULT = {"score": 84, "verdict": "apply", "matched": ["позиціонування"], "gaps": [],
          "red_flags": [], "why": "Підходить.", "cover_hook": "Кейс Vyriy."}
VAC = Vacancy("djinni", "1", "https://djinni.co/jobs/1/", "Head of Marketing", company="Acme")


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def gemini_ok(text):
    return httpx.Response(200, json={"candidates": [{
        "content": {"parts": [{"text": text}], "role": "model"}, "finishReason": "STOP"}]})


# ---------------------------------------------------------------- Gemini
def test_gemini_request_and_parse():
    seen = {}

    def handler(req):
        seen["url"], seen["key"] = str(req.url), req.headers.get("x-goog-api-key")
        seen["body"] = json.loads(req.content)
        return gemini_ok(json.dumps(RESULT, ensure_ascii=False))

    s = GeminiScorer({"headline": "CMO"}, "KEY", min_interval=0, http=client(handler))
    r = s.score(VAC)
    assert (r.score, r.verdict, r.scorer) == (84, "apply", "gemini:gemini-3.5-flash-lite")
    assert seen["url"].endswith("/v1beta/models/gemini-3.5-flash-lite:generateContent")
    assert seen["key"] == "KEY"
    gen = seen["body"]["generationConfig"]
    assert gen["responseMimeType"] == "application/json" and gen["responseJsonSchema"] == SCORE_SCHEMA
    assert "CMO" in seen["body"]["systemInstruction"]["parts"][0]["text"]
    assert "Head of Marketing" in seen["body"]["contents"][0]["parts"][0]["text"]


def test_gemini_falls_back_to_plain_json_mode():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append("responseJsonSchema" in body["generationConfig"])
        if calls[-1]:
            return httpx.Response(400, json={"error": {"message": "Unknown name \"responseJsonSchema\""}})
        return gemini_ok("```json\n" + json.dumps(RESULT) + "\n```")

    s = GeminiScorer({}, "KEY", min_interval=0, http=client(handler))
    assert s.score(VAC).score == 84
    assert calls == [True, False]
    s.score(VAC)
    assert calls == [True, False, False]  # remembers the fallback


def test_gemini_rate_limit_and_errors():
    s = GeminiScorer({}, "KEY", min_interval=0, http=client(lambda r: httpx.Response(429, text="quota")))
    with pytest.raises(RateLimited):
        s.score(VAC)
    s = GeminiScorer({}, "KEY", min_interval=0, http=client(
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})))
    with pytest.raises(RuntimeError, match="blocked"):
        s.score(VAC)
    s = GeminiScorer({}, "KEY", min_interval=0, http=client(
        lambda r: httpx.Response(403, text="API key not valid")))
    with pytest.raises(RuntimeError, match="403"):
        s.score(VAC)


def test_gemini_ignores_thought_parts():
    resp = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "thinking about it", "thought": True}, {"text": json.dumps(RESULT)}]}}]}
    s = GeminiScorer({}, "KEY", min_interval=0, http=client(lambda r: httpx.Response(200, json=resp)))
    assert s.score(VAC).score == 84


# ---------------------------------------------------------------- Ollama
def test_ollama_request_and_parse():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(RESULT)},
                                          "done_reason": "stop"})

    s = OllamaScorer({"headline": "CMO"}, http=client(handler))
    r = s.score(VAC)
    assert (r.score, r.scorer) == (84, "ollama:gemma3:4b")
    b = seen["body"]
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert b["stream"] is False and b["format"] == SCORE_SCHEMA and "think" not in b
    assert b["messages"][0]["role"] == "system"


def test_ollama_helpful_errors():
    s = OllamaScorer({}, http=client(lambda r: httpx.Response(404, json={"error": "model not found"})))
    with pytest.raises(RuntimeError, match="ollama pull gemma3:4b"):
        s.score(VAC)

    def refuse(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(RuntimeError, match="not running"):
        OllamaScorer({}, http=client(refuse)).score(VAC)


def test_parse_json_text_tolerates_wrapping():
    assert _parse_json_text('Here you go: {"score": 1}') == {"score": 1}
    with pytest.raises(ValueError):
        _parse_json_text("no json")


# ---------------------------------------------------------------- provider selection / runner
def settings(provider="auto", gemini=None, anthropic=False):
    return SimpleNamespace(
        gemini_api_key=gemini, anthropic_key_present=anthropic,
        section=lambda name: {"provider": provider} if name == "scoring" else {},
    )


def test_resolve_provider():
    assert resolve_provider(settings()) == "heuristic"
    assert resolve_provider(settings(gemini="k", anthropic=True)) == "gemini"  # free first
    assert resolve_provider(settings(anthropic=True)) == "claude"
    assert resolve_provider(settings("ollama", gemini="k")) == "ollama"
    assert resolve_provider(settings(gemini="k"), no_llm=True) == "heuristic"


def test_runner_stops_on_quota_and_keeps_vacancies_pending():
    class Quota:
        name = "quota"
        calls = 0

        def score(self, v):
            Quota.calls += 1
            raise RateLimited("429")

    class One:
        name = "one"

        def collect(self):
            return [Vacancy("djinni", str(i), f"u{i}", "Head of Marketing") for i in range(3)]

        def enrich(self, v):
            return v

    db = DB(":memory:")
    rep = Runner(db, [One()], TitleMatcher(), Rules(), Quota(), printer=lambda *_: None).run()
    assert rep.rate_limited and Quota.calls == 1 and rep.errors == 0
    assert len(db.by_status("new")) == 3  # all wait for the next run

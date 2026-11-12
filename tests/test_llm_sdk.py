"""Runs LLMScorer through the real Anthropic SDK with a mocked HTTP transport.

Checks that the SDK accepts our parameter combination and serialises it the way the
Messages API expects, without spending tokens.
"""

import json

import anthropic
import httpx2

from jobradar.models import Vacancy
from jobradar.pipeline.scorer import LLMScorer


def test_llm_scorer_through_real_sdk():
    captured = {}
    result = {
        "score": 83, "verdict": "apply", "matched": ["позиціонування"], "gaps": [],
        "red_flags": [], "why": "Сильний збіг.", "cover_hook": "Кейс OLX Safe Deal.",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_test", "type": "message", "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1200, "output_tokens": 150},
            },
        )

    client = anthropic.Anthropic(
        api_key="sk-ant-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    scorer = LLMScorer({"headline": "CMO"}, client=client)
    r = scorer.score(Vacancy("djinni", "1", "https://djinni.co/jobs/1/", "Head of Marketing"))

    assert (r.score, r.verdict) == (83, "apply")
    body = captured["body"]
    assert captured["url"].split("?")[0].endswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert "Head of Marketing" in body["messages"][0]["content"]

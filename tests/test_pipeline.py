import json
from datetime import datetime, timezone
from types import SimpleNamespace

from jobradar.collectors.base import Collector, CollectorError
from jobradar.db import DB
from jobradar.models import ScoreResult, Vacancy
from jobradar.notify.telegram import format_digest, format_vacancy, vacancy_keyboard
from jobradar.pipeline.dedup import dedup_key
from jobradar.pipeline.rules import Rules
from jobradar.pipeline.scorer import (
    SCORE_SCHEMA,
    HeuristicScorer,
    LLMScorer,
    build_system_prompt,
    vacancy_prompt,
)
from jobradar.pipeline.title_match import TitleMatcher
from jobradar.runner import Runner, process_feedback

PROFILE = {
    "headline": "CMO",
    "target_titles": ["Head of Marketing", "CMO"],
    "keywords": {
        "strong": ["brand strategy", "positioning"],
        "plus": ["team"],
        "deal_breakers": ["igaming"],
    },
}


def V(**kw):
    base = dict(source="djinni", source_id="1", url="https://djinni.co/jobs/1-x/", title="Head of Marketing")
    base.update(kw)
    return Vacancy(**base)


# ---------------------------------------------------------------- rules / dedup
def test_rules_keyword_salary_age():
    r = Rules.from_config({"exclude_keywords": ["igaming"], "min_salary_usd": 3000, "max_age_days": 10})
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert r.check(V(description="We are an iGaming company"), now)[0] is False
    assert r.check(V(salary_to=2000, currency="USD"), now)[0] is False
    assert r.check(V(salary_to=150000, currency="UAH"), now)[0] is True  # ~3600 USD
    assert r.check(V(), now)[0] is True  # unknown salary passes
    assert r.check(V(posted_at="2026-10-01T10:00:00+00:00"), now)[0] is False
    assert r.check(V(posted_at="2026-11-10T10:00:00+00:00"), now)[0] is True


def test_dedup_key_cross_site():
    a = V(company="ТОВ Смачна Кава", title="Senior Head of Marketing (Remote)")
    b = V(source="workua", source_id="9", company="Смачна Кава", title="Head of Marketing")
    assert dedup_key(a) == dedup_key(b) != ""
    assert dedup_key(V(company="")) == ""


# ---------------------------------------------------------------- scorers
def test_heuristic_scorer():
    s = HeuristicScorer(PROFILE)
    good = s.score(V(description="Own brand strategy and positioning, lead a team", salary_to=5000))
    assert good.verdict in ("apply", "maybe") and good.score >= 55
    assert "brand strategy" in good.matched
    bad = s.score(V(description="Leading iGaming brand"))
    assert bad.verdict == "skip"


def test_prompts_contain_profile_and_vacancy():
    sys_prompt = build_system_prompt(PROFILE, [{"title": "CMO", "company": "X", "score": 85, "feedback": "skipped"}])
    assert "brand strategy" in sys_prompt and "SKIPPED" in sys_prompt
    up = vacancy_prompt(V(company="Acme", description="desc", description_is_snippet=True))
    assert "Acme" in up and "short e-mail snippet" in up and "not specified" in up


class _FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.kwargs = payload, stop_reason, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            model=kwargs["model"],
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


def _fake_client(payload, stop_reason="end_turn"):
    msgs = _FakeMessages(payload, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_llm_scorer_request_and_parsing():
    payload = {"score": 86, "verdict": "apply", "matched": ["бренд-стратегія"], "gaps": [],
               "red_flags": ["зарплата не вказана"], "why": "Підходить.", "cover_hook": "Кейс McDonald's."}
    client, msgs = _fake_client(payload)
    s = LLMScorer(PROFILE, client=client)
    r = s.score(V())
    assert (r.score, r.verdict, r.scorer) == (86, "apply", "llm:claude-opus-5-5")
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"]["schema"] == SCORE_SCHEMA
    assert k["output_config"]["effort"] == "medium"
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in k and "temperature" not in k


def test_llm_scorer_verdict_follows_thresholds_but_skip_wins():
    client, _ = _fake_client({"score": 60, "verdict": "apply", "matched": [], "gaps": [],
                              "red_flags": [], "why": "", "cover_hook": ""})
    assert LLMScorer(PROFILE, client=client).score(V()).verdict == "maybe"
    client, _ = _fake_client({"score": 90, "verdict": "skip", "matched": [], "gaps": [],
                              "red_flags": ["gambling"], "why": "", "cover_hook": ""})
    assert LLMScorer(PROFILE, client=client).score(V()).verdict == "skip"


def test_llm_scorer_refusal_raises():
    client, _ = _fake_client({}, stop_reason="refusal")
    try:
        LLMScorer(PROFILE, client=client).score(V())
    except RuntimeError as e:
        assert "declined" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------- telegram formatting
def test_format_vacancy_escapes_html():
    r = ScoreResult(score=88, verdict="apply", matched=["a<b>"], why="x & y", cover_hook="hook")
    text = format_vacancy(V(title="Head <of> Marketing", company="A&B", salary_from=3000, currency="USD"), r)
    assert "Head &lt;of&gt; Marketing" in text and "A&amp;B" in text and "88/100" in text
    kb = vacancy_keyboard(42, "https://x")
    assert kb["inline_keyboard"][1][0]["callback_data"] == "fb:applied:42"
    digest = format_digest([(1, V(), ScoreResult(score=60, verdict="maybe", why="w"))])
    assert "Варто глянути (1)" in digest


# ---------------------------------------------------------------- runner end-to-end
class _ListCollector(Collector):
    name = "fake"

    def __init__(self, items, fail=False):
        self.items, self.fail, self.enriched = items, fail, []

    def collect(self):
        if self.fail:
            raise CollectorError("blocked")
        return list(self.items)

    def enrich(self, v):
        self.enriched.append(v.source_id)
        v.company = v.company or "Acme"
        return v


class _FixedScorer:
    name = "fixed"

    def __init__(self, by_title):
        self.by_title, self.calls = by_title, 0

    def score(self, v):
        self.calls += 1
        sc = self.by_title.get(v.title, 30)
        verdict = "apply" if sc >= 80 else "maybe" if sc >= 55 else "skip"
        return ScoreResult(score=sc, verdict=verdict, why="ok", scorer="fixed")


class _FakeTelegram:
    def __init__(self, updates=None):
        self.sent, self.updates, self.answered, self.marked = [], updates or [], [], []

    def send_vacancy(self, row_id, v, r):
        self.sent.append(("vacancy", row_id, v.title))
        return 100 + row_id

    def send(self, text, reply_markup=None):
        self.sent.append(("text", text))
        return 999

    def get_updates(self, offset=None):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def answer_callback(self, cid, text):
        self.answered.append(cid)

    def mark_message(self, mid, url, label):
        self.marked.append(mid)


def _runner(db, collectors, scorer, tg=None, dry=False):
    return Runner(
        db=db, collectors=collectors, matcher=TitleMatcher(exclude=["smm"]),
        rules=Rules(exclude_keywords=["igaming"]), scorer=scorer, telegram=tg,
        notify_cfg={"min_score": 55, "send_maybe": True}, dry_run=dry, printer=lambda *_: None,
    )


def test_runner_end_to_end():
    db = DB(":memory:")
    items = [
        V(source_id="1", title="Head of Marketing"),
        V(source_id="2", title="Brand Director"),
        V(source_id="3", title="Head of SMM"),  # title filter
        V(source_id="4", title="Marketing Director", description="Top iGaming brand"),  # rule
        V(source_id="5", title="Junior Copywriter"),  # title filter
        V(source_id="6", title="CMO"),  # low score -> not sent
    ]
    col = _ListCollector(items)
    scorer = _FixedScorer({"Head of Marketing": 90, "Brand Director": 65, "CMO": 40})
    tg = _FakeTelegram()
    rep = _runner(db, [col, _ListCollector([], fail=True)], scorer, tg).run()

    assert rep.new == 6 and rep.title_rejected == 2 and rep.rule_rejected == 1
    assert rep.scored == 3 and scorer.calls == 3
    assert sorted(col.enriched) == ["1", "2", "4", "6"]  # details only for title matches
    assert rep.sent_apply == 1 and rep.sent_maybe == 1
    assert tg.sent[0] == ("vacancy", 1, "Head of Marketing")
    assert "Brand Director" in tg.sent[1][1]
    assert "fake" in rep.collector_errors  # the failing collector is reported…
    assert any("джерела не відповіли" in s[1] for s in tg.sent if s[0] == "text")  # …and alerted

    # second run: nothing new, nothing re-scored or re-sent
    tg2 = _FakeTelegram()
    rep2 = _runner(db, [_ListCollector(items)], scorer, tg2).run()
    assert rep2.new == 0 and scorer.calls == 3 and tg2.sent == []


def test_runner_cross_site_duplicate_not_rescored():
    db = DB(":memory:")
    scorer = _FixedScorer({"Head of Marketing": 90})
    _runner(db, [_ListCollector([V(source_id="1", company="Acme")])], scorer, _FakeTelegram()).run()
    dup = V(source="workua", source_id="777", url="https://www.work.ua/jobs/777/", company="Acme")
    rep = _runner(db, [_ListCollector([dup])], scorer, _FakeTelegram()).run()
    assert rep.duplicates == 1 and scorer.calls == 1


def test_runner_retries_failed_scoring():
    db = DB(":memory:")

    class Flaky:
        name = "flaky"
        calls = 0

        def score(self, v):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("API down")
            return ScoreResult(score=85, verdict="apply", scorer="flaky")

    tg = _FakeTelegram()
    rep = _runner(db, [_ListCollector([V()])], Flaky(), tg).run()
    assert rep.errors == 1 and tg.sent == []
    rep = _runner(db, [_ListCollector([])], Flaky(), tg).run()
    assert rep.scored == 1 and rep.sent_apply == 1


def test_dry_run_does_not_consume_notifications():
    db = DB(":memory:")
    scorer = _FixedScorer({"Head of Marketing": 90})
    out = []
    r = _runner(db, [_ListCollector([V()])], scorer, None, dry=True)
    r.print = out.append
    r.run()
    assert any("Head of Marketing" in o for o in out)
    assert db.by_status("scored")  # still waiting for a real run


def test_feedback_buttons():
    db = DB(":memory:")
    scorer = _FixedScorer({"Head of Marketing": 90})
    tg = _FakeTelegram()
    _runner(db, [_ListCollector([V()])], scorer, tg).run()
    tg.updates = [
        {"update_id": 10, "callback_query": {"id": "cb1", "data": "fb:applied:1"}},
        {"update_id": 11, "message": {"chat": {"id": 5}, "text": "hi"}},
    ]
    assert process_feedback(db, tg) == 1
    assert db.get(1)["feedback"] == "applied"
    assert tg.answered == ["cb1"] and tg.marked == [101]
    assert db.get_meta("tg_offset") == "12"
    assert process_feedback(db, tg) == 0  # offset respected
    assert db.feedback_examples()[0]["feedback"] == "applied"

"""Daily Gemini budget, quota pause and the run lock."""

from datetime import datetime, timezone

from jobradar.budget import DailyBudget
from jobradar.cli import _acquire_run_lock
from jobradar.db import DB
from jobradar.models import ScoreResult, Vacancy
from jobradar.pipeline.rules import Rules
from jobradar.pipeline.scorer import RateLimited
from jobradar.pipeline.title_match import TitleMatcher
from jobradar.runner import Runner


class Clock:
    def __init__(self, iso):
        self.t = datetime.fromisoformat(iso)

    def __call__(self):
        return self.t


def test_budget_counts_per_pacific_day():
    db, clock = DB(":memory:"), Clock("2026-11-13T12:00:00+00:00")  # 04:00 Pacific
    b = DailyBudget(db, 10, now=clock)
    b.add(7)
    assert b.used() == 7 and b.remaining() == 3
    clock.t = datetime.fromisoformat("2026-11-14T07:59:00+00:00")  # 23:59 Pacific, same day
    assert b.used() == 7
    clock.t = datetime.fromisoformat("2026-11-14T08:01:00+00:00")  # 00:01 Pacific, new day
    assert b.used() == 0 and b.remaining() == 10


def test_pause_until_reset():
    db, clock = DB(":memory:"), Clock("2026-11-13T12:00:00+00:00")
    b = DailyBudget(db, 10, now=clock)
    until = b.pause_until_reset()
    assert until == datetime(2026, 11, 14, 8, 5, tzinfo=timezone.utc)  # 00:05 PST
    assert b.paused_until() == until
    clock.t = datetime.fromisoformat("2026-11-14T08:06:00+00:00")
    assert b.paused_until() is None


class _Items:
    name = "fake"

    def __init__(self, n):
        self.n = n

    def collect(self):
        return [Vacancy("djinni", str(i), f"https://x/{i}", "Head of Marketing") for i in range(self.n)]

    def enrich(self, v):
        return v


class _CountingScorer:
    """Pretends each score costs 2 HTTP requests (e.g. one 503 retry)."""

    name = "gemini:test"

    def __init__(self, fail_quota_after=None):
        self.requests_made, self.calls, self.fail_after = 0, 0, fail_quota_after

    def score(self, v):
        self.calls += 1
        self.requests_made += 2
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RateLimited("429 everywhere", quota=True)
        return ScoreResult(score=90, verdict="apply", scorer=self.name)


class _Tg:
    def __init__(self):
        self.sent = []

    def send_vacancy(self, row_id, v, r):
        self.sent.append(row_id)
        return row_id

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return 1

    def get_updates(self, offset=None):
        return []


def _run(db, scorer, budget, n=5, tg=None):
    return Runner(db, [_Items(n)], TitleMatcher(), Rules(), scorer, telegram=tg,
                  budget=budget, printer=lambda *_: None).run()


def test_runner_respects_daily_budget():
    db = DB(":memory:")
    budget = DailyBudget(db, 6)
    scorer = _CountingScorer()
    rep = _run(db, scorer, budget)
    assert scorer.calls == 3 and rep.llm_requests == 6 and rep.llm_paused
    assert budget.used() == 6
    assert len(db.by_status("new")) == 2  # wait for tomorrow
    rep2 = _run(db, scorer, budget)
    assert scorer.calls == 3  # still nothing today


def test_runner_pauses_on_quota_and_notifies_once():
    db = DB(":memory:")
    budget = DailyBudget(db, 100)
    scorer, tg = _CountingScorer(fail_quota_after=1), _Tg()
    rep = _run(db, scorer, budget, tg=tg)
    assert rep.llm_paused and rep.scored == 1
    assert any("ліміт Gemini" in str(s) for s in tg.sent)
    assert budget.paused_until() is not None
    calls = scorer.calls
    rep2 = _run(db, scorer, budget, tg=tg)
    assert scorer.calls == calls and rep2.llm_paused  # no requests while paused
    assert sum("ліміт Gemini" in str(s) for s in tg.sent) == 1


def test_run_lock(tmp_path):
    first = _acquire_run_lock(tmp_path / "db.sqlite3")
    assert first is not None
    assert _acquire_run_lock(tmp_path / "db.sqlite3") is None
    first.close()
    again = _acquire_run_lock(tmp_path / "db.sqlite3")
    assert again is not None
    again.close()

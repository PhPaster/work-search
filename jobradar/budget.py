"""Daily LLM request budget, so scheduled runs never blow through a free-tier quota.

Gemini's free-tier "requests per day" reset at midnight Pacific time, so the budget day is
counted in America/Los_Angeles. Every HTTP request counts (also retries on 503).
State lives in the `meta` table: llm_day, llm_used, llm_paused_until.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB

RESET_TZ = "America/Los_Angeles"


class DailyBudget:
    def __init__(self, db: DB, limit: int | None, tz: str = RESET_TZ, now=None):
        self.db = db
        self.limit = limit
        self.tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _today(self) -> str:
        return self._now().astimezone(self.tz).date().isoformat()

    def used(self) -> int:
        if self.db.get_meta("llm_day") != self._today():
            return 0
        return int(self.db.get_meta("llm_used", "0") or 0)

    def remaining(self) -> int:
        if self.limit is None:
            return 10**9
        return max(0, self.limit - self.used())

    def add(self, n: int) -> None:
        if n <= 0:
            return
        used = self.used() + n
        self.db.set_meta("llm_day", self._today())
        self.db.set_meta("llm_used", str(used))

    def next_reset(self) -> datetime:
        local = self._now().astimezone(self.tz)
        midnight = datetime.combine(local.date() + timedelta(days=1), time(0, 5), tzinfo=self.tz)
        return midnight.astimezone(timezone.utc)

    def pause_until_reset(self) -> datetime:
        until = self.next_reset()
        self.db.set_meta("llm_paused_until", until.isoformat())
        return until

    def paused_until(self) -> datetime | None:
        raw = self.db.get_meta("llm_paused_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        return until if self._now() < until else None

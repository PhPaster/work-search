"""Orchestrates one pipeline run: collect -> filter -> enrich -> rules -> dedup -> score -> notify."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .collectors.base import Collector, CollectorError
from .db import DB, row_to_vacancy
from .models import ScoreResult, Vacancy
from .notify.telegram import Telegram, TelegramError, format_digest, format_vacancy
from .pipeline.dedup import dedup_key
from .pipeline.rules import Rules
from .pipeline.scorer import RateLimited, Scorer
from .pipeline.title_match import TitleMatcher

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class RunReport:
    collected: dict[str, int] = field(default_factory=dict)
    collector_errors: dict[str, str] = field(default_factory=dict)
    new: int = 0
    title_rejected: int = 0
    rule_rejected: int = 0
    duplicates: int = 0
    scored: int = 0
    errors: int = 0
    rate_limited: bool = False
    sent_apply: int = 0
    sent_maybe: int = 0
    feedback: int = 0

    def summary(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def process_feedback(db: DB, tg: Telegram) -> int:
    """Read button presses since the last run and store them as feedback."""
    offset = db.get_meta("tg_offset")
    updates = tg.get_updates(int(offset) if offset else None)
    n = 0
    for u in updates:
        db.set_meta("tg_offset", str(u["update_id"] + 1))
        cq = u.get("callback_query")
        if not cq or not str(cq.get("data", "")).startswith("fb:"):
            continue
        try:
            _, fb, row_id = cq["data"].split(":")
            row = db.get(int(row_id))
        except ValueError:
            continue
        if row is None:
            continue
        db.set_feedback(int(row_id), fb)
        n += 1
        label = "✅ Відгукнувся" if fb == "applied" else "⏭ Пропущено"
        tg.answer_callback(cq["id"], label)
        if row["tg_message_id"]:
            tg.mark_message(row["tg_message_id"], row["url"], label)
    return n


class Runner:
    def __init__(
        self,
        db: DB,
        collectors: list[Collector],
        matcher: TitleMatcher,
        rules: Rules,
        scorer: Scorer,
        telegram: Telegram | None = None,
        notify_cfg: dict | None = None,
        max_scores_per_run: int = 40,
        dry_run: bool = False,
        printer=print,
    ):
        self.db = db
        self.collectors = collectors
        self.by_name = {c.name: c for c in collectors}
        self.matcher = matcher
        self.rules = rules
        self.scorer = scorer
        self.tg = telegram
        self.notify_cfg = notify_cfg or {}
        self.max_scores = max_scores_per_run
        self.dry_run = dry_run
        self.print = printer

    # ------------------------------------------------------------------ stages
    def _collect(self, report: RunReport) -> list[tuple[Collector, Vacancy]]:
        out = []
        for c in self.collectors:
            try:
                items = c.collect()
            except CollectorError as e:
                log.error("%s", e)
                report.collector_errors[c.name] = str(e)
                continue
            except Exception as e:  # never let one source kill the run
                log.exception("collector %s crashed", c.name)
                report.collector_errors[c.name] = f"{type(e).__name__}: {e}"
                continue
            report.collected[c.name] = len(items)
            out.extend((c, v) for v in items)
        return out

    def _ingest(self, c: Collector, v: Vacancy, report: RunReport) -> int | None:
        """Store a new vacancy; return row id if it should be scored."""
        if self.db.exists(*v.key):
            return None
        report.new += 1
        ok, why = self.matcher.match(v.title)
        if not ok:
            self.db.insert(v, status="title_rejected", reason=why)
            report.title_rejected += 1
            return None
        v = c.enrich(v)
        key = dedup_key(v)
        ok, why = self.rules.check(v)
        if not ok:
            self.db.insert(v, status="rule_rejected", reason=why, dedup_key=key)
            report.rule_rejected += 1
            return None
        row_id = self.db.insert(v, status="new", dedup_key=key)
        dup = self.db.find_duplicate(key, row_id)
        if dup is not None:
            self.db.set_status(row_id, "duplicate", f"same as #{dup['id']} ({dup['source']})")
            report.duplicates += 1
            return None
        return row_id

    def _score_pending(self, report: RunReport) -> None:
        pending = [r for r in self.db.by_status("new", "error")]
        budget = self.max_scores
        for row in pending:
            attempts = 0
            if row["status"] == "error" and row["reason"].startswith("attempt "):
                attempts = int(row["reason"].split(":")[0].split()[1])
                if attempts >= MAX_ATTEMPTS:
                    continue
            if budget <= 0:
                log.info("score budget exhausted, %d left for next run", len(pending))
                break
            budget -= 1
            v = row_to_vacancy(row)
            try:
                result = self.scorer.score(v)
            except RateLimited as e:
                # free-tier quota: not the vacancy's fault, keep it pending for the next run
                log.warning("%s – stopping scoring for this run", e)
                report.rate_limited = True
                break
            except Exception as e:
                log.error("scoring #%s failed: %s", row["id"], e)
                self.db.set_status(row["id"], "error", f"attempt {attempts + 1}: {e}"[:500])
                report.errors += 1
                continue
            self.db.save_score(row["id"], result)
            report.scored += 1

    def _notify(self, report: RunReport) -> None:
        min_score = int(self.notify_cfg.get("min_score", 55))
        send_maybe = bool(self.notify_cfg.get("send_maybe", True))
        apply_items, maybe_items = [], []
        for row in self.db.by_status("scored"):
            r = ScoreResult.from_dict(json.loads(row["analysis"]))
            v = row_to_vacancy(row)
            if r.verdict == "apply" and r.score >= min_score:
                apply_items.append((row["id"], v, r))
            elif r.verdict == "maybe" and r.score >= min_score and send_maybe:
                maybe_items.append((row["id"], v, r))
            else:
                # below the bar: mark as handled so it isn't reconsidered every run
                self.db.mark_sent(row["id"], None)
        apply_items.sort(key=lambda x: -x[2].score)
        maybe_items.sort(key=lambda x: -x[2].score)

        if self.dry_run or self.tg is None:
            for row_id, v, r in apply_items:
                self.print("\n" + format_vacancy(v, r) + f"\n{v.url}")
            if maybe_items:
                self.print("\n" + format_digest(maybe_items))
            if not self.dry_run:  # no Telegram configured: treat console as the channel
                for row_id, _, _ in apply_items + maybe_items:
                    self.db.mark_sent(row_id, None)
            report.sent_apply, report.sent_maybe = len(apply_items), len(maybe_items)
            return

        for row_id, v, r in apply_items:
            try:
                mid = self.tg.send_vacancy(row_id, v, r)
            except TelegramError as e:
                log.error("telegram send failed for #%s: %s", row_id, e)
                continue
            self.db.mark_sent(row_id, mid)
            report.sent_apply += 1
            time.sleep(0.5)
        if maybe_items:
            # Telegram messages max out at 4096 chars – send the digest in chunks of 12
            for i in range(0, len(maybe_items), 12):
                chunk = maybe_items[i : i + 12]
                try:
                    mid = self.tg.send(format_digest(chunk))
                except TelegramError as e:
                    log.error("telegram digest failed: %s", e)
                    break
                for row_id, _, _ in chunk:
                    self.db.mark_sent(row_id, mid)
                report.sent_maybe += len(chunk)

    def _alert_broken_collectors(self, report: RunReport) -> None:
        if not report.collector_errors or self.tg is None or self.dry_run:
            return
        last = float(self.db.get_meta("last_collector_alert", "0") or 0)
        if time.time() - last < 24 * 3600:
            return
        text = "⚠️ <b>jobradar</b>: джерела не відповіли\n" + "\n".join(
            f"• {k}: {v[:300]}" for k, v in report.collector_errors.items()
        )
        try:
            self.tg.send(text)
            self.db.set_meta("last_collector_alert", str(time.time()))
        except TelegramError as e:
            log.error("cannot send collector alert: %s", e)

    # ------------------------------------------------------------------ public
    def run(self) -> RunReport:
        report = RunReport()
        if self.tg is not None and not self.dry_run:
            try:
                report.feedback = process_feedback(self.db, self.tg)
            except TelegramError as e:
                log.error("feedback polling failed: %s", e)
        for c, v in self._collect(report):
            try:
                self._ingest(c, v, report)
            except Exception:
                log.exception("ingest failed for %s", v.url)
        self._score_pending(report)
        self._notify(report)
        self._alert_broken_collectors(report)
        self.db.set_meta("last_run", time.strftime("%Y-%m-%dT%H:%M:%S"))
        return report

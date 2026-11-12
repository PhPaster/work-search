"""Command line entry point: `jobradar <command>` (or `python -m jobradar`)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .collectors.base import Collector
from .collectors.djinni import DjinniCollector
from .collectors.email_alerts import EmailAlertsCollector
from .config import Settings, load_settings
from .db import DB, row_to_vacancy
from .http import PoliteClient
from .notify.telegram import Telegram, format_vacancy
from .pipeline.rules import Rules
from .pipeline.scorer import DEFAULT_MODEL, HeuristicScorer, LLMScorer
from .pipeline.title_match import TitleMatcher
from .runner import Runner, process_feedback

log = logging.getLogger("jobradar")


def build_collectors(s: Settings, only: set[str] | None, http: PoliteClient) -> list[Collector]:
    out: list[Collector] = []
    dj = s.section("djinni")
    if dj.get("enabled", True) and (not only or "djinni" in only):
        out.append(
            DjinniCollector(http, dj.get("searches") or [], fetch_details=dj.get("fetch_details", True))
        )
    em = s.section("email_alerts")
    if em.get("enabled", False) and (not only or "email" in only):
        if not (s.imap_host and s.imap_user and s.imap_password):
            log.warning("email_alerts enabled but IMAP_HOST/IMAP_USER/IMAP_PASSWORD are not set")
        else:
            out.append(
                EmailAlertsCollector(
                    s.imap_host, s.imap_user, s.imap_password,
                    sites=em.get("sites") or ["workua", "robotaua"],
                    folder=em.get("folder", "INBOX"),
                    days_back=int(em.get("days_back", 3)),
                )
            )
    return out


def build_scorer(s: Settings, db: DB, no_llm: bool):
    sc = s.section("scoring")
    thresholds = sc.get("thresholds") or {"apply": 80, "maybe": 55}
    if no_llm or not s.anthropic_key_present:
        if not no_llm:
            log.warning("ANTHROPIC_API_KEY not set – using the heuristic scorer")
        return HeuristicScorer(s.profile, thresholds)
    examples = [dict(r) for r in db.feedback_examples(int(sc.get("feedback_examples", 6)))]
    return LLMScorer(
        s.profile,
        model=sc.get("model", DEFAULT_MODEL),
        effort=sc.get("effort", "medium"),
        thresholds=thresholds,
        feedback_examples=examples,
    )


def build_telegram(s: Settings, required: bool = False) -> Telegram | None:
    if not s.telegram_token:
        if required:
            sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
        return None
    return Telegram(s.telegram_token, s.telegram_chat_id)


# ---------------------------------------------------------------- commands
def cmd_run(s: Settings, db: DB, a) -> int:
    http = PoliteClient(min_interval=float(s.section("http").get("min_interval_sec", 2.0)))
    only = set(a.sources.split(",")) if a.sources else None
    tg = None if (a.dry_run or a.no_telegram) else build_telegram(s)
    if tg is not None and not s.telegram_chat_id:
        sys.exit("TELEGRAM_CHAT_ID is not set – run `jobradar tg-setup` first")
    runner = Runner(
        db=db,
        collectors=build_collectors(s, only, http),
        matcher=TitleMatcher.from_config(s.section("title_filter")),
        rules=Rules.from_config(s.section("rules")),
        scorer=build_scorer(s, db, a.no_llm),
        telegram=tg,
        notify_cfg=s.section("notify"),
        max_scores_per_run=int(s.section("scoring").get("max_per_run", 40)),
        dry_run=a.dry_run,
    )
    report = runner.run()
    http.close()
    print("\n" + report.summary())
    return 1 if report.collector_errors and not report.collected else 0


def cmd_feedback(s: Settings, db: DB, a) -> int:
    tg = build_telegram(s, required=True)
    print(f"processed {process_feedback(db, tg)} button presses")
    return 0


def cmd_tg_setup(s: Settings, db: DB, a) -> int:
    tg = build_telegram(s, required=True)
    updates = tg.get_updates()
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("No messages yet. Send any message to your bot (or add it to a group and write there), then rerun.")
        return 1
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    # {name}")
    return 0


def cmd_tg_test(s: Settings, db: DB, a) -> int:
    tg = build_telegram(s, required=True)
    mid = tg.send("✅ <b>jobradar</b> підключено. Сюди приходитимуть вакансії.")
    print(f"sent message {mid}")
    return 0


def cmd_stats(s: Settings, db: DB, a) -> int:
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    print("last_run:", db.get_meta("last_run"))
    return 0


def cmd_show(s: Settings, db: DB, a) -> int:
    rows = db.by_status(*a.status.split(","))[-a.limit :]
    for r in rows:
        score = "" if r["score"] is None else f"{r['score']:>3} {r['verdict']:<5}"
        print(f"#{r['id']:<5} {r['source']:<8} {score:<10} {r['title'][:70]:<70} {r['company'][:25]:<25} {r['reason'][:50]}")
    return 0


def cmd_rescore(s: Settings, db: DB, a) -> int:
    row = db.get(a.id)
    if row is None:
        sys.exit(f"no vacancy #{a.id}")
    v = row_to_vacancy(row)
    r = build_scorer(s, db, a.no_llm).score(v)
    print(format_vacancy(v, r))
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jobradar", description=__doc__)
    p.add_argument("--config", type=Path, help="config.yaml path")
    p.add_argument("--profile", type=Path, help="profile.yaml path")
    p.add_argument("--db", type=Path, help="SQLite path (default data/jobradar.sqlite3)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="collect, score and notify")
    r.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    r.add_argument("--no-llm", action="store_true", help="use the heuristic scorer")
    r.add_argument("--no-telegram", action="store_true", help="print results, mark them as sent")
    r.add_argument("--sources", help="comma list: djinni,email")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("feedback", help="read Telegram button presses").set_defaults(fn=cmd_feedback)
    sub.add_parser("tg-setup", help="show chat ids that wrote to the bot").set_defaults(fn=cmd_tg_setup)
    sub.add_parser("tg-test", help="send a test message").set_defaults(fn=cmd_tg_test)
    sub.add_parser("stats", help="counts by source/status").set_defaults(fn=cmd_stats)

    sh = sub.add_parser("show", help="list vacancies by status")
    sh.add_argument("--status", default="sent,scored")
    sh.add_argument("--limit", type=int, default=30)
    sh.set_defaults(fn=cmd_show)

    rs = sub.add_parser("rescore", help="score one stored vacancy again and print it")
    rs.add_argument("id", type=int)
    rs.add_argument("--no-llm", action="store_true")
    rs.set_defaults(fn=cmd_rescore)

    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = load_settings(a.config, a.profile)
    db = DB(a.db or s.db_path)
    try:
        return a.fn(s, db, a)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

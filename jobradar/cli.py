"""Command line entry point: `jobradar <command>` (or `python -m jobradar`)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .budget import DailyBudget
from .collectors.base import Collector
from .collectors.djinni import DjinniCollector
from .collectors.email_alerts import EmailAlertsCollector
from .config import Settings, load_settings
from .db import DB, row_to_vacancy
from .http import PoliteClient
from .notify.telegram import Telegram, format_vacancy
from .pipeline.rules import Rules
from .models import Vacancy
from .pipeline.scorer import (
    DEFAULT_MODEL,
    GeminiScorer,
    HeuristicScorer,
    LLMScorer,
    OllamaScorer,
)
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


def resolve_provider(s: Settings, no_llm: bool = False) -> str:
    """auto: Gemini key -> Gemini (free tier); Anthropic key -> Claude; otherwise heuristic."""
    if no_llm:
        return "heuristic"
    provider = str(s.section("scoring").get("provider", "auto")).lower()
    if provider != "auto":
        return provider
    if s.gemini_api_key:
        return "gemini"
    if s.anthropic_key_present:
        return "claude"
    return "heuristic"


def build_scorer(s: Settings, db: DB, no_llm: bool):
    sc = s.section("scoring")
    thresholds = sc.get("thresholds") or {"apply": 80, "maybe": 55}
    provider = resolve_provider(s, no_llm)
    examples = [dict(r) for r in db.feedback_examples(int(sc.get("feedback_examples", 6)))]

    if provider == "gemini":
        if not s.gemini_api_key:
            sys.exit("scoring.provider is gemini but GEMINI_API_KEY is not set in .env")
        g = s.section("gemini")
        return GeminiScorer(
            s.profile, s.gemini_api_key, model=g.get("model", "gemini-3.5-flash-lite"),
            thresholds=thresholds, feedback_examples=examples,
            min_interval=float(g.get("min_interval_sec", 6.0)),
            fallback_models=g.get("fallback_models") or [],
            retries=int(g.get("retries", 3)),
        )
    if provider == "ollama":
        o = s.section("ollama")
        return OllamaScorer(
            s.profile, model=o.get("model", "gemma3:4b"),
            host=o.get("host", "http://localhost:11434"), thresholds=thresholds,
            feedback_examples=examples, num_ctx=int(o.get("num_ctx", 8192)), think=o.get("think"),
        )
    if provider == "claude":
        if not s.anthropic_key_present:
            sys.exit("scoring.provider is claude but ANTHROPIC_API_KEY is not set in .env")
        c = s.section("claude")
        return LLMScorer(
            s.profile, model=c.get("model", DEFAULT_MODEL), effort=c.get("effort", "medium"),
            thresholds=thresholds, feedback_examples=examples,
        )
    if provider != "heuristic":
        sys.exit(f"unknown scoring.provider: {provider} (auto|gemini|ollama|claude|heuristic)")
    if not no_llm:
        log.warning("no LLM configured – using the heuristic (keyword) scorer")
    return HeuristicScorer(s.profile, thresholds)


def build_telegram(s: Settings, required: bool = False) -> Telegram | None:
    if not s.telegram_token:
        if required:
            sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
        return None
    return Telegram(s.telegram_token, s.telegram_chat_id)


# ---------------------------------------------------------------- commands
def _acquire_run_lock(db_path: Path):
    """One run at a time (launchd + a manual run must not overlap). Returns the open lock file."""
    import fcntl

    lock_path = Path(str(db_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def build_budget(s: Settings, db: DB, scorer) -> DailyBudget | None:
    """Daily request cap for providers with a free-tier quota (Gemini)."""
    if not getattr(scorer, "name", "").startswith("gemini:"):
        return None
    limit = s.section("gemini").get("max_requests_per_day", 150)
    return DailyBudget(db, None if limit in (None, 0) else int(limit))


def cmd_run(s: Settings, db: DB, a) -> int:
    lock = _acquire_run_lock(a.db or s.db_path)
    if lock is None:
        print("another jobradar run is in progress – skipping this one")
        return 0
    http = PoliteClient(min_interval=float(s.section("http").get("min_interval_sec", 2.0)))
    only = set(a.sources.split(",")) if a.sources else None
    tg = None if (a.dry_run or a.no_telegram) else build_telegram(s)
    if tg is not None and not s.telegram_chat_id:
        sys.exit("TELEGRAM_CHAT_ID is not set – run `jobradar tg-setup` first")
    scorer = build_scorer(s, db, a.no_llm)
    runner = Runner(
        db=db,
        collectors=build_collectors(s, only, http),
        matcher=TitleMatcher.from_config(s.section("title_filter")),
        rules=Rules.from_config(s.section("rules")),
        scorer=scorer,
        telegram=tg,
        notify_cfg=s.section("notify"),
        max_scores_per_run=int(s.section("scoring").get("max_per_run", 40)),
        dry_run=a.dry_run,
        budget=build_budget(s, db, scorer),
    )
    report = runner.run()
    http.close()
    lock.close()
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
    limit = s.section("gemini").get("max_requests_per_day", 150)
    b = DailyBudget(db, limit)
    print(f"gemini requests today (Pacific day): {b.used()} / {limit}")
    if until := b.paused_until():
        print(f"LLM paused until {until.astimezone():%Y-%m-%d %H:%M} (daily quota)")
    return 0


def cmd_show(s: Settings, db: DB, a) -> int:
    rows = db.by_status(*a.status.split(","))[-a.limit :]
    for r in rows:
        score = "" if r["score"] is None else f"{r['score']:>3} {r['verdict']:<5}"
        print(f"#{r['id']:<5} {r['source']:<8} {score:<10} {r['title'][:70]:<70} {r['company'][:25]:<25} {r['reason'][:50]}")
    return 0


SAMPLE_VACANCY = Vacancy(
    source="djinni", source_id="0", url="https://djinni.co/jobs/0-sample/",
    title="Head of Marketing", company="Sample Defence Tech", remote=True,
    description=(
        "We are a Ukrainian defence-tech product company. Looking for a Head of Marketing to "
        "build the brand platform and positioning, own go-to-market for new products, lead a "
        "team of 6 and work with agencies. 8+ years in marketing leadership required."
    ),
)


def cmd_llm_check(s: Settings, db: DB, a) -> int:
    """One scoring call on a built-in sample vacancy – verifies keys/model/Ollama setup."""
    provider = resolve_provider(s)
    print(f"provider: {provider}")
    scorer = build_scorer(s, db, no_llm=False)
    print(f"scorer:   {scorer.name}\n")
    try:
        r = scorer.score(SAMPLE_VACANCY)
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return 1
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    print("\n✅ OK")
    return 0


def cmd_requeue(s: Settings, db: DB, a) -> int:
    """Send already-seen vacancies back to scoring (e.g. after editing profile.yaml or rules).

    Never touches vacancies that were already delivered to Telegram.
    """
    n_skip = n_rule = 0
    if a.skipped:
        for row in db.conn.execute(
            "SELECT id FROM vacancies WHERE status='sent' AND tg_message_id IS NULL"
        ).fetchall():
            db.set_status(row["id"], "new", "requeued")
            n_skip += 1
    if a.rule_rejected:
        rules = Rules.from_config(s.section("rules"))
        for row in db.by_status("rule_rejected"):
            ok, _ = rules.check(row_to_vacancy(row))
            if ok:
                db.set_status(row["id"], "new", "requeued")
                n_rule += 1
    print(f"requeued: {n_skip} previously skipped, {n_rule} previously rule-rejected")
    print("run `uv run jobradar run` to score them")
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
    sub.add_parser("llm-check", help="score a sample vacancy to test the LLM setup").set_defaults(
        fn=cmd_llm_check
    )

    sh = sub.add_parser("show", help="list vacancies by status")
    sh.add_argument("--status", default="sent,scored")
    sh.add_argument("--limit", type=int, default=30)
    sh.set_defaults(fn=cmd_show)

    rq = sub.add_parser("requeue", help="re-score already seen vacancies after profile/rule changes")
    rq.add_argument("--skipped", action="store_true", help="scored low and not sent to Telegram")
    rq.add_argument("--rule-rejected", action="store_true", help="dropped by rules that now pass")
    rq.set_defaults(fn=cmd_requeue)

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

"""SQLite storage. One row per (source, source_id); status tracks pipeline progress."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ScoreResult, Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT DEFAULT '',
    city          TEXT DEFAULT '',
    remote        INTEGER,
    salary_from   INTEGER,
    salary_to     INTEGER,
    currency      TEXT DEFAULT '',
    posted_at     TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    description_is_snippet INTEGER DEFAULT 0,
    query         TEXT DEFAULT '',
    dedup_key     TEXT DEFAULT '',
    -- new | title_rejected | rule_rejected | duplicate | scored | sent | error
    status        TEXT NOT NULL DEFAULT 'new',
    reason        TEXT DEFAULT '',
    score         INTEGER,
    verdict       TEXT,
    analysis      TEXT,          -- ScoreResult as JSON
    feedback      TEXT,          -- applied | skipped (from Telegram buttons)
    tg_message_id INTEGER,
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    sent_at       TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_vac_status ON vacancies(status);
CREATE INDEX IF NOT EXISTS ix_vac_dedup ON vacancies(dedup_key);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- meta -------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---- vacancies -----------------------------------------------------------
    def exists(self, source: str, source_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM vacancies WHERE source=? AND source_id=?", (source, source_id)
            ).fetchone()
            is not None
        )

    def insert(self, v: Vacancy, status: str = "new", reason: str = "", dedup_key: str = "") -> int:
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO vacancies
               (source, source_id, url, title, company, city, remote, salary_from, salary_to,
                currency, posted_at, description, description_is_snippet, query, dedup_key,
                status, reason, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                v.source, v.source_id, v.url, v.title, v.company, v.city,
                None if v.remote is None else int(v.remote),
                v.salary_from, v.salary_to, v.currency, v.posted_at, v.description,
                int(v.description_is_snippet), v.query, dedup_key, status, reason, now, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_vacancy(self, row_id: int, v: Vacancy, dedup_key: str | None = None) -> None:
        self.conn.execute(
            """UPDATE vacancies SET url=?, title=?, company=?, city=?, remote=?, salary_from=?,
               salary_to=?, currency=?, posted_at=?, description=?, description_is_snippet=?,
               dedup_key=COALESCE(?, dedup_key), updated_at=? WHERE id=?""",
            (
                v.url, v.title, v.company, v.city, None if v.remote is None else int(v.remote),
                v.salary_from, v.salary_to, v.currency, v.posted_at, v.description,
                int(v.description_is_snippet), dedup_key, _now(), row_id,
            ),
        )
        self.conn.commit()

    def set_status(self, row_id: int, status: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE vacancies SET status=?, reason=?, updated_at=? WHERE id=?",
            (status, reason, _now(), row_id),
        )
        self.conn.commit()

    def save_score(self, row_id: int, result: ScoreResult) -> None:
        self.conn.execute(
            "UPDATE vacancies SET status='scored', score=?, verdict=?, analysis=?, updated_at=? "
            "WHERE id=?",
            (result.score, result.verdict, json.dumps(result.to_dict(), ensure_ascii=False),
             _now(), row_id),
        )
        self.conn.commit()

    def mark_sent(self, row_id: int, message_id: int | None) -> None:
        now = _now()
        self.conn.execute(
            "UPDATE vacancies SET status='sent', tg_message_id=?, sent_at=?, updated_at=? WHERE id=?",
            (message_id, now, now, row_id),
        )
        self.conn.commit()

    def set_feedback(self, row_id: int, feedback: str) -> None:
        self.conn.execute(
            "UPDATE vacancies SET feedback=?, updated_at=? WHERE id=?", (feedback, _now(), row_id)
        )
        self.conn.commit()

    def find_duplicate(self, dedup_key: str, exclude_id: int) -> sqlite3.Row | None:
        """Another vacancy with the same company+title that already went through scoring."""
        if not dedup_key:
            return None
        return self.conn.execute(
            "SELECT * FROM vacancies WHERE dedup_key=? AND id<>? "
            "AND status IN ('scored','sent') ORDER BY id LIMIT 1",
            (dedup_key, exclude_id),
        ).fetchone()

    def get(self, row_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM vacancies WHERE id=?", (row_id,)).fetchone()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM vacancies WHERE status IN ({q}) ORDER BY id", statuses
        ).fetchall()

    def stats(self) -> dict:
        out: dict = {}
        for row in self.conn.execute(
            "SELECT source, status, COUNT(*) n FROM vacancies GROUP BY source, status"
        ):
            out.setdefault(row["source"], {})[row["status"]] = row["n"]
        out["feedback"] = {
            r["feedback"]: r["n"]
            for r in self.conn.execute(
                "SELECT feedback, COUNT(*) n FROM vacancies WHERE feedback IS NOT NULL "
                "GROUP BY feedback"
            )
        }
        return out

    def feedback_examples(self, limit: int = 6) -> list[sqlite3.Row]:
        """Latest vacancies the owner explicitly reacted to (for few-shot calibration)."""
        return self.conn.execute(
            "SELECT title, company, score, feedback FROM vacancies WHERE feedback IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


def row_to_vacancy(row: sqlite3.Row) -> Vacancy:
    return Vacancy(
        source=row["source"], source_id=row["source_id"], url=row["url"], title=row["title"],
        company=row["company"] or "", city=row["city"] or "",
        remote=None if row["remote"] is None else bool(row["remote"]),
        salary_from=row["salary_from"], salary_to=row["salary_to"],
        currency=row["currency"] or "", posted_at=row["posted_at"] or "",
        description=row["description"] or "",
        description_is_snippet=bool(row["description_is_snippet"]), query=row["query"] or "",
    )

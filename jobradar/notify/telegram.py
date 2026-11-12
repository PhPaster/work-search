"""Telegram Bot API over plain HTTPS: send recommendations, read button presses."""

from __future__ import annotations

import html
import logging

import httpx

from ..models import ScoreResult, Vacancy
from ..text import truncate

log = logging.getLogger(__name__)

SOURCE_LABEL = {"djinni": "Djinni", "workua": "Work.ua", "robotaua": "Robota.ua"}
VERDICT_ICON = {"apply": "🟢", "maybe": "🟡", "skip": "⚪️"}


def _e(s: str) -> str:
    return html.escape(s or "", quote=False)


def format_vacancy(v: Vacancy, r: ScoreResult) -> str:
    head = f"{VERDICT_ICON.get(r.verdict, '')} <b>{r.score}/100</b> — <b>{_e(v.title)}</b>"
    sub = " · ".join(
        x for x in [_e(v.company), SOURCE_LABEL.get(v.source, v.source)] if x
    )
    meta = " · ".join(
        x
        for x in [
            f"💰 {_e(v.salary_text())}" if v.salary_text() else "",
            "🏠 Remote" if v.remote else "",
            f"📍 {_e(v.city)}" if v.city else "",
        ]
        if x
    )
    lines = [head, sub]
    if meta:
        lines.append(meta)
    if r.why:
        lines += ["", _e(r.why)]
    if r.matched:
        lines.append("✔ " + _e(", ".join(r.matched[:6])))
    if r.gaps:
        lines.append("✖ " + _e(", ".join(r.gaps[:4])))
    if r.red_flags:
        lines.append("⚠ " + _e(", ".join(r.red_flags[:4])))
    if r.cover_hook:
        lines += ["", f"💡 <i>{_e(r.cover_hook)}</i>"]
    return truncate("\n".join(lines), 4000)


def format_digest(items: list[tuple[int, Vacancy, ScoreResult]]) -> str:
    lines = [f"🟡 <b>Варто глянути ({len(items)})</b>", ""]
    for _, v, r in items:
        company = f" · {_e(v.company)}" if v.company else ""
        lines.append(
            f"<b>{r.score}</b> — <a href=\"{_e(v.url)}\">{_e(v.title)}</a>{company} "
            f"({SOURCE_LABEL.get(v.source, v.source)})"
        )
        if r.why:
            lines.append(f"   <i>{_e(truncate(r.why, 160))}</i>")
    return truncate("\n".join(lines), 4000)


def vacancy_keyboard(row_id: int, url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Відкрити вакансію", "url": url}],
            [
                {"text": "✅ Відгукнувся", "callback_data": f"fb:applied:{row_id}"},
                {"text": "⏭ Пропустити", "callback_data": f"fb:skipped:{row_id}"},
            ],
        ]
    }


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str | None, timeout: float = 20.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=timeout)

    def _call(self, method: str, **payload) -> dict | list | bool:
        resp = self.http.post(f"{self.base}/{method}", json=payload)
        try:
            data = resp.json()
        except ValueError as e:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from e
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, text: str, reply_markup: dict | None = None) -> int:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        msg = self._call("sendMessage", **payload)
        return int(msg["message_id"])  # type: ignore[index]

    def send_vacancy(self, row_id: int, v: Vacancy, r: ScoreResult) -> int:
        return self.send(format_vacancy(v, r), vacancy_keyboard(row_id, v.url))

    def get_updates(self, offset: int | None = None) -> list[dict]:
        payload: dict = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", **payload))  # type: ignore[arg-type]

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:  # callbacks older than ~15 min can't be answered
            log.debug("answerCallbackQuery failed: %s", e)

    def mark_message(self, message_id: int, url: str, label: str) -> None:
        try:
            self._call(
                "editMessageReplyMarkup",
                chat_id=self.chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": [[{"text": f"{label} · відкрити", "url": url}]]},
            )
        except TelegramError as e:
            log.debug("editMessageReplyMarkup failed: %s", e)

"""Work.ua and Robota.ua via their own e-mail job alerts (read over IMAP).

Why e-mail: both sites put every page (and Robota.ua's API) behind a Cloudflare JS challenge
for non-browser clients. We do NOT try to get around that. Instead the owner subscribes to
saved-search alerts on each site ("Отримувати нові вакансії на e-mail"), and this collector
reads those e-mails from the mailbox in read-only mode.

The parser is deliberately layout-agnostic: it linearises the HTML into text + links,
finds links that point to a vacancy (also inside tracking redirects), uses the anchor text
as the title and the text until the next vacancy link as the snippet (company, salary, city).
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import unquote

from ..models import Vacancy
from .base import Collector, CollectorError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteRule:
    source: str
    sender: str  # substring matched against From (IMAP FROM search)
    pattern: re.Pattern  # group(1) = vacancy id, applied to the (decoded) href
    url_template: str


SITES = {
    "workua": SiteRule(
        source="workua",
        sender="work.ua",
        pattern=re.compile(r"work\.ua/(?:(?:ru|en|uk)/)?jobs/(\d{5,})"),
        url_template="https://www.work.ua/jobs/{id}/",
    ),
    "robotaua": SiteRule(
        source="robotaua",
        sender="robota.ua",
        pattern=re.compile(r"robota\.ua/(?:(?:ru|en|uk)/)?company\d+/vacancy(\d+)"),
        url_template="",  # keep the canonical URL found in the e-mail
    ),
}

# Anchor texts that are buttons, not titles
_GENERIC = re.compile(
    r"^(детальніше|докладніше|переглянути|відгукнутися|подробнее|откликнуться|"
    r"смотреть|дивитися|view|apply|more|details|відкрити|перейти)\b",
    re.I,
)
_SALARY_RE = re.compile(
    r"(\d[\d\s  ]{2,})(?:\s*[–\-—]\s*(\d[\d\s  ]{2,}))?\s*(грн|uah|\$|usd|€|eur)",
    re.I,
)


class _Linearizer(HTMLParser):
    """Turns HTML into a flat list of ('text', s) and ('link', href, anchor_text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._anchor = []
        elif tag in ("br", "p", "div", "tr", "td", "li", "table", "h1", "h2", "h3"):
            self.tokens.append(("text", "\n"))

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._anchor)).strip()
            self.tokens.append(("link", self._href, text))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._anchor.append(data)
        else:
            self.tokens.append(("text", data))


def _decode_href(href: str) -> str:
    """Tracking links often carry the real URL url-encoded (sometimes twice)."""
    out = href
    for _ in range(3):
        nxt = unquote(out)
        if nxt == out:
            break
        out = nxt
    return out


def _clean_snippet(parts: list[str]) -> str:
    text = "".join(parts).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:800]


def _parse_salary(snippet: str) -> tuple[int | None, int | None, str]:
    m = _SALARY_RE.search(snippet)
    if not m:
        return None, None, ""

    def num(s: str | None) -> int | None:
        if not s:
            return None
        digits = re.sub(r"\D", "", s)
        return int(digits) if digits else None

    cur = m.group(3).lower()
    cur = {"грн": "UAH", "uah": "UAH", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR"}[cur]
    lo, hi = num(m.group(1)), num(m.group(2))
    return lo, hi or lo, cur


def parse_alert_html(html_text: str, rule: SiteRule) -> list[Vacancy]:
    p = _Linearizer()
    p.feed(html_text)
    p.close()

    found: dict[str, dict] = {}
    order: list[str] = []
    current: str | None = None
    for tok in p.tokens:
        if tok[0] == "link":
            _, href, anchor = tok
            decoded = _decode_href(href)
            m = rule.pattern.search(decoded)
            if m:
                vid = m.group(1)
                if vid not in found:
                    url = (
                        rule.url_template.format(id=vid)
                        if rule.url_template
                        else "https://" + decoded[m.start():m.end()] + "/"
                    )
                    found[vid] = {"title": "", "url": url, "snippet": []}
                    order.append(vid)
                if not found[vid]["title"] and anchor and not _GENERIC.match(anchor):
                    found[vid]["title"] = anchor
                current = vid
                continue
            # a non-vacancy link inside a block (e.g. company page) – keep its text
            if current and anchor:
                found[current]["snippet"].append(" " + anchor + " ")
        elif current:
            found[current]["snippet"].append(tok[1])

    out = []
    for vid in order:
        rec = found[vid]
        if not rec["title"]:
            continue
        snippet = _clean_snippet(rec["snippet"])
        lo, hi, cur = _parse_salary(snippet)
        out.append(
            Vacancy(
                source=rule.source,
                source_id=vid,
                url=rec["url"],
                title=rec["title"],
                description=snippet,
                description_is_snippet=True,
                salary_from=lo,
                salary_to=hi,
                currency=cur,
                query="email-alert",
            )
        )
    return out


def html_parts(msg: Message) -> list[str]:
    parts = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    return parts


def parse_alert_message(raw: bytes, rule: SiteRule) -> list[Vacancy]:
    msg = email.message_from_bytes(raw)
    out: list[Vacancy] = []
    for h in html_parts(msg):
        out.extend(parse_alert_html(h, rule))
    return out


class EmailAlertsCollector(Collector):
    """Reads job-alert e-mails from an IMAP mailbox (read-only: nothing is marked as read)."""

    name = "email"

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        sites: list[str],
        folder: str = "INBOX",
        days_back: int = 3,
        port: int = 993,
    ):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.rules = [SITES[s] for s in sites]
        self.folder = folder
        self.days_back = days_back

    def collect(self) -> list[Vacancy]:
        since = (date.today() - timedelta(days=self.days_back)).strftime("%d-%b-%Y")
        try:
            imap = imaplib.IMAP4_SSL(self.host, self.port)
            imap.login(self.user, self.password)
        except (imaplib.IMAP4.error, OSError) as e:
            raise CollectorError(f"email: cannot log in to {self.host}: {e}") from e
        out: list[Vacancy] = []
        try:
            typ, _ = imap.select(f'"{self.folder}"', readonly=True)
            if typ != "OK":
                raise CollectorError(f"email: cannot open folder {self.folder}")
            for rule in self.rules:
                typ, data = imap.search(None, "SINCE", since, "FROM", f'"{rule.sender}"')
                ids = data[0].split() if typ == "OK" and data and data[0] else []
                n_before = len(out)
                for num in ids:
                    typ, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                    if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    out.extend(parse_alert_message(msg_data[0][1], rule))
                log.info(
                    "email %s: %d messages since %s -> %d vacancies",
                    rule.source, len(ids), since, len(out) - n_before,
                )
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        # de-duplicate within this batch (the same vacancy often comes in several alerts)
        uniq: dict[tuple[str, str], Vacancy] = {}
        for v in out:
            uniq.setdefault(v.key, v)
        return list(uniq.values())

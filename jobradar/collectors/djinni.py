"""Djinni (djinni.co) collector.

Search: the public RSS feed `https://djinni.co/jobs/rss/?<params>` — the same filters as the
site's search page (`all_keywords=...` is a full-text AND search, `primary_keyword=Marketing`
is the whole Marketing category). Each item carries the full description.

Details: each vacancy page embeds schema.org `JobPosting` JSON-LD with company, salary,
remote flag and location.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from ..http import PoliteClient
from ..models import Vacancy
from ..text import html_to_text
from .base import Collector, CollectorError

log = logging.getLogger(__name__)

RSS_URL = "https://djinni.co/jobs/rss/"
_ID_RE = re.compile(r"/jobs/(\d+)")
_LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)


def parse_rss(xml_text: str | bytes, query: str = "") -> list[Vacancy]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise CollectorError(f"djinni: RSS is not valid XML ({e})") from e
    channel = root.find("channel")
    if channel is None:
        raise CollectorError("djinni: RSS has no <channel>")
    out: list[Vacancy] = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        m = _ID_RE.search(link)
        if not m:
            continue
        posted = ""
        if pub := item.findtext("pubDate"):
            try:
                posted = parsedate_to_datetime(pub).isoformat()
            except (TypeError, ValueError):
                posted = ""
        out.append(
            Vacancy(
                source="djinni",
                source_id=m.group(1),
                url=link,
                title=(item.findtext("title") or "").strip(),
                description=html_to_text(item.findtext("description") or ""),
                posted_at=posted,
                query=query,
            )
        )
    return out


def parse_job_ldjson(page_html: str) -> dict | None:
    for block in _LDJSON_RE.findall(page_html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d
    return None


def apply_ldjson(v: Vacancy, d: dict) -> Vacancy:
    org = d.get("hiringOrganization") or {}
    if isinstance(org, dict) and org.get("name"):
        v.company = str(org["name"]).strip()
    if d.get("jobLocationType") == "TELECOMMUTE":
        v.remote = True
    locs = d.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    cities = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, dict) and addr.get("addressLocality"):
            cities.append(str(addr["addressLocality"]))
    if cities:
        v.city = ", ".join(dict.fromkeys(cities))
    sal = d.get("baseSalary") or {}
    if isinstance(sal, dict):
        val = sal.get("value") or {}
        if isinstance(val, dict):
            lo, hi = val.get("minValue"), val.get("maxValue")
            if lo is None and hi is None and val.get("value") is not None:
                lo = hi = val.get("value")
            v.salary_from = int(float(lo)) if lo is not None else None
            v.salary_to = int(float(hi)) if hi is not None else None
        v.currency = str(sal.get("currency") or v.currency or "")
    if not v.description and d.get("description"):
        v.description = html_to_text(str(d["description"]))
    return v


class DjinniCollector(Collector):
    name = "djinni"

    def __init__(self, http: PoliteClient, searches: list[dict], fetch_details: bool = True):
        self.http = http
        self.searches = searches
        self.fetch_details = fetch_details

    def collect(self) -> list[Vacancy]:
        seen: dict[str, Vacancy] = {}
        failures = 0
        for params in self.searches:
            url = f"{RSS_URL}?{urlencode(params)}"
            q = urlencode(params)
            try:
                resp = self.http.get(url)
                if resp.status_code != 200:
                    raise CollectorError(f"djinni: HTTP {resp.status_code} for {url}")
                items = parse_rss(resp.content, query=q)
            except Exception as e:  # one broken search must not kill the others
                failures += 1
                log.error("djinni search %s failed: %s", q, e)
                continue
            log.info("djinni %s -> %d items", q, len(items))
            for v in items:
                seen.setdefault(v.source_id, v)
        if failures and failures == len(self.searches):
            raise CollectorError("djinni: every search failed")
        return list(seen.values())

    def enrich(self, v: Vacancy) -> Vacancy:
        if not self.fetch_details:
            return v
        try:
            resp = self.http.get(v.url)
        except Exception as e:
            log.warning("djinni details %s failed: %s", v.url, e)
            return v
        if resp.status_code != 200:
            log.warning("djinni details %s -> HTTP %s", v.url, resp.status_code)
            return v
        d = parse_job_ldjson(resp.text)
        return apply_ldjson(v, d) if d else v

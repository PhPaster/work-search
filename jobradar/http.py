"""A polite HTTP client: per-host rate limit, retries with backoff, honest User-Agent."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; jobradar/0.1; personal job alerts; "
    "+https://github.com/PhPaster/work-search)"
)


class PoliteClient:
    def __init__(self, min_interval: float = 2.0, timeout: float = 20.0, retries: int = 3):
        self.min_interval = min_interval
        self.retries = retries
        self._last: dict[str, float] = {}
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"},
        )

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            delta = time.monotonic() - last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
        self._last[host] = time.monotonic()

    def get(self, url: str, **kw) -> httpx.Response:
        host = urlparse(url).netloc
        backoff = 5.0
        for attempt in range(1, self.retries + 1):
            self._wait(host)
            try:
                resp = self._client.get(url, **kw)
            except httpx.TransportError as e:
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt)
                if attempt == self.retries:
                    raise
            else:
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    log.warning("GET %s -> %s, retrying in %.0fs", url, resp.status_code, backoff)
                else:
                    return resp
            time.sleep(backoff)
            backoff *= 2
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self._client.close()

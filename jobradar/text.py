"""Text helpers: HTML -> plain text, normalisation."""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "td", "blockquote",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(src: str) -> str:
    if not src:
        return ""
    p = _TextExtractor()
    p.feed(src)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html.unescape(text).strip()


def normalize(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces. Keeps Cyrillic."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("ё", "е").replace("’", "'").replace("ʼ", "'")
    s = re.sub(r"[^\w']+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

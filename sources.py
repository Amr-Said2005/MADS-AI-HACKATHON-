"""Fetch tools and the citation registry.

The registry holds only URLs that this run actually fetched. A claim may cite a
URL only if it is registered; anything else is stamped ASSUMPTION by code before
the file is written. A URL the model produced from memory is never trusted.
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

USER_AGENT = "venture-builders/0.1 (open-source research agent)"
MAX_BYTES = 400_000
ASSUMPTION = "ASSUMPTION"


class FetchError(RuntimeError):
    pass


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    title: str
    text: str
    fetched_at: str


@dataclass
class Claim:
    text: str
    url: str | None = None
    kind: str = "other"  # "problem", "paying", "market", "other"
    label: str = ""      # set by stamp_claims: "SOURCED" or ASSUMPTION

    def to_dict(self) -> dict:
        return {"text": self.text, "url": self.url, "kind": self.kind, "label": self.label}


_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS = re.compile(r"\s+")


def html_to_text(raw: str) -> tuple[str, str]:
    m = _TITLE.search(raw)
    title = html.unescape(_WS.sub(" ", m.group(1))).strip() if m else ""
    text = html.unescape(_WS.sub(" ", _TAGS.sub(" ", raw))).strip()
    return title, text


@dataclass
class SourceRegistry:
    max_sources: int = 6
    pages: dict[str, FetchedPage] = field(default_factory=dict)
    transport: httpx.BaseTransport | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def fetch(self, url: str, timeout: float = 15.0) -> FetchedPage:
        if not url.startswith(("http://", "https://")):
            raise FetchError(f"not an http(s) URL: {url}")
        if url in self.pages:
            return self.pages[url]
        with self._lock:
            if len(self.pages) >= self.max_sources:
                raise FetchError(f"source budget reached (MAX_SOURCES={self.max_sources})")
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, transport=self.transport,
                              headers={"User-Agent": USER_AGENT}) as c:
                r = c.get(url)
        except httpx.HTTPError as exc:
            raise FetchError(f"fetch failed for {url}: {exc}") from exc
        if r.status_code >= 400:
            raise FetchError(f"fetch failed for {url}: HTTP {r.status_code}")
        raw = r.content[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
        if "html" in r.headers.get("content-type", "html"):
            title, text = html_to_text(raw)
        else:
            title, text = "", raw
        page = FetchedPage(url, str(r.url), r.status_code, title, text[:20_000],
                           datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        with self._lock:
            self.pages[url] = page
            self.pages.setdefault(str(r.url), page)
        return page

    def restore(self, url: str) -> None:
        """Re-register a URL fetched earlier in this same run (after a pause for approval)."""
        self.pages.setdefault(url, FetchedPage(url, url, 200, "", "", ""))

    def is_registered(self, url: str | None) -> bool:
        return bool(url) and url in self.pages

    def urls(self) -> list[str]:
        return list(dict.fromkeys(p.url for p in self.pages.values()))

    def stamp_claims(self, claims: list[Claim]) -> list[Claim]:
        """Keep a citation only if this run fetched it; otherwise label the claim ASSUMPTION."""
        out = []
        for c in claims:
            if self.is_registered(c.url):
                out.append(Claim(c.text, c.url, c.kind, "SOURCED"))
            else:
                out.append(Claim(c.text, None, c.kind, ASSUMPTION))
        return out


def claims_to_markdown(claims: list[Claim]) -> str:
    lines = []
    for c in claims:
        if c.label == "SOURCED":
            lines.append(f"- {c.text} ([source]({c.url}))")
        else:
            lines.append(f"- {c.text} **{ASSUMPTION}**")
    return "\n".join(lines)

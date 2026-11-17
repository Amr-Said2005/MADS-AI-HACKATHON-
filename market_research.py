"""Market research helpers.

Three jobs, all done by code so the model never decides them:

1. Search: role-tagged adapters, general web search first (search()).
2. Queries: built from the brief's own words (build_queries()).
3. Competitors: every entry is checked against the page it cites
   (verify_competitors()).

The model does not browse and calls no tools. Code runs the searches and
fetches; the model only reads the text it is given.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Source roles
# --------------------------------------------------------------------------

ROLE_GENERAL = "general"      # may supply SAY, PAY and competitors
ROLE_COMMUNITY = "community"  # may supply SAY only
ROLE_REFERENCE = "reference"  # background only: never evidence, never a competitor

REFERENCE_HOSTS = (
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org",
    "britannica.com", "github.com",
)
COMMUNITY_HOSTS = (
    "news.ycombinator.com", "stackexchange.com", "stackoverflow.com",
    "superuser.com", "serverfault.com",
)


def host_of(url: str) -> str:
    """Lower-case host of a URL without a leading 'www.'."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_in(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def role_for_url(url: str, adapter_role: str = ROLE_GENERAL) -> str:
    """Role of a page. The host can only make the role stricter.

    A Wikipedia page is reference material even when a general web search
    returned it.
    """
    host = host_of(url)
    if adapter_role == ROLE_REFERENCE or _host_in(host, REFERENCE_HOSTS):
        return ROLE_REFERENCE
    if adapter_role == ROLE_COMMUNITY or _host_in(host, COMMUNITY_HOSTS):
        return ROLE_COMMUNITY
    return ROLE_GENERAL


# --------------------------------------------------------------------------
# Queries built from the brief
# --------------------------------------------------------------------------

PURPOSE_SAY = "say"                  # people describing the problem
PURPOSE_PAY = "pay"                  # people paying, and competitors
PURPOSE_MARKETPLACE = "marketplace"  # digital-product marketplaces

MARKETPLACES = ("Udemy", "Coursera", "Gumroad", "Etsy", "Teachable")

_STOPWORDS = frozenset(
    "about with that this from they them their there have what when which "
    "your want wants need needs into than then also some more most very "
    "for and the best".split()
)


@dataclass(frozen=True)
class BriefTerms:
    """Words taken from the founder's brief by the assessor."""
    customer: str
    problem: str
    product: str
    topic: str = ""


@dataclass(frozen=True)
class Query:
    text: str
    purpose: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def content_words(text: str) -> set[str]:
    """Lower-case words of four or more letters that are not stopwords."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def build_queries(terms: BriefTerms, marketplaces: Sequence[str] = MARKETPLACES[:2]) -> list[Query]:
    """Queries filled from the brief. No free-form model text is used."""
    customer, problem = _clean(terms.customer), _clean(terms.problem)
    product, topic = _clean(terms.product), _clean(terms.topic)
    candidates: list[tuple[str, str]] = []
    if customer and problem:
        candidates += [
            (f"{customer} struggling with {problem}", PURPOSE_SAY),
            (f"how do {customer} deal with {problem}", PURPOSE_SAY),
        ]
    if problem:
        candidates.append((f"{problem} forum", PURPOSE_SAY))
    if product:
        if customer:
            candidates += [
                (f"{product} for {customer} price", PURPOSE_PAY),
                (f"best {product} for {customer}", PURPOSE_PAY),
            ]
        candidates += [
            (f"{product} alternatives", PURPOSE_PAY),
            (_clean(f"buy {product} {topic}"), PURPOSE_PAY),
        ]
        for name in marketplaces:
            candidates.append((_clean(f"{product} {topic} {name}"), PURPOSE_MARKETPLACE))
    seen: set[str] = set()
    queries: list[Query] = []
    for text, purpose in candidates:
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            queries.append(Query(text, purpose))
    return queries


def accept_model_query(query: str, terms: BriefTerms) -> bool:
    """A model-suggested query needs three words and one word from the brief."""
    query = _clean(query)
    if len(query.split()) < 3:
        return False
    brief_words = content_words(" ".join([terms.customer, terms.problem, terms.product, terms.topic]))
    return bool(content_words(query) & brief_words)


def add_model_queries(base: list[Query], suggestions: Iterable[str], terms: BriefTerms,
                      purpose: str = PURPOSE_SAY, limit: int = 2) -> list[Query]:
    """Append at most `limit` accepted model queries to the code-built ones."""
    out = list(base)
    seen = {q.text.lower() for q in out}
    added = 0
    for suggestion in suggestions:
        text = _clean(suggestion)
        if added >= limit:
            break
        if text.lower() in seen or not accept_model_query(text, terms):
            continue
        out.append(Query(text, purpose))
        seen.add(text.lower())
        added += 1
    return out


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

SearchFn = Callable[[str, int], list[dict]]


@dataclass(frozen=True)
class Adapter:
    name: str
    role: str
    fn: SearchFn


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    adapter: str
    role: str
    query: str
    purpose: str


@dataclass
class SearchReport:
    results: list[SearchResult] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)  # adapter, query, reason

    @property
    def collected_nothing(self) -> bool:
        return not self.results


def ddgs_search(query: str, max_results: int = 5) -> list[dict]:
    """Keyless general web search through the `ddgs` library."""
    from ddgs import DDGS  # imported here so tests need no network or package
    return DDGS().text(query, max_results=max_results)


def _result_url(item: dict) -> str:
    return str(item.get("href") or item.get("url") or item.get("link") or "")


def search(queries: Iterable[Query], adapters: Sequence[Adapter],
           max_results: int = 5, per_domain_cap: int = 2) -> SearchReport:
    """Run the queries and return role-tagged results plus every failure.

    - General adapters are tried in the order given; the first one that
      returns results answers the query.
    - Community adapters are used only for SAY queries.
    - Reference adapters are never called here.
    - An adapter that raises or returns nothing is recorded, never hidden,
      so an empty report can say why nothing was collected.
    """
    report = SearchReport()
    seen_urls: set[str] = set()
    per_domain: dict[str, int] = {}

    def take(items: list[dict], adapter: Adapter, query: Query) -> int:
        kept = 0
        for item in items or []:
            url = _result_url(item)
            host = host_of(url)
            if not url.startswith(("http://", "https://")) or not host or url in seen_urls:
                continue
            if per_domain.get(host, 0) >= per_domain_cap:
                continue
            seen_urls.add(url)
            per_domain[host] = per_domain.get(host, 0) + 1
            report.results.append(SearchResult(
                url=url,
                title=_clean(str(item.get("title") or "")),
                snippet=_clean(str(item.get("body") or item.get("snippet") or item.get("content") or "")),
                adapter=adapter.name,
                role=role_for_url(url, adapter.role),
                query=query.text,
                purpose=query.purpose,
            ))
            kept += 1
        return kept

    def call(adapter: Adapter, query: Query) -> int:
        try:
            items = adapter.fn(query.text, max_results)
        except Exception as exc:  # network, rate limit, CAPTCHA, parsing
            report.failures.append({"adapter": adapter.name, "query": query.text,
                                    "reason": f"{type(exc).__name__}: {exc}"[:200]})
            return 0
        if not items:
            report.failures.append({"adapter": adapter.name, "query": query.text, "reason": "no results"})
            return 0
        return take(items, adapter, query)

    general = [a for a in adapters if a.role == ROLE_GENERAL]
    community = [a for a in adapters if a.role == ROLE_COMMUNITY]
    for query in queries:
        for adapter in general:
            if call(adapter, query):
                break
        if query.purpose == PURPOSE_SAY:
            for adapter in community:
                call(adapter, query)
    return report


# --------------------------------------------------------------------------
# Prices and evidence tags
# --------------------------------------------------------------------------

_CURRENCY_WORDS = r"usd|eur|gbp|egp|sar|aed|inr|cad|aud|dollars?|euros?|pounds?"
_PRICE_RE = re.compile(
    rf"""(?ix)
    (?:[$€£¥₹]\s?(?P<a>\d[\d,]*(?:\.\d+)?))
    |(?:\b(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?:{_CURRENCY_WORDS})\b)
    |(?:\b(?:{_CURRENCY_WORDS})\s?(?P<c>\d[\d,]*(?:\.\d+)?))
    """
)


def find_price(text: str) -> str | None:
    """First amount above zero that carries a currency sign or name."""
    for match in _PRICE_RE.finditer(text or ""):
        number = match.group("a") or match.group("b") or match.group("c")
        try:
            if float(number.replace(",", "")) > 0:
                return _clean(match.group(0))
        except ValueError:
            continue
    return None


def confirm_tag(proposed: str, page_text: str, role: str) -> tuple[str | None, str]:
    """Check the model's SAY or PAY tag. Returns (tag or None, note).

    - Reference pages are never evidence.
    - PAY needs a general web page with a price in its text; otherwise the
      tag is downgraded to SAY and the note says why.
    """
    proposed = (proposed or "").strip().upper()
    if role == ROLE_REFERENCE:
        return None, "reference page: background only"
    if proposed == "PAY":
        if role != ROLE_GENERAL:
            return "SAY", "downgraded: PAY needs a general web page"
        price = find_price(page_text)
        if not price:
            return "SAY", "downgraded: no price found in the page text"
        return "PAY", f"price found: {price}"
    if proposed == "SAY":
        return "SAY", ""
    return None, f"unknown tag: {proposed or 'empty'}"


# --------------------------------------------------------------------------
# Competitors
# --------------------------------------------------------------------------

KIND_DIRECT = "direct"                   # sells a comparable product, price on the page
KIND_UNPRICED = "unpriced"               # comparable offer, no price found on the page
KIND_FREE = "free_alternative"           # free material covering the same need
KIND_NOT = "not_a_competitor"
_KINDS = {KIND_DIRECT, KIND_UNPRICED, KIND_FREE, KIND_NOT}

GENERIC_NAMES = frozenset({
    "world wide web", "www", "web", "internet", "online", "websites", "other websites",
    "online platform", "online platforms", "search engine", "search engines",
    "google search", "wikipedia", "encyclopedia", "encyclopedias", "social media",
    "e-learning", "online learning", "online courses", "online education",
    "competitors", "various", "n/a", "none", "unknown",
})

NO_COMPETITOR_LINE = "No competitor was confirmed in the fetched pages."


@dataclass(frozen=True)
class Page:
    number: int
    url: str
    text: str
    role: str
    title: str = ""


@dataclass(frozen=True)
class CompetitorEntry:
    """What the model returns for one competitor."""
    name: str
    page: int
    kind: str
    what_they_offer: str
    quote: str


@dataclass(frozen=True)
class VerifiedCompetitor:
    name: str
    url: str
    kind: str
    what_they_offer: str
    quote: str
    price: str | None = None


@dataclass
class CompetitorReport:
    kept: list[VerifiedCompetitor] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # name, reason


def normalise(text: str) -> str:
    """NFKC, lower case, straight quotes and dashes, single spaces."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_generic(name: str) -> bool:
    key = normalise(name)
    key = re.sub(r"^(the|a|an)\s+", "", key)
    return key in GENERIC_NAMES or len(key) < 2


def verify_competitors(entries: Iterable[CompetitorEntry], pages: Iterable[Page],
                       min_quote_words: int = 3) -> CompetitorReport:
    """Keep only competitors the cited page actually supports."""
    by_number = {p.number: p for p in pages}
    report = CompetitorReport()

    def drop(entry: CompetitorEntry, reason: str) -> None:
        report.dropped.append({"name": entry.name, "reason": reason})

    for entry in entries:
        kind = (entry.kind or "").strip().lower()
        if kind not in _KINDS:
            drop(entry, f"unknown kind: {entry.kind!r}")
            continue
        if kind == KIND_NOT:
            drop(entry, "marked not a competitor")
            continue
        if _is_generic(entry.name):
            drop(entry, "generic name, not a competitor")
            continue
        page = by_number.get(entry.page)
        if page is None:
            drop(entry, f"cites page {entry.page}, which was not fetched")
            continue
        if page.role != ROLE_GENERAL:
            drop(entry, f"page is {page.role} material, not a general web page")
            continue
        page_text = normalise(page.text)
        quote = normalise(entry.quote)
        if len(quote.split()) < min_quote_words or quote not in page_text:
            drop(entry, "quote not found in the page text")
            continue
        name = normalise(entry.name)
        on_page = name in page_text or name in normalise(page.title)
        in_host = name.replace(" ", "") in host_of(page.url).replace("-", "").replace(".", "")
        if not (on_page or in_host):
            drop(entry, "name not found on the page")
            continue
        price = find_price(page.text)
        if kind == KIND_DIRECT and not price:
            kind = KIND_UNPRICED
        report.kept.append(VerifiedCompetitor(
            name=_clean(entry.name), url=page.url, kind=kind,
            what_they_offer=_clean(entry.what_they_offer), quote=_clean(entry.quote),
            price=price if kind == KIND_DIRECT else None,
        ))
    return report


_KIND_LABEL = {
    KIND_DIRECT: "sells a comparable product",
    KIND_UNPRICED: "comparable offer, no price found on the page",
    KIND_FREE: "free alternative",
}


def render_competitors(report: CompetitorReport) -> str:
    """Markdown for the market file. Invents nothing when the list is empty."""
    if not report.kept:
        return NO_COMPETITOR_LINE
    lines = []
    for c in report.kept:
        price = f", price seen: {c.price}" if c.price else ""
        lines.append(f'- **{c.name}** ({_KIND_LABEL[c.kind]}{price}): {c.what_they_offer} '
                     f'Quote: "{c.quote}" Source: {c.url}')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# What the model sees
# --------------------------------------------------------------------------

def excerpt(text: str, query: str, limit: int = 1500) -> str:
    """Passages of a page that match the query words, in page order.

    Falls back to the start of the page when nothing matches.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    words = content_words(query)
    passages = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text) if p.strip()]
    scored = [(sum(1 for w in words if w in p.lower()), i, p) for i, p in enumerate(passages)]
    hits = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
    if not hits:
        return text[:limit]
    chosen: list[tuple[int, str]] = []
    used = 0
    for _, index, passage in hits:
        piece = passage[: max(0, limit - used)]
        if not piece:
            break
        chosen.append((index, piece))
        used += len(piece) + 1
        if used >= limit:
            break
    return "\n".join(p for _, p in sorted(chosen))

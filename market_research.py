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

import gates

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

# Marketplace hosts by product family, used in `site:` queries. An entry may carry one extra word after the host.
MARKETPLACE_HOSTS: dict[str, tuple[str, ...]] = {
    "Written content": ("gumroad.com", "etsy.com"),
    "Courses and learning": ("udemy.com", "coursera.org"),
    "Templates and tools": ("gumroad.com", "etsy.com", "notion.so templates"),
}
DEFAULT_MARKETPLACE_HOSTS = ("gumroad.com", "etsy.com")
# Written study material (notes, summaries, cheat sheets) is sold on study-notes marketplaces first.
STUDY_HOSTS = ("stuvia.com", "studocu.com", "gumroad.com", "etsy.com")
STUDY_RX = re.compile(r"\b(?:notes?|summar(?:y|ies)|cheat ?sheets?|flash ?cards?|study guides?|question banks?|formula sheets?)\b",
                      re.IGNORECASE)
MARKETPLACES = tuple(dict.fromkeys(STUDY_HOSTS + tuple(h for hosts in MARKETPLACE_HOSTS.values() for h in hosts)))
MAX_QUERIES = 8          # queries one step sends at most
MAX_MARKETPLACE_HOSTS = 3
MAX_PRODUCT_WORDS = 3
MAX_JOINED_WORDS = 4     # words in one product phrase joined from neighbours

_STOPWORDS = frozenset(
    "about with that this from they them their there have what when which "
    "your want wants need needs into than then also some more most very "
    "for and the best".split()
)

# Deliverables a founder may name that the product-family lists do not hold. They count as written content.
_DELIVERABLES_RX = re.compile(r"\b(?:summar(?:y|ies)|flash ?cards?|study guides?|question banks?|formula sheets?|"
                              r"cheat ?sheets?|templates?|checklists?|guides?|worksheets?|notes?)\b", re.IGNORECASE)
# A product-family word after one of these describes what the customer already does or owns, not the product.
_CONTEXT_RX = re.compile(r"\b(?:(?:enrolled in|taking|attending|studying)\s+(?:[\w-]+\s+){0,3}"
                         r"|(?:their|your|his|her)\s+(?:[\w-]+\s+)?)$", re.IGNORECASE)
# What follows one of these is what the founder delivers.
_CUE_RX = re.compile(r"\b(?:provid(?:ing|es?)|offer(?:ing|s)?|sell(?:ing|s)?|includ(?:ing|es?)|that gives?)\b", re.IGNORECASE)
_NEAR_CUE_RX = re.compile(r"\b(?:with|of)\s+(?:[\w-]+\s+){0,2}$", re.IGNORECASE)
# Words that never belong in a topic: filler, pronouns, what the founder wants to do, and words for building a site.
_TOPIC_STOP = frozenset("""
a an the and or of for to in on at by from as with without that this these those which who whom whose is are was
were be been being it its i me my we us our you your he she his her they them their there so such very can will
would should could just also only not no than then into about over under through during while when what how why
want wants wanted need needs like wish plan plans planning aim aims going try trying
develop develops developing build builds building create creates creating make makes making design designing
launch launching start starting help helps helping provide provides providing offer offers offering sell sells
selling give gives giving include includes including deliver delivers delivering let lets get gets use uses using
used ace learn learning track tracks tracking run running choose choosing manage managing work working do does doing
website websites site sites web webpage page pages app apps platform platforms portal online digital internet
store shop marketplace product products service services tool tools
new best easy simple better good great more most people person someone anyone everyone smoothly quickly day
""".split())


@dataclass(frozen=True)
class BriefTerms:
    """Words taken from the founder's brief. `products` (best first) and `family` are derived by code."""
    customer: str
    problem: str
    product: str
    topic: str = ""
    products: tuple[str, ...] = ()
    family: str = ""


@dataclass(frozen=True)
class Query:
    text: str
    purpose: str
    rung: int = 0  # 1 intent query, 2 marketplace `site:` query, 3 relaxed fallback; 0 for a problem (SAY) query


@dataclass(frozen=True)
class ProductWord:
    text: str    # as the idea says it, lower case ("digital notes")
    search: str  # as it goes into a query ("notes")
    family: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def content_words(text: str) -> set[str]:
    """Lower-case words of four or more letters that are not stopwords."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _stem(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if re.search(r"[a-z]", w)]


def _product_matches(idea: str) -> list[tuple[int, int, str]]:
    """(start, end, family) of every product-family or deliverable word in the idea, longest first where two
    overlap, and with neighbours joined ("Notion" + "template pack")."""
    found = [(m.start(), m.end(), fam.name) for fam in gates.PRODUCT_FAMILIES for m in fam.rx.finditer(idea)]
    found += [(m.start(), m.end(), "Written content") for m in _DELIVERABLES_RX.finditer(idea)]
    kept: list[tuple[int, int, str]] = []
    for start, end, family in sorted(found, key=lambda f: (f[0], f[0] - f[1])):
        if kept and start < kept[-1][1]:
            continue  # inside a longer match
        joined = idea[kept[-1][0]:end].lower() if kept else ""
        if (kept and not idea[kept[-1][1]:start].strip() and start > kept[-1][1] and len(_tokens(joined)) <= MAX_JOINED_WORDS
                and idea[start:end].lower() not in idea[kept[-1][0]:kept[-1][1]].lower()):
            kept[-1] = (kept[-1][0], end, family)  # adjacent words are one product; the last word names it
        else:
            kept.append((start, end, family))
    return kept


def derive_products(idea: str, target_market: str = "") -> list[ProductWord]:
    """The words in the idea that name what is sold, best first. A word that describes the customer's own
    situation is dropped: one that is also in the target market, or one that follows "enrolled in", "taking",
    "attending", "studying" or a possessive. A word after a delivery cue ("providing", "offering", "selling",
    "includes", "with", "of") goes first. A sales-channel word (website, platform, app) is never returned."""
    idea = str(idea or "")
    market = {_stem(w) for w in _tokens(target_market)}
    cue = _CUE_RX.search(idea)
    ranked: list[tuple[int, int, ProductWord]] = []
    for start, end, family in _product_matches(idea):
        text = _clean(idea[start:end]).lower()
        if gates.CHANNEL_RX.fullmatch(text):
            continue
        if _CONTEXT_RX.search(idea[:start]) or all(_stem(w) in market for w in _tokens(text)):
            continue
        after_cue = bool(cue and start >= cue.end()) or bool(_NEAR_CUE_RX.search(idea[:start]))
        search = re.sub(r"^digital ", "", text)
        ranked.append((0 if after_cue else 1, start, ProductWord(text, search, family)))
    out: list[ProductWord] = []
    for _, _, word in sorted(ranked, key=lambda r: r[:2]):
        if _stem(word.search) not in {_stem(w.search) for w in out}:
            out.append(word)
    return out[:MAX_PRODUCT_WORDS]


def derive_topic(idea: str, target_market: str = "", limit: int = 3) -> str:
    """The subject the product is about. The longest run of words that the idea and the target market share
    wins ("electrical engineering"); with none, the idea's first content words. Filler verbs, pronouns,
    website-building words and product words are never used."""
    idea = str(idea or "")
    product = {w for start, end, _ in _product_matches(idea) for w in _tokens(idea[start:end])}
    words = [w for w in _tokens(idea) if w not in _TOPIC_STOP and w not in product]
    market = [_stem(w) for w in _tokens(target_market) if w not in _TOPIC_STOP]
    for n in range(min(limit, len(words)), 0, -1):
        runs = [words[i:i + n] for i in range(len(words) - n + 1)]
        shared = [r for r in runs if any([_stem(w) for w in r] == market[j:j + n] for j in range(len(market) - n + 1))]
        if shared and n > 1:
            return " ".join(shared[0])
        if shared:  # only single words are shared: keep them in the idea's order
            return " ".join(list(dict.fromkeys(r[0] for r in shared))[:limit])
    return " ".join(list(dict.fromkeys(words))[:limit] or [w for w in _tokens(target_market) if w not in _TOPIC_STOP][:limit])


def marketplace_hosts(family: str, products: Sequence[str] = ()) -> tuple[str, ...]:
    """The marketplaces to search for a product family; study material gets the study-notes marketplaces."""
    if str(family or "") in ("", "Written content") and any(STUDY_RX.search(p) for p in products):
        return STUDY_HOSTS
    return MARKETPLACE_HOSTS.get(str(family or ""), DEFAULT_MARKETPLACE_HOSTS)


_CONNECTORS = frozenset("who whom that which working with of in at on and or for from to".split())


def _short_customer(customer: str, said: str) -> str:
    """The customer's leading words that the query does not already say, up to the first connecting word."""
    used = {_stem(w) for w in _tokens(said)}
    out: list[str] = []
    for w in customer.split():
        w = w.strip(".,;:!?()[]{}<>\"'&/|")  # the customer may be model text; only its words go into a query
        if not w:
            continue
        if w.lower() in _CONNECTORS:
            break
        if _stem(re.sub(r"[^a-z0-9]", "", w.lower())) not in used:
            out.append(w)
    return " ".join(out[:3]).lower()


def _query(text: str) -> str:
    """One query: single spaces, and a word said twice is kept once ("notion ... notion template")."""
    seen: set[str] = set()
    out: list[str] = []
    for w in _clean(text).split(" "):
        key = _stem(re.sub(r"[^a-z0-9]", "", w.lower())) if not w.lower().startswith("site:") else w.lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(w)
    return " ".join(out).strip()


def build_queries(terms: BriefTerms, marketplaces: Sequence[str] | None = None) -> list[Query]:
    """Queries filled from the brief, in priority order. No free-form model text is used.

    Problem (SAY) queries come first. The buying queries follow as three rungs: 1 intent queries for the first
    product word, 2 `site:` queries on marketplaces chosen by product family, 3 topic and product alone with
    no operator. `marketplaces` replaces the family's hosts when given."""
    customer, problem = _clean(terms.customer), _clean(terms.problem)
    topic = _clean(terms.topic)
    products = [p for p in dict.fromkeys(_clean(p) for p in (terms.products or (terms.product,))) if p]
    candidates: list[tuple[str, str, int]] = []
    if customer and problem:
        candidates += [
            (f"{customer} struggling with {problem}", PURPOSE_SAY, 0),
            (f"how do {customer} deal with {problem}", PURPOSE_SAY, 0),
        ]
    if problem:
        candidates.append((f"{problem} forum", PURPOSE_SAY, 0))
    if products:
        first = products[0]
        who = _short_customer(customer, f"{topic} {first}")
        candidates += [
            (_query(f"{topic} {first} price"), PURPOSE_PAY, 1),
            (_query(f"best {topic} {first}") + (f" for {who}" if who else ""), PURPOSE_PAY, 1),
            (_query(f"buy {topic} {first}"), PURPOSE_PAY, 1),
            (_query(f"{topic} {first} alternatives"), PURPOSE_PAY, 1),
        ]
        hosts = tuple(marketplaces) if marketplaces is not None else marketplace_hosts(terms.family, products)
        for i, host in enumerate(hosts[:MAX_MARKETPLACE_HOSTS]):
            candidates.append((_query(f"site:{host} {topic} {products[i % len(products)]}"), PURPOSE_MARKETPLACE, 2))
        for product in products:
            relaxed = _query(f"{topic} {product}")
            if len(relaxed.split()) > 1:
                candidates.append((relaxed, PURPOSE_PAY, 3))
    seen: set[str] = set()
    queries: list[Query] = []
    for text, purpose, rung in candidates:
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            queries.append(Query(text, purpose, rung))
    return queries


def accept_model_query(query: str, terms: BriefTerms) -> bool:
    """A model-suggested query needs three words and one word from the brief."""
    query = _clean(query)
    if len(query.split()) < 3:
        return False
    brief_words = content_words(" ".join([terms.customer, terms.problem, terms.product, terms.topic, *terms.products]))
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

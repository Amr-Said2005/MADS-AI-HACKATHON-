"""Relevance scoring of search hits before anything is fetched, and code-built relaxed queries.

Pure functions: no network, no model. A hit is scored from its title, snippet and URL against the
topic and product words of the search. Those words are passed in, or taken from the query text.
A hit that shares none of them scores 0 and is never fetched. For pay-evidence searches a price in
the title or snippet and a marketplace host add points, so priced offers are fetched first.

relaxed_queries() builds the retry queries for the fixed relaxation rounds (RELAX_ROUNDS): quotes
and search operators are stripped and the query is cut to its first content words.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence
from urllib.parse import urlparse

RELAX_ROUNDS = 2          # fixed: gather retries with relaxed queries at most this many times
RELAX_WORDS = (5, 3)      # content words kept in round 1 and round 2
THRESHOLD = 1             # a hit needs at least this score to be fetched

TITLE_POINTS = 2          # per term found in the title
TEXT_POINTS = 1           # per term found in the snippet or the URL
PRICE_POINTS = 2          # pay-evidence search: a price in the title or snippet
MARKETPLACE_POINTS = 2    # pay-evidence search: the page is on a marketplace host

MARKETPLACE_HOSTS = ("udemy.com", "coursera.org", "gumroad.com", "etsy.com", "teachable.com", "skillshare.com",
                     "podia.com", "payhip.com", "creativemarket.com", "teacherspayteachers.com", "thinkific.com",
                     "kajabi.com", "lemonsqueezy.com", "appsumo.com", "envato.com", "themeforest.net")

_FILLER = frozenset("""a an the and or of for to in on with that this these those which who whom whose is are was
were be been it its their his her our your my by from as at into about over under than then so such very can
will just how what when where why do does did not have has had you they them there some more most also any all""".split())
# Words that say what kind of page is wanted, not what it is about.
_INTENT = frozenset("""price prices pricing priced paid pay buy buying purchase cost costs subscription cheap
alternative alternatives best top review reviews forum forums struggling struggle deal dealing problem problems
help online free vs versus""".split())
_PAY_WORDS = frozenset("price prices pricing priced paid pay buy buying purchase cost costs subscription alternatives".split())
_MARKETPLACE_NAMES = frozenset(h.split(".")[0] for h in MARKETPLACE_HOSTS)

_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_OPERATOR = re.compile(r"(?:^|\s)-?(?:site|inurl|intitle|intext|filetype|ext|lang|language):\S+", re.IGNORECASE)
_PRICE = re.compile(
    r"(?:[$€£¥₹]\s?\d[\d,.]*"
    r"|\b\d[\d,.]*\s?(?:[$€£]|(?:USD|EUR|GBP|EGP|dollars?|euros?|pounds?)\b)"
    r"|\b(?:USD|EUR|GBP|EGP|US\$)\s?\d[\d,.]*"
    r"|\b\d[\d,.]*\s?(?:/|per|a)\s?(?:month|mo|year|yr|week|user|seat)\b)", re.IGNORECASE)


def stem(word: str) -> str:
    """A crude stem so that "notes" matches "note" and "invoicing" matches "invoice"."""
    w = word.lower()
    for suffix in ("ing", "ies", "es", "s", "e"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def strip_operators(query: str) -> str:
    """The query without quotes, site:/inurl:/... operators, boolean words and minus terms."""
    q = _OPERATOR.sub(" ", query or "")
    q = re.sub(r"[\"“”'()]", " ", q)
    q = re.sub(r"(?:^|\s)-\S+", " ", q)
    q = re.sub(r"\b(?:AND|OR|NOT)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def words(text: str) -> list[str]:
    """Lower-case words of three or more characters, in order, without filler words."""
    out: list[str] = []
    for w in _WORD.findall((text or "").lower()):
        if w not in _FILLER and w not in out:
            out.append(w)
    return out


def query_terms(query: str) -> list[str]:
    """The topic and product words of a query: its content words without intent words ("price",
    "forum", "best") and marketplace names. When nothing else is left, the content words themselves."""
    content = words(strip_operators(query))
    topical = [w for w in content if w not in _INTENT and w not in _MARKETPLACE_NAMES]
    return topical or content


def is_pay_query(query: str) -> bool:
    """True when the query looks for payment evidence: a price word or a marketplace name is in it."""
    found = set(words(strip_operators(query)))
    return bool(found & _PAY_WORDS or found & _MARKETPLACE_NAMES)


def host_of(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_marketplace(url: str) -> bool:
    host = host_of(url)
    return any(host == m or host.endswith("." + m) for m in MARKETPLACE_HOSTS)


def has_price(text: str) -> bool:
    return any(re.search(r"[1-9]", m.group(0)) for m in _PRICE.finditer(text or ""))


def score_hit(title: str, snippet: str, url: str, query: str = "", *, terms: Iterable[str] | None = None,
              pay: bool | None = None) -> int:
    """Score one search hit. 0 means off-topic: the hit shares no term with the search.

    terms: the brief's topic and product words; taken from the query when not given.
    pay:   whether this is a pay-evidence search; taken from the query when not given.
    """
    wanted = {stem(t) for t in (words(" ".join(terms)) if terms is not None else query_terms(query))}
    if not wanted:
        return 0
    parsed = urlparse(url or "")
    in_title = {stem(w) for w in words(title)}
    in_text = {stem(w) for w in words(snippet)} | {stem(w) for w in words(f"{parsed.hostname or ''} {parsed.path}")}
    score = 0
    for t in wanted:
        if t in in_title:
            score += TITLE_POINTS
        elif t in in_text:
            score += TEXT_POINTS
    if score == 0:
        return 0
    if pay if pay is not None else is_pay_query(query):
        if has_price(f"{title} {snippet}"):
            score += PRICE_POINTS
        if is_marketplace(url):
            score += MARKETPLACE_POINTS
    return score


def rank(scored: Sequence[tuple[int, object]], threshold: int = THRESHOLD) -> list[tuple[int, object]]:
    """(score, item) pairs at or above the threshold, best first; equal scores keep their order."""
    return sorted((p for p in scored if p[0] >= threshold), key=lambda p: -p[0])


def relax_query(query: str, max_words: int) -> str:
    """A looser form of a query: no quotes or operators, cut to its first `max_words` content
    words (topic words first, then the others in their original order)."""
    content = words(strip_operators(query))
    topical = query_terms(query)
    keep = (topical + [w for w in content if w not in topical])[:max_words]
    return " ".join(w for w in content if w in keep)


def relaxed_queries(queries: Sequence[str], round_number: int, tried: Iterable[str] = ()) -> list[str]:
    """The relaxed variants for one relaxation round (1-based), without repeats and without
    anything already tried. Returns [] past RELAX_ROUNDS."""
    if not (1 <= round_number <= RELAX_ROUNDS):
        return []
    done = {t.strip().lower() for t in tried} | {q.strip().lower() for q in queries}
    out: list[str] = []
    for q in queries:
        relaxed = relax_query(q, RELAX_WORDS[round_number - 1])
        if relaxed and relaxed.lower() not in done:
            done.add(relaxed.lower())
            out.append(relaxed)
    return out

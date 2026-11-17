"""Agents, one module per role. Each step function takes a RunContext and returns a StepResult.

A function decorated with @stubbed is not built yet: calling it raises NotImplementedError,
the orchestrator marks the step STUBBED and the run pauses there (status "blocked").
Remove the decorator only when the function does the real work.

Shared helpers for built agents live here:
- ask():    one schema-validated model call. The system prompt comes from prompts/<agent>.md;
            fetched pages go into the user message as fenced, untrusted data, never the system prompt.
- gather(): search, score the hits for relevance, then fetch the best through the run's registry.
            Only fetched pages can be cited.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

import search_relevance
import sources


def stubbed(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"STUBBED: {fn.__module__}.{fn.__name__} is not built yet")

    wrapper.__status__ = "STUBBED"  # type: ignore[attr-defined]
    return wrapper


T = TypeVar("T", bound=BaseModel)

_FILLER = set("""a an the and or of for to in on with that this these those which who whom whose is are was were be been
it its their his her our your my by from as at into about over under than then so such very can will just
helps help helping make makes making let lets get gets use using used new best easy simple people person""".split())
_PRODUCT_WORDS = set("template templates pack packs guide guides checklist checklists course courses mini mini-course ebook tool tools".split())
_STOPWORDS = _FILLER | _PRODUCT_WORDS

SKIP_DOMAINS = ("reddit.com", "youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com", "x.com",
                "twitter.com", "pinterest.com", "linkedin.com")


def keywords(text: str, limit: int = 8, product_words: bool = False) -> list[str]:
    """Content words of a text, in order, without filler words (and without product-type words unless asked)."""
    stop = _FILLER if product_words else _STOPWORDS
    out: list[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text or ""):
        lw = w.lower()
        if lw not in stop and lw not in out:
            out.append(lw)
    return out[:limit]


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def task_prompt(ctx, agent: str, task: str) -> str:
    """The role rules at the top of prompts/<agent>.md plus that file's "## Task: <task>" section."""
    head, *sections = re.split(r"^## Task: ", ctx.prompt(agent), flags=re.M)
    for s in sections:
        name, _, body = s.partition("\n")
        if name.strip() == task:
            return f"{head.strip()}\n\n## Task: {task}\n{body.strip()}"
    return head.strip()


def brief_block(ctx) -> str:
    b = ctx.brief
    lines = ["Founder brief:", f"- idea: {b['idea']}", f"- skills: {', '.join(b['skills']) or 'none listed'}",
             f"- budget_usd: {b['budget_usd']}", f"- hours_per_week: {b['hours_per_week']}",
             f"- target_market: {b['target_market']}", f"- values: {', '.join(b['values']) or 'none listed'}"]
    if ctx.instructions:
        lines += ["Founder instructions during this run:"] + [f"- {i}" for i in ctx.instructions]
    return "\n".join(lines)


def chosen_opportunity(ctx) -> dict:
    """The opportunity code picked in Phase 1 (id, title, customer, problem), plus the scorer's smallest version."""
    scoring = ctx.artifact("phase1_step2").get("data", {})
    opps = {o["id"]: o for o in ctx.artifact("phase1_step1").get("data", {}).get("opportunities", [])}
    chosen = dict(opps.get(str(scoring.get("pick") or ""), {}))
    chosen["smallest_version"] = scoring.get("smallest_version", "")
    return chosen


def focus_block(ctx) -> str:
    """What later phases build on: the chosen opportunity, its customer and its problem."""
    chosen, brief = chosen_opportunity(ctx), ctx.brief
    return (f"Chosen opportunity: {chosen.get('title') or brief['idea']}.\n"
            f"Customer: {chosen.get('customer') or brief['target_market']}.\n"
            f"Problem: {chosen.get('problem') or 'see the idea'}.\n"
            + (f"Smallest version (ASSUMPTION): {chosen['smallest_version']}.\n" if chosen.get("smallest_version") else ""))


# Requirement R-UNTRUSTED-TEXT: fetched pages go into the user message as fenced blocks; the
# system message is built only from the agent's prompt file, never from page text.
# Verified by: tests/test_agents.py::test_fetched_pages_are_fenced_and_never_in_the_system_prompt
def ask(ctx, agent: str, task: str, schema: type[T], body: str, *, pages: list | tuple = (),
        max_page_chars: int = 1400, max_tokens: int | None = None) -> T:
    """One structured model call. Raises llm.ModelOutputInvalid if the reply fails the schema twice."""
    parts = [brief_block(ctx), body]
    criteria = getattr(ctx, "plan_criteria", None)
    if criteria:  # makers work to the plan that was approved before they started
        parts.append("Pass criteria for this phase (your work is checked against them):\n"
                     + "\n".join(f"- {c.get('check')} Threshold: {c.get('threshold')}" for c in criteria))
    if ctx.feedback:
        parts.append("Feedback on the previous attempt (fix this):\n" + ctx.feedback)
    for i, page in enumerate(pages, start=1):
        parts.append(f"[Page {i}]\n" + page.as_prompt_block(ctx.registry.delimiter, max_chars=max_page_chars))
    messages = [{"role": "system", "content": task_prompt(ctx, agent, task)},
                {"role": "user", "content": "\n\n".join(parts)}]
    obj, _ = ctx.llm.structured(messages, schema, agent=agent, max_tokens=max_tokens)
    return obj


def web_search(query: str, *, limit: int = 5, searxng_url: str | None = None) -> list[sources.SearchHit]:
    """Search adapters in sources.py (SearXNG first, then keyless APIs). Hits are leads, not citations."""
    return sources.search(query, limit=limit, searxng_url=searxng_url)


def _skipped(d: str) -> bool:
    return any(d == s or d.endswith("." + s) for s in SKIP_DOMAINS)


def collect_hits(ctx, queries: list[str], per_query: int) -> list[tuple[int, str, sources.SearchHit]]:
    """Search every query in order and score each hit before anything is fetched. Returns
    (score, query, hit) for the hits worth fetching, best first; equal scores keep search order.
    Off-topic hits (score 0) and hits on sites that are not used are recorded in the search
    report and left out. A URL found by several queries is kept once, with its best score.

    Scoring applies to hits from the search adapters in sources.ADAPTER_NAMES. A hit that names
    no known adapter (a lead handed in by the caller) carries no searchable text to score, so it
    is kept at the lowest passing score."""
    best: dict[str, tuple[int, str, sources.SearchHit]] = {}
    for q in queries:
        try:
            hits = web_search(q, limit=per_query, searxng_url=ctx.settings.searxng_url or None)
        except Exception as exc:  # a search adapter problem must not fail the step; fewer sources is the honest result
            sources.report_query(q, "search", f"{type(exc).__name__}: {str(exc)[:120]}")
            hits = []
        for h in hits:
            d = domain(h.url)
            if not d or _skipped(d):
                sources.report_hit(q, h, sources.HIT_SKIPPED)
                continue
            score = search_relevance.score_hit(h.title, h.snippet, h.url, q)
            if score < search_relevance.THRESHOLD and (h.adapter not in sources.ADAPTER_NAMES
                                                       or not search_relevance.query_terms(q)):
                # also a query with no words the scorer can read (for example one not in Latin
                # script): its hits cannot be scored, so they are not dropped
                score = search_relevance.THRESHOLD
            if score < search_relevance.THRESHOLD:
                if h.url not in best:
                    sources.report_hit(q, h, sources.HIT_OFF_TOPIC, "shares no topic word with the query", score)
                continue
            if h.url not in best or score > best[h.url][0]:
                best[h.url] = (score, q, h)
    return search_relevance.rank(list(best.values()))


def gather(ctx, queries: list[str], *, want: int, per_query: int = 6, have: list | tuple = ()) -> list:
    """Search the queries, rank the hits by relevance, and fetch the best first through the
    registry until `want` pages from different domains are in hand. Off-topic hits are never
    fetched, so they are never registered. Pages that fail to fetch (blocked, empty, non-public)
    are skipped and the reason goes into the search report (sources.search_report_markdown()).

    When fewer than `want` pages were fetched after all queries, the search is repeated with
    relaxed queries built by code, for a fixed number of rounds (search_relevance.RELAX_ROUNDS)."""
    pages = list(have)
    seen = {domain(p.url) for p in pages}
    tried: list[str] = []
    batch = list(queries)
    try:
        for round_number in range(search_relevance.RELAX_ROUNDS + 1):
            if len(pages) >= want or not batch:
                break
            tried += batch
            for score, q, h in collect_hits(ctx, batch, per_query):
                d = domain(h.url)
                if d in seen:
                    sources.report_hit(q, h, sources.HIT_DUPLICATE, score=score)
                    continue
                if len(pages) >= want:
                    sources.report_hit(q, h, sources.HIT_KEPT, "enough pages in hand", score)
                    continue
                try:
                    page = ctx.registry.fetch(h.url)
                except sources.FetchError as exc:
                    sources.report_hit(q, h, sources.HIT_FAILED, sources.fetch_failure_reason(exc), score)
                    if "source budget" in str(exc):
                        return pages
                    continue
                sources.report_hit(q, h, sources.HIT_FETCHED, score=score)
                seen.add(d)
                pages.append(page)
            batch = search_relevance.relaxed_queries(list(queries), round_number + 1, tried)
    finally:
        sources.note_gather_summary()
    return pages


def page_url(pages: list, number: int | None) -> str | None:
    """The URL of [Page number] as shown to the model, or None if the model named a page that does not exist."""
    if number is None or not (1 <= number <= len(pages)):
        return None
    return pages[number - 1].url


def cite(ctx, text: str, url: str | None) -> str:
    """A markdown bullet: a link for a page this run fetched, else the ASSUMPTION label."""
    return sources.claim_line(text, url if url and ctx.registry.is_registered(url) else None)

"""Phase 1 researcher: opportunities (step 1) and market/competitors (step 3).

Both steps search first (sources.search adapters), fetch hits through the run's registry,
and then make ONE model call over the fetched pages. Step 1 lists the evidence that the
founder's problem exists (it counts for every opportunity) and then 2-4 opportunities, each a
customer segment with that problem. The model points at pages by number;
code turns a page number into its URL, so the model never types a URL. A statement with no
page behind it is printed and counted as ASSUMPTION.

Source tags: SAY = a page where someone describes the problem; PAY = a page showing that
someone already pays for a comparable solution. The model proposes PAY; code keeps the tag
only when it finds a price in the page's text (sources.find_price).

Speed: while step 1 runs, the searches of the market step are already running in the background
(search hits only; nothing is fetched early). Pages the founder provided and the run already fetched
come first and count toward the pages a step wants; when they cover the step, it does not search.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Literal

from pydantic import BaseModel, Field

from agents import PER_QUERY, SearchPrefetch, ask, cite, gather, keywords, page_url
from llm import ShortText
import gates
import market_research as mr
import sources
from sources import Claim

logger = logging.getLogger(__name__)

SAY_PAGES = 6
PAY_PAGES = 4

# Which of the founder's fetched pages each step reads first, by the kind the founder gave them.
SAY_FOUNDER_KINDS = ("problem", "traction")
PAY_FOUNDER_KINDS = ("pay", "traction")
SEARCH_SKIPPED = "search skipped: the founder's evidence covers this step"
STATED_PREFIX = "Founder-stated, not a source: "
MAX_STATEMENTS = 6

PREFETCH_JOIN_SECONDS = 2.0   # how long the market step waits for searches that are still running
PREFETCH_MAX_SECONDS = 45.0   # after this a background search starts no new request
PREFETCH_KEPT = 8             # runs whose early searches are kept at once
_prefetches: dict[str, SearchPrefetch] = {}   # run id -> the market step's early searches
_prefetch_lock = threading.Lock()


class Evidence(BaseModel):
    page: int = Field(ge=1, description="number of the page that shows it")
    text: ShortText = Field(description="one sentence: what that page shows about the founder's problem")


class Opportunity(BaseModel):
    title: ShortText
    customer: ShortText = Field(description="a specific segment of the founder's customers")
    problem: ShortText = Field(description="the founder's problem as this segment has it")
    pages: list[int] = Field(default_factory=list, max_length=6, description="page numbers from `evidence` that support this")
    assumptions: list[ShortText] = Field(default_factory=list, max_length=2,
                                         description="statements this opportunity relies on that no page shows")


class Opportunities(BaseModel):
    evidence: list[Evidence] = Field(default_factory=list, max_length=6)
    opportunities: list[Opportunity] = Field(min_length=2, max_length=4)


class MarketClaim(BaseModel):
    page: int = Field(ge=1, description="number of the page that shows this")
    kind: Literal["PAY", "market"] = Field(description="PAY only if the page shows a price or a paid comparable product")
    text: ShortText


class Competitor(BaseModel):
    name: ShortText = Field(description="the organisation or product, as the page names it")
    page: int = Field(ge=1, description="number of the fetched page that shows it")
    kind: Literal["direct", "unpriced", "free_alternative", "not_a_competitor"]
    what_they_offer: ShortText
    quote: ShortText = Field(description="a few words copied exactly from that page")


class Market(BaseModel):
    claims: list[MarketClaim] = Field(default_factory=list, max_length=8,
                                      description="what the fetched pages show; every entry names its page")
    competitors: list[Competitor] = Field(default_factory=list, max_length=6,
                                          description="who sells or gives away something the same customer could use instead")
    gaps: list[ShortText] = Field(default_factory=list, max_length=2,
                                  description="what existing solutions seem to miss; these are assumptions")


def _derived(ctx) -> list[mr.ProductWord]:
    b = ctx.brief
    return mr.derive_products(b["idea"], b["target_market"])


def product_words(ctx) -> list[str]:
    """Short product words for the queries, best first: what the idea says is delivered (code reads it from
    the idea), else the assessor's product word and the name of its product family."""
    derived = [w.search for w in _derived(ctx)]
    if derived:
        return derived
    d = ctx.artifact("feasibility").get("data", {})
    family = gates.family_of(str(d.get("family") or ""))
    words = [str(d.get("product_type") or "").strip(), family.noun if family else ""]
    return [w for w in dict.fromkeys(words) if w and w != "digital product"][:2] or [" ".join(keywords(ctx.brief["idea"], 3, product_words=True))]


def brief_terms(ctx, product: str = "", shift: int = 0) -> mr.BriefTerms:
    """The brief's words for the queries: the assessor's customer and problem, and the product words, product
    family and topic that code reads from the idea. `shift` moves another product word to the front."""
    d = ctx.artifact("feasibility").get("data", {})
    b = ctx.brief
    derived = _derived(ctx)
    products = product_words(ctx)
    if shift and len(products) > 1:
        products = products[shift % len(products):] + products[:shift % len(products)]
    if product:
        products = [product] + [p for p in products if p != product]
    family = next((w.family for w in derived if w.search == products[0]), "") or str(d.get("family") or "")
    return mr.BriefTerms(customer=str(d.get("customer") or b["target_market"]), problem=str(d.get("problem") or ""),
                         product=products[0], topic=mr.derive_topic(b["idea"], b["target_market"]),
                         products=tuple(products), family=family)


def built_queries(ctx, purposes: tuple[str, ...]) -> list[str]:
    """Queries built by code from the brief's words, best first. On a repeat the next product word leads, so
    the queries are new and the strongest kind still runs first. The model may add two problem queries; one
    with fewer than three words or with no word from the brief is rejected."""
    terms = brief_terms(ctx, shift=int(getattr(ctx, "repeats", 0) or 0))
    out = [q for q in mr.build_queries(terms) if q.purpose in purposes]
    if mr.PURPOSE_SAY in purposes:
        suggested = [str(t) for t in ctx.artifact("feasibility").get("data", {}).get("search_terms", [])]
        out = mr.add_model_queries(out, suggested, terms)
    return [q.text for q in out]


def _terms(ctx) -> list[str]:
    """Search phrases: the assessor's, else keywords from the brief."""
    terms = [t for t in ctx.artifact("feasibility").get("data", {}).get("search_terms", []) if str(t).strip()]
    if not terms:
        b = ctx.brief
        terms = [" ".join(keywords(f"{b['target_market']} {b['idea']}", 7))]
    return terms[:4]


def _sources_md(pages: list, queries: list[str]) -> list[str]:
    lines = ["## Sources fetched in this run", ""]
    lines += [f"{i}. [{(p.title or p.url)[:90]}]({p.url})" for i, p in enumerate(pages, start=1)] or ["None could be fetched."]
    return lines + ["", "Search queries used: " + ("; ".join(f"`{q}`" for q in queries) or f"none ({SEARCH_SKIPPED})")]


def _founder_items(ctx) -> list[dict]:
    """The founder's evidence items of this run; [] when there are none or the record has another shape."""
    try:
        items = (ctx.rec.artifacts.get("founder_evidence") or {}).get("items", [])
    except AttributeError:
        return []
    return [e for e in items if isinstance(e, dict)] if isinstance(items, (list, tuple)) else []


def founder_pages(ctx, kinds: tuple[str, ...], limit: int) -> list:
    """Pages the founder provided that this run fetched (status "fetched"), oldest first, at most `limit`.
    Only a page that is in the run's registry with its text is returned, so each one is citable."""
    pages: list = []
    for e in _founder_items(ctx):
        url = e.get("url")
        if e.get("status") != "fetched" or str(e.get("kind") or "").lower() not in kinds or not isinstance(url, str) or not url:
            continue
        page = getattr(ctx.registry, "pages", {}).get(url)
        if page is not None and getattr(page, "text", "") and all(page.url != p.url for p in pages):
            pages.append(page)
    return pages[:max(0, limit)]


def founder_statements(ctx) -> str:
    """What the founder stated without a fetched page behind it, as prompt lines. It is context for the
    model only: it has no page number, so no claim can cite it."""
    said: list[str] = []
    for e in _founder_items(ctx):
        text = " ".join(str(e.get("statement") or "").split())[:300]
        if text and e.get("status") != "fetched" and text not in said:
            said.append(text)
    if not said:
        return ""
    return ("\nThe founder also stated the following. It is context only: it is not a page, it has no page number, "
            "and nothing may cite it or report it as something a page shows.\n"
            + "\n".join(STATED_PREFIX + t for t in said[:MAX_STATEMENTS]))


def _market_queries(ctx) -> list[str]:
    """The market step's queries. They are built from the brief and the feasibility check only, so they
    are known before step 1 has produced anything."""
    # PAY evidence is about comparable products, so these queries keep the product words of the idea.
    product = " ".join(keywords(ctx.brief["idea"], 6, product_words=True))
    queries = built_queries(ctx, (mr.PURPOSE_PAY, mr.PURPOSE_MARKETPLACE))
    return queries[:mr.MAX_QUERIES] or [f"{product} price", f"{_terms(ctx)[0]} pricing"]


def _run_key(ctx) -> str:
    return str(getattr(getattr(ctx, "rec", None), "run_id", "") or "")


def _start_market_prefetch(ctx) -> None:
    """Start the market step's searches in the background. Whatever goes wrong here is logged and
    ignored: the market step then searches as usual."""
    try:
        key = _run_key(ctx)
        if (not key or sources.search_workers() <= 1
                or os.environ.get("SEARCH_PREFETCH", "true").strip().lower() == "false"):
            return
        want = min(PAY_PAGES, ctx.settings.max_sources)
        if want <= 0 or len(founder_pages(ctx, PAY_FOUNDER_KINDS, want)) >= want:
            return  # that step will not search
        started = SearchPrefetch(_market_queries(ctx), per_query=PER_QUERY, searxng_url=ctx.settings.searxng_url or None,
                                 max_seconds=PREFETCH_MAX_SECONDS)
        with _prefetch_lock:
            dropped = [_prefetches.pop(key)] if key in _prefetches else []
            _prefetches[key] = started
            while len(_prefetches) > PREFETCH_KEPT:
                dropped.append(_prefetches.pop(next(iter(_prefetches))))
        for old in dropped:
            old.cancel()
    except Exception as exc:
        logger.info("market search prefetch not started: %s", exc)


def _take_market_prefetch(ctx) -> dict:
    """The early searches that finished, {query: outcome}; {} when there are none. Waits at most
    PREFETCH_JOIN_SECONDS and never raises."""
    try:
        with _prefetch_lock:
            started = _prefetches.pop(_run_key(ctx), None)
        return started.take(PREFETCH_JOIN_SECONDS) if started is not None else {}
    except Exception as exc:
        logger.info("market search prefetch not used: %s", exc)
        return {}


def find_opportunities(ctx):
    """Search public pages for the brief's problem; return the evidence and 2-4 opportunities built on it."""
    from orchestrator import StepResult

    _start_market_prefetch(ctx)  # the market step's searches run while this step works
    terms = _terms(ctx)
    # New queries only when the gate asked for a repeat. A rework for a checker defect keeps the same
    # queries, so the pages (cached by the registry) and the evidence stay the same.
    queries = built_queries(ctx, (mr.PURPOSE_SAY,))
    if ctx.repeats:  # the gate asked for a repeat: the same words, asked another way
        t = brief_terms(ctx)
        queries = [f"{t.problem} advice for {t.customer}", f"why is {t.problem} hard", f"{t.customer} {t.problem} tips"] + queries
    queries = queries[:6] or [f"{x} problem" for x in terms][:6]
    want = min(SAY_PAGES, ctx.settings.max_sources)
    mine = founder_pages(ctx, SAY_FOUNDER_KINDS, want)
    if mine and len(mine) >= want:
        pages, queries = mine, []
        sources.report_note(f"find_opportunities: {SEARCH_SKIPPED}")
    else:
        pages = gather(ctx, queries, want=want, have=mine)
    body = ("First list the evidence: for each fetched page that shows a real person or organisation describing the "
            "founder's problem, one sentence on what it shows. Skip pages that do not show it. Then give 2 to 4 "
            "opportunities, each a specific customer segment with that problem. "
            + (f"{len(pages)} fetched page(s) follow." if pages
               else "No page could be fetched, so `evidence` must be empty and every opportunity rests on assumptions.")
            + founder_statements(ctx))
    o = ask(ctx, "researcher", "find_opportunities", Opportunities, body, pages=pages)

    claims: list[Claim] = []
    lines = ["# Opportunities", "", f"Idea: {ctx.brief['idea']}", "", "## Evidence that the problem exists", ""]
    cited: dict[int, str] = {}
    for e in o.evidence:
        url = page_url(pages, e.page)
        claims.append(Claim(e.text, url, "SAY"))  # group "": evidence for the problem counts for every opportunity
        if url:
            cited[e.page] = url
        lines.append(cite(ctx, e.text, url))
    if not o.evidence:
        lines.append("No fetched page shows the problem.")
    lines.append("")
    opps = []
    for i, opp in enumerate(o.opportunities, start=1):
        oid = f"O{i}"
        support = [n for n in dict.fromkeys(opp.pages) if n in cited]
        opps.append({"id": oid, "title": opp.title, "customer": opp.customer, "problem": opp.problem,
                     "supported_by": [cited[n] for n in support]})
        lines += [f"## {oid}: {opp.title}", "", f"- Customer: {opp.customer}", f"- Problem: {opp.problem}",
                  "- Supported by: " + (", ".join(cited[n] for n in support) or f"no fetched page. {sources.ASSUMPTION}")]
        for a in opp.assumptions:
            claims.append(Claim(a, None, "SAY", group=oid))
            lines.append(cite(ctx, a, None))
        lines.append("")
    lines += _sources_md(pages, queries)
    return StepResult("\n".join(lines) + "\n", claims=claims,
                      data={"opportunities": opps, "queries": queries, "pages": [p.url for p in pages]})


def market_research(ctx):
    """Competitors and what people already pay for comparable solutions (PAY claims)."""
    from orchestrator import StepResult

    pick = ctx.artifact("phase1_step2").get("data", {})
    opps = {o["id"]: o for o in ctx.artifact("phase1_step1").get("data", {}).get("opportunities", [])}
    chosen = opps.get(str(pick.get("pick") or ""), {})
    queries = _market_queries(ctx)
    early = _take_market_prefetch(ctx)  # taken even when unused, so the background searches stop
    want = min(PAY_PAGES, ctx.settings.max_sources)
    mine = founder_pages(ctx, PAY_FOUNDER_KINDS, want)
    if mine and len(mine) >= want:
        pages, queries = mine, []
        sources.report_note(f"market_research: {SEARCH_SKIPPED}")
    else:
        pages = gather(ctx, queries, want=want, have=mine, prefetched=early)
    body = ((f"Chosen opportunity: {chosen.get('title', 'not chosen')} (customer: {chosen.get('customer', '-')}; "
             f"problem: {chosen.get('problem', '-')}).\n" if chosen else "")
            + "Describe the market for this opportunity: existing solutions and what people already pay. "
            + (f"{len(pages)} fetched page(s) follow. Every claim reports what one page shows and gives that page's "
               "number. Use kind PAY only for a page that shows a price or a paid comparable product. Then list the "
               "competitors the pages show, each with a few words copied exactly from its page." if pages
               else "No page could be fetched, so `claims` must be empty.")
            + founder_statements(ctx))
    m = ask(ctx, "researcher", "market_research", Market, body, pages=pages)

    claims: list[Claim] = []
    lines = ["# Market and competitors", "", f"For: {chosen.get('title') or ctx.brief['idea']}", "",
             "## What exists and what people pay", ""]
    for c in m.claims:
        url = page_url(pages, c.page)
        claims.append(Claim(c.text, url, c.kind))  # stamp_claims confirms or downgrades the PAY tag and logs it
        price = ctx.registry.price_in(url) if c.kind == "PAY" else None
        text = f"[{'PAY' if price else 'market'}] {c.text.rstrip('. ')}"
        if price:  # the note goes before the source, so the line still ends with its URL
            text += f". Price found on the page by code: {price}"
        elif c.kind == "PAY" and url:
            text += ". The model tagged this PAY, but code found no price on the page, so it is not counted as payment evidence"
        lines.append(cite(ctx, text, url))
    # Competitors: every entry is checked against the page it cites. One whose quote is not on the page, whose
    # page is not a general web page, or whose name is generic is dropped, and the drop is logged.
    checked = mr.verify_competitors(
        [mr.CompetitorEntry(c.name, c.page, c.kind, c.what_they_offer, c.quote) for c in m.competitors],
        [mr.Page(i, p.url, p.text, sources.page_role(p.url), p.title or "") for i, p in enumerate(pages, start=1)])
    for d in checked.dropped:
        ctx.log.record("researcher", "competitor_dropped", outcome="dropped", reason=f"{d['name']}: {d['reason']}"[:300])
    lines += ["", "## Competitors", "", mr.render_competitors(checked)]
    if m.gaps:
        lines += ["", "## Possible gaps", ""]
        for g in m.gaps:
            claims.append(Claim(g, None, "market"))
            lines.append(cite(ctx, g, None))
    provided = sources.founder_evidence_markdown(_founder_items(ctx))
    if provided:
        lines += ["", provided.rstrip("\n")]
    lines += [""] + _sources_md(pages, queries)
    return StepResult("\n".join(lines) + "\n", claims=claims, data={"queries": queries, "pages": [p.url for p in pages],
                                                                          "competitors": [c.name for c in checked.kept]})

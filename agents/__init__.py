"""Agents, one module per role. Each step function takes a RunContext and returns a StepResult.

A function decorated with @stubbed is not built yet: calling it raises NotImplementedError,
the orchestrator marks the step STUBBED and the run pauses there (status "blocked").
Remove the decorator only when the function does the real work.

Shared helpers for built agents live here:
- ask():    one schema-validated model call. The system prompt comes from prompts/<agent>.md;
            fetched pages go into the user message as fenced, untrusted data, never the system prompt.
- gather(): search, then fetch hits through the run's registry. Only fetched pages can be cited.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

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


def gather(ctx, queries: list[str], *, want: int, per_query: int = 6, have: list | tuple = ()) -> list:
    """Search each query and fetch hits through the registry until `want` pages from different
    domains are in hand. Pages that fail to fetch (blocked, empty, non-public) are skipped."""
    pages = list(have)
    seen = {domain(p.url) for p in pages}
    for q in queries:
        if len(pages) >= want:
            break
        try:
            hits = web_search(q, limit=per_query, searxng_url=ctx.settings.searxng_url or None)
        except Exception:  # a search adapter problem must not fail the step; fewer sources is the honest result
            hits = []
        for h in hits:
            if len(pages) >= want:
                break
            d = domain(h.url)
            if not d or d in seen or any(d == s or d.endswith("." + s) for s in SKIP_DOMAINS):
                continue
            try:
                page = ctx.registry.fetch(h.url)
            except sources.FetchError as exc:
                if "source budget" in str(exc):
                    return pages
                continue
            seen.add(d)
            pages.append(page)
    return pages


def page_url(pages: list, number: int | None) -> str | None:
    """The URL of [Page number] as shown to the model, or None if the model named a page that does not exist."""
    if number is None or not (1 <= number <= len(pages)):
        return None
    return pages[number - 1].url


def cite(ctx, text: str, url: str | None) -> str:
    """A markdown bullet: a link for a page this run fetched, else the ASSUMPTION label."""
    text = text.rstrip(". ")
    if url and ctx.registry.is_registered(url):
        return f"- {text} ([source]({url}))"
    return f"- {text} **{sources.ASSUMPTION}**"

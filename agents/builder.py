"""Phase 3 builder: text digital products only (guide, template pack, checklist, mini-course).

mvp_spec:      one model call. The smallest version as 3-5 sections, plus what is left out.
               The acceptance tests are fixed by code (ACCEPTANCE) and printed in the spec.
build_product: one model call writes every section; then code runs the acceptance tests on
               the text. data["tests"] maps test name -> bool, and the Phase 3 gate passes
               only if every test passes. A failed test sends the work back to the makers.
user_docs:     one model call. Quick start, a short FAQ and what the product does not do.

Review loop (review_loop.py, REVIEW_STEPS revisions at most, 0 = off): after the product or the user docs
are drafted, code checks the draft (product_defects, docs_defects). A clean draft costs no extra call. A
draft with defects goes back to the model with the numbered defect list; the revision is kept only when
code finds fewer defects in it. Every revision call is one decision log entry, review_<what>_step<n>.
Whatever the loop does, code then removes "Source:" lines that carry no fetched URL and turns headings
inside a section body into bold text, so the product has exactly one "## " heading per section.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

import gates
import guardrails
import review_loop
from agents import ask, chosen_opportunity, focus_block
from llm import LLMError, ModelOutputInvalid, ShortText

ACCEPTANCE = gates.PRODUCT_ACCEPTANCE  # fixed by code, printed in the spec, run on the product
MIN_SECTION_CHARS = gates.MIN_SECTION_CHARS
MAX_PRODUCT_CHARS = gates.MAX_PRODUCT_CHARS
_PLACEHOLDER = re.compile(r"lorem ipsum|\bTODO\b|\bTBD\b|\[insert[^\]]*\]|\[your [^\]]*\]|coming soon", re.IGNORECASE)

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]
SectionBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=150, max_length=6000)]


class SpecSection(BaseModel):
    title: Title
    purpose: ShortText = Field(description="what this section gives the customer")


class Spec(BaseModel):
    product_name: Title
    format: ShortText = Field(description="how the product is delivered, e.g. a Markdown guide with three tables to copy")
    sections: list[SpecSection] = Field(min_length=3, max_length=5)
    out_of_scope: list[ShortText] = Field(min_length=1, max_length=3, description="what the smallest version leaves out")


class ProductSection(BaseModel):
    title: Title = Field(description="exactly one of the spec's section titles")
    body: SectionBody = Field(description="the full text of the section, in Markdown")


class Product(BaseModel):
    sections: list[ProductSection] = Field(min_length=3, max_length=5)


class Faq(BaseModel):
    question: ShortText
    answer: ShortText


class Docs(BaseModel):
    quick_start: list[ShortText] = Field(min_length=3, max_length=6, description="steps to start using the product")
    faq: list[Faq] = Field(min_length=2, max_length=4)
    limits: list[ShortText] = Field(min_length=1, max_length=3, description="what the product does not do")


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _strip_heading(title: str, body: str) -> str:
    """Drop a first line that only repeats the section title as a heading; code prints the heading."""
    lines = body.lstrip().splitlines()
    if lines and lines[0].lstrip().startswith("#") and _norm(lines[0]) == _norm(title):
        return "\n".join(lines[1:]).lstrip()
    return body


# ---------------------------------------------------------------- code checks and clean-up for the review loop

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SOURCE_LINE = re.compile(r"^\s*(?:[-*+>]\s*)?[*_]*(?:sources?|references?)[*_]*\s*:", re.IGNORECASE)
_LINK = re.compile(r"https?://[^\s)>\]]+")
_DESCRIBES = re.compile(
    r"\b(?:this (?:section|module|lesson|chapter|guide|part) (?:offers|provides|covers|will|contains|explains|gives|includes|"
    r"describes|introduces|presents|outlines)|the (?:notes|guide|lessons?|course|templates?|checklist|section|module|pack) will"
    r"|(?:users|learners|students|readers|customers) (?:can|will)|you will (?:learn|find|discover))\b", re.IGNORECASE)
_CONCRETE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S|^\s*\|.+\|\s*$|```|\$[^$\n]+\$|\S\s=\s\S|\b(?:for example|worked example|e\.g\.)"
                       r"|\bexample\s*\d*:", re.IGNORECASE | re.MULTILINE)
# what a text product does not have unless its spec says so; the price is set in a later phase
_DOCS_ABSENT = (
    ("an account or sign-in", re.compile(r"\b(?:sign(?:ing)?[- ]?(?:in|up)|log(?:ging)?[- ]?in|accounts?|register(?:ing)?|passwords?)\b", re.IGNORECASE)),
    ("a PDF download", re.compile(r"\bpdfs?\b", re.IGNORECASE)),
    ("an app", re.compile(r"\b(?:apps?|app store|android|ios)\b", re.IGNORECASE)),
    ("link sharing", re.compile(r"\bshar(?:e|es|ed|ing|able)\b[^.\n]{0,40}\blinks?\b|\blinks?\b[^.\n]{0,40}\bshar(?:e|ed|ing)\b", re.IGNORECASE)),
    ("a subscription", re.compile(r"\bsubscri(?:be|bes|bed|bing|ption|ptions|ber|bers)\b", re.IGNORECASE)),
    ("a price or \"free\"", re.compile(r"[$\u20ac\u00a3]\s?\d|\b\d+(?:\.\d+)?\s?(?:usd|dollars?|euros?)\b|\bpric(?:e|es|ed|ing)\b"
                                       r"|\bfree(?: of charge)?\b(?!\s+(?:plan|tier|version)\b)", re.IGNORECASE)),
)


def _body_lines(body: str):
    """Each line with whether it is inside a code fence (a "# comment" in a code block is not a heading)."""
    fence = ""
    for line in body.splitlines():
        mark = line.lstrip()[:3]
        if mark in ("```", "~~~") and fence in ("", mark):
            fence = "" if fence else mark
            yield line, True
        else:
            yield line, bool(fence)


def _has_source(line: str, registry=None) -> bool:
    """True if the line carries a URL; with a registry, a URL this run fetched (others are removed downstream)."""
    urls = [u.rstrip(".,;:") for u in _LINK.findall(line)]
    return any(registry.is_registered(u) for u in urls) if registry is not None else bool(urls)


def inner_headings(body: str) -> list[str]:
    return [m.group(1) for line, fenced in _body_lines(body) if not fenced and (m := _HEADING.match(line))]


def unsourced_lines(body: str, registry=None) -> list[str]:
    return [line for line, fenced in _body_lines(body)
            if not fenced and _SOURCE_LINE.match(line) and not _has_source(line, registry)]


def demote_headings(body: str) -> str:
    """Headings inside a section body become bold lines: only the spec's sections are "## " headings."""
    out = []
    for line, fenced in _body_lines(body):
        m = None if fenced else _HEADING.match(line)
        if m:
            text = re.sub(r"^(\*\*|\*)(.+)\1$", r"\2", m.group(1).strip()).strip()  # "## **Step**" -> "Step"
            if text:
                out.append(f"**{text}**")
        else:
            out.append(line)
    return "\n".join(out).strip()


def strip_unsourced(body: str, registry=None) -> tuple[str, int]:
    """Remove "Source:" and "Reference:" lines that name no fetched URL. Returns the body and how many went."""
    kept, removed = [], 0
    for line, fenced in _body_lines(body):
        if not fenced and _SOURCE_LINE.match(line) and not _has_source(line, registry):
            removed += 1
        else:
            kept.append(line)
    return (re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), removed) if removed else (body, 0)


def product_defects(spec_titles: list[str], sections: list[dict], registry=None) -> list[str]:
    """What code finds wrong with a product draft, as short strings the model can act on."""
    out = []
    have = {_norm(s["title"]) for s in sections}
    wanted = {_norm(t) for t in spec_titles}
    out += [f'the section "{t}" from the spec is missing' for t in spec_titles if _norm(t) not in have]
    total = sum(len(s["title"]) + len(s["body"]) for s in sections)
    if total > MAX_PRODUCT_CHARS:  # an acceptance test: a revision that grows past it must not replace the draft
        out.append(f"the product has {total} characters; the limit is {MAX_PRODUCT_CHARS}: shorten the longest sections")
    for s in sections:
        title, body = s["title"], s["body"]
        if wanted and _norm(title) not in wanted:
            out.append(f'the section "{title}" is not in the spec: use the spec\'s titles only')
        if len(body) < MIN_SECTION_CHARS:
            out.append(f'the section "{title}" has {len(body)} characters; it needs at least {MIN_SECTION_CHARS} of real content')
        said = _DESCRIBES.search(body)
        if said and not _CONCRETE.search(body):
            out.append(f'the section "{title}" describes its content ("{said.group(0)}") instead of containing it: '
                       "write the content itself with a list, a table, a formula or a worked example")
        if inner_headings(body):
            out.append(f'the section "{title}" has headings inside its body: use bold text, not "#" lines')
        if unsourced_lines(body, registry):
            out.append(f'the section "{title}" has a "Source:" or "Reference:" line without a fetched URL: remove the line')
        hit = _PLACEHOLDER.search(body)
        if hit:
            out.append(f'the section "{title}" has placeholder text ("{hit.group(0)}"): write the real content')
    return out


def docs_defects(docs: dict, known: str) -> list[str]:
    """User docs that mention things the product does not have. `known` is the spec and the product text:
    a thing named there is allowed. The limits list is not checked: it says what the product does not do."""
    parts = [("quick_start", x) for x in docs.get("quick_start", [])]
    for f in docs.get("faq", []):
        parts += [("faq", f.get("question", "")), ("faq", f.get("answer", ""))]
    out = []
    for label, rx in _DOCS_ABSENT:
        if rx.search(known):
            continue
        for where, text in parts:
            m = rx.search(text)
            if m:
                out.append(f'{where} mentions {label} ("{m.group(0)}"), which the product does not have: remove it')
                break
    return out


def _review(ctx, what: str, schema, body: str, draft, to_draft, checks) -> review_loop.Review:
    """The fixed-step review of one draft. Each revision is one model call through ask(), so it counts in the
    run budget, and one decision log entry named review_<what>_step<n>."""
    def revise(current, defects, step):
        message = (body + "\n\nYour draft, as JSON:\n" + json.dumps(current, ensure_ascii=False)
                   + "\n\nDefects found by code in this draft. Fix every one and keep the rest of the text:\n"
                   + review_loop.numbered(defects))
        try:
            return to_draft(ask(ctx, "builder", f"review_{what}", schema, message))
        except (ModelOutputInvalid, LLMError):  # a failed revision call never costs the step its valid draft
            return None

    def log(record):
        outcome = "revision kept" if record["kept"] else "revision dropped" if record["revised"] else "no valid reply"
        ctx.log.record("builder", f"review_{what}_step{record['step']}", model=ctx.llm.model_label, outcome=outcome,
                       reason=(f"{len(record['defects'])} defects before, {record['defects_after']} after "
                               "(cost counted in the step's own entry): " + "; ".join(record["defects"][:3]))[:600])

    return review_loop.run(draft, checks, revise, getattr(ctx.settings, "review_steps", 0), on_step=log)


def product_name(ctx) -> str:
    """One name across phases: the landing page's, else the spec's."""
    return (ctx.artifact("phase2_step2").get("data", {}).get("product_name")
            or ctx.artifact("phase3_step1").get("data", {}).get("product_name") or "the product")


def mvp_spec(ctx):
    """Smallest version that solves the chosen problem: sections, format, acceptance tests."""
    from orchestrator import StepResult

    ptype = ctx.artifact("feasibility").get("data", {}).get("product_type") or "guide"
    name = ctx.artifact("phase2_step2").get("data", {}).get("product_name")
    solution = ctx.artifact("phase2_step1").get("data", {}).get("boxes", {}).get("solution", [])
    body = (focus_block(ctx) + f"Product type: {ptype} (a text product).\n"
            + (f"Product name (use it): {name}\n" if name else "")
            + ("Solution from the Lean Canvas: " + "; ".join(solution) + "\n" if solution else "")
            + "\nWrite the spec for the smallest version: 3 to 5 sections.")
    s = ask(ctx, "builder", "mvp_spec", Spec, body)
    name = name or s.product_name
    lines = [f"# MVP spec: {name}", "", f"- Product type: {ptype}", f"- Format: {s.format}",
             f"- For: {chosen_opportunity(ctx).get('customer') or ctx.brief['target_market']}", "", "## Sections", ""]
    lines += [f"{i}. **{sec.title}**: {sec.purpose}" for i, sec in enumerate(s.sections, start=1)]
    lines += ["", "## Left out of the smallest version", ""] + [f"- {x}" for x in s.out_of_scope]
    lines += ["", "## Acceptance tests (run by code on the finished product)", ""]
    lines += [f"- {k}: {v}" for k, v in ACCEPTANCE.items()]
    return StepResult("\n".join(lines) + "\n", data={"product_name": name, "product_type": ptype, "format": s.format,
                                                     "sections": [{"title": x.title, "purpose": x.purpose} for x in s.sections]})


def run_acceptance(spec_titles: list[str], sections: list[dict], text: str) -> dict[str, bool]:
    """The spec's acceptance tests, in code."""
    have = {_norm(s["title"]): s for s in sections}
    return {
        "every section in the spec is present": bool(spec_titles) and all(_norm(t) in have for t in spec_titles),
        "every section has enough content": bool(sections) and all(len(s["body"]) >= MIN_SECTION_CHARS for s in sections),
        "the product is short enough to review": len(text) <= MAX_PRODUCT_CHARS,
        "no placeholder text": not _PLACEHOLDER.search(text),
    }


def build_product(ctx):
    """Write the product itself following the spec, then run the spec's tests in code."""
    from orchestrator import StepResult

    spec = ctx.artifact("phase3_step1").get("data", {})
    spec_sections = spec.get("sections", [])
    name = product_name(ctx)
    body = (focus_block(ctx) + f"Product: {name} ({spec.get('product_type', 'guide')}). Format: {spec.get('format', 'Markdown')}.\n"
            "Write the complete product. Use exactly these section titles, in this order:\n"
            + "\n".join(f"- {s['title']}: {s['purpose']}" for s in spec_sections))
    p = ask(ctx, "builder", "build_product", Product, body)
    spec_titles = [s["title"] for s in spec_sections]
    review = _review(ctx, "product", Product, body,
                     [{"title": s.title, "body": _strip_heading(s.title, s.body)} for s in p.sections],
                     lambda r: [{"title": s.title, "body": _strip_heading(s.title, s.body)} for s in r.sections],
                     lambda draft: product_defects(spec_titles, draft, ctx.registry))
    sections, removed = [], 0
    for s in review.draft:  # clean-up by code, with or without the loop
        text_body, n = strip_unsourced(s["body"], ctx.registry)
        removed += n
        sections.append({"title": s["title"], "body": demote_headings(text_body)})
    if removed:
        ctx.log.record("builder", "strip_unsourced_lines", outcome=f"{removed} removed",
                       reason='"Source:" or "Reference:" lines in the product that named no fetched URL')
    customer = chosen_opportunity(ctx).get("customer") or ctx.brief["target_market"]
    text = "\n".join([guardrails.DRAFT_HEADER, "", f"# {name}", "", f"A {spec.get('product_type', 'guide')} for {customer}.", ""]
                     + [f"## {s['title']}\n\n{s['body']}\n" for s in sections])
    tests = run_acceptance(spec_titles, sections, text)
    return StepResult(text, data={"tests": tests, "sections": [s["title"] for s in sections], "characters": len(text),
                                  "review": review.summary(), "source_lines_removed": removed})


def user_docs(ctx):
    """How to use the product, in plain language."""
    from orchestrator import StepResult

    name = product_name(ctx)
    titles = ctx.artifact("phase3_step2").get("data", {}).get("sections", [])
    body = (focus_block(ctx) + f"Product: {name}. Its sections: {'; '.join(titles) or 'see the spec'}.\n"
            "Write the user documentation for a customer who has just received it.")
    spec = ctx.artifact("phase3_step1").get("data", {})
    known = "\n".join([str(spec.get("format", ""))] + [f"{s.get('title', '')}: {s.get('purpose', '')}" for s in spec.get("sections", [])]
                      + [ctx.read_output("phase3_step2")])
    review = _review(ctx, "user_docs", Docs, body, ask(ctx, "builder", "user_docs", Docs, body).model_dump(),
                     lambda r: r.model_dump(), lambda draft: docs_defects(draft, known))
    d = Docs.model_validate(review.draft)
    lines = [guardrails.DRAFT_HEADER, "", f"# How to use {name}", "", "## Quick start", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(d.quick_start, start=1)]
    lines += ["", "## Questions", ""]
    for f in d.faq:
        lines += [f"**{f.question}**", "", f.answer, ""]
    lines += ["## What it does not do", ""] + [f"- {x}" for x in d.limits]
    lines += ["", "## Support", "", "Contact the founder. The founder adds contact details before anything is published."]
    return StepResult("\n".join(lines) + "\n", data={"steps": len(d.quick_start), "review": review.summary()})


# ---------------------------------------------------------------- website from fixed templates (optional)

SITE_FAMILIES = ("Written content", "Courses and learning", "Templates and tools")
SITE_KINDS = {"notes": "notes", "note": "notes", "checklist": "checklist", "checklists": "checklist", "course": "course",
              "template": "templates", "templates": "templates", "spreadsheet": "templates", "planner": "templates"}
FAMILY_KIND = {"Written content": "guide", "Courses and learning": "course", "Templates and tools": "templates"}


class SiteCopy(BaseModel):
    tagline: ShortText = Field(description="one plain sentence: what the product is and who it is for")
    about: ShortText = Field(description="two or three sentences on what the product contains")
    summary: ShortText = Field(description="one sentence that describes the product on a card")
    for_whom: ShortText = Field(description="who it is for, in one sentence")


def site_applies(ctx) -> bool:
    """A site is assembled for text products only; software, media and access products keep their specification."""
    return str(ctx.artifact("feasibility").get("data", {}).get("family", "")) in SITE_FAMILIES


def _product_sections(text: str) -> list[dict]:
    """The product draft as sections: each title with its paragraphs. Copied, not asked from the model."""
    out = []
    for chunk in re.split(r"(?m)^## ", text)[1:]:
        title, _, body = chunk.partition("\n")
        paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]
        if title.strip() and paragraphs:
            out.append({"title": title.strip(), "paragraphs": paragraphs})
    return out


def site_content(ctx, copy: "SiteCopy") -> dict:
    """The content object for site_builder. Everything that exists in the run is copied by code: the name, the
    audience, the sections as lessons and the list of what is inside. The model supplies four short texts."""
    family = str(ctx.artifact("feasibility").get("data", {}).get("family", ""))
    word = str(ctx.artifact("feasibility").get("data", {}).get("product_type", "")).lower().split(" ")[-1]
    sections = _product_sections(ctx.read_output("phase3_step2"))
    spec_titles = {_norm(s.get("title", "")) for s in ctx.artifact("phase3_step1").get("data", {}).get("sections", [])} - {""}
    if spec_titles:  # one lesson per spec section, never more lessons than the spec has sections
        sections = ([s for s in sections if _norm(s["title"]) in spec_titles] or sections)[:len(spec_titles)]
    sections = sections[:12]
    inside = [s["title"] for s in sections][:8]
    pricing = ctx.artifact("phase4_step1").get("data", {})
    item = {"kind": SITE_KINDS.get(word, FAMILY_KIND.get(family, "guide")), "title": product_name(ctx), "summary": copy.summary,
            "for_whom": copy.for_whom, "whats_inside": inside, "lessons": sections}
    if pricing.get("price_usd"):
        item["price_usd"] = pricing["price_usd"]
    return {"site": {"name": product_name(ctx), "tagline": copy.tagline, "about": copy.about,
                     "audience": chosen_opportunity(ctx).get("customer") or ctx.brief["target_market"]},
            "items": [item], "faq": []}


def build_site(ctx):
    """Assemble a small static site for the product from fixed templates. The model writes no HTML."""
    import site_builder
    from orchestrator import OutputBlocked, StepResult

    name = product_name(ctx)
    body = (focus_block(ctx) + f"Product: {name}. Write four short texts for its website: a tagline, an about text, "
            "a one-sentence summary and who it is for. Describe what the product is and contains, not what it will bring.")
    copy = ask(ctx, "builder", "site_copy", SiteCopy, body)
    try:
        pages = site_builder.build_site(site_content(ctx, copy))
    except site_builder.SiteContentError as exc:
        raise OutputBlocked(f"the site content does not fit the templates: {exc}") from exc
    problems = site_builder.check_site(pages)
    home = site_builder.OUTPUT_PREFIX + "_home.html"
    for page_name, html in pages.items():
        if page_name == home:
            continue
        check = guardrails.check_output(page_name, html, ctx.log)
        if not check.ok:
            raise OutputBlocked(f"{page_name} blocked by the output check: {'; '.join(check.hits)}")
        ctx.write_output(page_name, html)
    return StepResult(pages[home], data={"pages": sorted(pages), "problems": problems})

"""Phase 3 builder: text digital products only (guide, template pack, checklist, mini-course).

mvp_spec:      one model call. The smallest version as 3-5 sections, plus what is left out.
               The acceptance tests are fixed by code (ACCEPTANCE) and printed in the spec.
build_product: one model call writes every section; then code runs the acceptance tests on
               the text. data["tests"] maps test name -> bool, and the Phase 3 gate passes
               only if every test passes. A failed test sends the work back to the makers.
user_docs:     one model call. Quick start, a short FAQ and what the product does not do.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

import gates
from agents import ask, chosen_opportunity, focus_block
from llm import ShortText

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
    sections = [{"title": s.title, "body": _strip_heading(s.title, s.body)} for s in p.sections]
    customer = chosen_opportunity(ctx).get("customer") or ctx.brief["target_market"]
    text = "\n".join([f"# {name}", "", f"A {spec.get('product_type', 'guide')} for {customer}.", ""]
                     + [f"## {s['title']}\n\n{s['body']}\n" for s in sections])
    tests = run_acceptance([s["title"] for s in spec_sections], sections, text)
    return StepResult(text, data={"tests": tests, "sections": [s["title"] for s in sections], "characters": len(text)})


def user_docs(ctx):
    """How to use the product, in plain language."""
    from orchestrator import StepResult

    name = product_name(ctx)
    titles = ctx.artifact("phase3_step2").get("data", {}).get("sections", [])
    body = (focus_block(ctx) + f"Product: {name}. Its sections: {'; '.join(titles) or 'see the spec'}.\n"
            "Write the user documentation for a customer who has just received it.")
    d = ask(ctx, "builder", "user_docs", Docs, body)
    lines = [f"# How to use {name}", "", "## Quick start", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(d.quick_start, start=1)]
    lines += ["", "## Questions", ""]
    for f in d.faq:
        lines += [f"**{f.question}**", "", f.answer, ""]
    lines += ["## What it does not do", ""] + [f"- {x}" for x in d.limits]
    lines += ["", "## Support", "", "Contact the founder. The founder adds contact details before anything is published."]
    return StepResult("\n".join(lines) + "\n", data={"steps": len(d.quick_start)})

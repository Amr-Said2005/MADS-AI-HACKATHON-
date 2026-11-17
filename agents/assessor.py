"""Feasibility assessor: idea sanity and founder fit, once per run, before any planning.

It does not rate market demand; that needs evidence and belongs to the Phase 1 gate.
The assessor only extracts and estimates. gates.feasibility_gate() decides, in code.

One model call. Before it, at most 2 searches look for a page showing the problem exists
(criterion I3); the model says which fetched page, if any, shows it. Every estimate
(skills, cost, effort) is printed as ASSUMPTION in the feasibility report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model

from agents import ask, gather, keywords, page_url
from llm import ShortText
from sources import Claim

MAX_SEARCHES = 2
MAX_PAGES = 2


class Evidence(BaseModel):
    page: int = Field(ge=1, description="number of the page that shows the problem exists")
    shows: ShortText = Field(description="what on that page shows someone has this problem")


class AssessmentBase(BaseModel):
    customer: ShortText = Field(description="the specific customer named in the brief (from the brief, not from a page)")
    problem: ShortText = Field(description="the specific problem that customer has (from the brief, not from a page)")
    product_type: Literal["guide", "template pack", "checklist", "mini-course", "other"]
    needed_skills: list[ShortText] = Field(min_length=1, max_length=6)
    mvp_cost_usd: float = Field(ge=0, le=100000, description="estimated cash cost of the smallest version")
    effort_hours: float = Field(gt=0, le=5000, description="estimated total hours to make the smallest version")
    narrowed_idea: ShortText = Field(description="a narrower version of the idea for one specific customer")
    search_terms: list[ShortText] = Field(min_length=2, max_length=4,
                                          description="short phrases this customer would type when looking for help")
    evidence: list[Evidence] = Field(default_factory=list, max_length=3)


def assessment_schema(values: list[str]) -> type[AssessmentBase]:
    """The reply schema for one brief: the conflicting value must be one of the founder's own values, or "none"."""
    options = tuple(dict.fromkeys(["none", *[v.strip() for v in values if v.strip()]]))
    return create_model(
        "Assessment", __base__=AssessmentBase,
        values_check=(ShortText, Field(description="one sentence: does making and selling this product break a stated value?")),
        conflicting_value=(Literal[options], Field(description='the stated value the idea clearly breaks, or "none"')),
    )


def assess(ctx):
    """Extract customer and problem, classify the product type, estimate skills, cost and effort."""
    from orchestrator import StepResult

    b = ctx.brief
    words = keywords(b["idea"], 8) or keywords(b["target_market"], 8)
    queries = [" ".join(words), " ".join(words[:6] + ["how", "do", "you"])][:MAX_SEARCHES]
    pages = gather(ctx, queries, want=MAX_PAGES)
    body = ("Assess this idea and this founder. "
            + (f"{len(pages)} fetched page(s) follow; cite one in `evidence` only if it shows a real person or "
               "organisation describing this problem." if pages else "No page could be fetched; leave `evidence` empty."))
    a = ask(ctx, "assessor", "assess", assessment_schema(b["values"]), body, pages=pages)
    claims = [Claim(e.shows, page_url(pages, e.page), "SAY") for e in a.evidence]
    data = a.model_dump(exclude={"evidence", "conflicting_value"})
    data["values_conflict"] = "" if a.conflicting_value == "none" else a.conflicting_value
    data["queries"] = queries
    content = (f"Customer: {a.customer}\nProblem: {a.problem}\nProduct type: {a.product_type}\n"
               f"Needed skills (ASSUMPTION): {', '.join(a.needed_skills)}\n"
               f"Cost of the smallest version (ASSUMPTION): ${a.mvp_cost_usd:g}\n"
               f"Effort (ASSUMPTION): {a.effort_hours:g} hours\n")
    return StepResult(content, claims=claims, data=data)

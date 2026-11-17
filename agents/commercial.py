"""Phase 4 commercial: pricing and draft policies (labelled: drafts, not legal advice).

pricing:  one model call. A price with reasons; a reason cites a numbered line of Phase 1
          market evidence (code supplies the URL) or is printed as ASSUMPTION. data carries
          price_usd and product_name for the Phase 4 gate and the mocked payment test.
policies: one model call. Refund, privacy and terms drafts. Code adds the heading that says
          they are drafts for the founder and not legal advice; the gate checks for it.

Both are recommendations. The founder reviews the price and the policies at the milestone.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents import ask, cite, focus_block
from agents.builder import product_name
from llm import LongText, ShortText
from sources import Claim

DRAFT_NOTICE = "Draft for founder review, not legal advice."


class Reason(BaseModel):
    text: ShortText
    evidence: int | None = Field(default=None, description="number of the market evidence line that shows this; null if none does")


class Pricing(BaseModel):
    price_usd: float = Field(gt=0, le=500)
    charged_as: Literal["one-time payment", "pay what you want with a minimum", "monthly subscription"]
    reasons: list[Reason] = Field(min_length=1, max_length=4)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=3)


class Policies(BaseModel):
    refund: LongText = Field(description="refund policy draft, 2 to 4 sentences")
    privacy: LongText = Field(description="privacy note draft: what is collected and why, 2 to 4 sentences")
    terms: LongText = Field(description="terms of use draft, 2 to 4 sentences")


def pricing(ctx):
    """Price from the paying-source evidence in Phase 1; state the reasoning and the ASSUMPTIONs."""
    from orchestrator import StepResult

    name = product_name(ctx)
    evidence = [c for c in ctx.artifact("phase1_step3").get("claims", []) if c.get("label") == "SOURCED"]
    body = (focus_block(ctx) + f"Product: {name}.\nMarket evidence from Phase 1 (cite a line by its number):\n"
            + ("\n".join(f"E{i} [{c['kind']}] {c['text']}" for i, c in enumerate(evidence, start=1)) or "none")
            + "\n\nRecommend one price in US dollars and how it is charged.")
    p = ask(ctx, "commercial", "pricing", Pricing, body)
    claims: list[Claim] = []
    lines = ["# Pricing", "", f"Product: {name}", "", f"**Recommended price: ${p.price_usd:,.2f}, {p.charged_as}.**", "",
             "This is a recommendation for the founder to approve or change. It is not a forecast: no sales or income are promised.",
             "", "## Reasons", ""]
    for r in p.reasons:
        src = evidence[r.evidence - 1] if r.evidence and 1 <= r.evidence <= len(evidence) else None
        url = src["url"] if src else None
        claims.append(Claim(r.text, url, src["kind"] if src else "market"))
        lines.append(cite(ctx, r.text, url))
    if p.assumptions:
        lines += ["", "## Assumptions behind the price", ""]
        for a in p.assumptions:
            claims.append(Claim(a, None, "market"))
            lines.append(cite(ctx, a, None))
    return StepResult("\n".join(lines) + "\n", claims=claims,
                      data={"price_usd": p.price_usd, "product_name": name, "charged_as": p.charged_as})


def policies(ctx):
    """Draft refund, privacy and terms text, marked as drafts and not legal advice."""
    from orchestrator import StepResult

    name = product_name(ctx)
    price = ctx.artifact("phase4_step1").get("data", {})
    body = (focus_block(ctx) + f"Product: {name}, a text product delivered as a file or a page.\n"
            + (f"Price: ${price['price_usd']:,.2f}, {price.get('charged_as', 'one-time payment')}.\n" if price.get("price_usd") else "")
            + "Write short drafts of the refund policy, the privacy note and the terms of use.")
    p = ask(ctx, "commercial", "policies", Policies, body)
    lines = [f"# Policies for {name}", "", f"> **{DRAFT_NOTICE}** These drafts were written by a model. The founder must read "
             "them, change them to fit the law where they sell, and have them checked before publishing.", "",
             "## Refund policy (draft)", "", p.refund, "", "## Privacy note (draft)", "", p.privacy, "",
             "## Terms of use (draft)", "", p.terms, "",
             "Refunds are decided and issued by the founder. This system never moves money."]
    return StepResult("\n".join(lines) + "\n", data={"refund_excerpt": p.refund[:240]})

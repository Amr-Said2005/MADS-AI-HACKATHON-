"""Phase 6 operator: a support FAQ and a pivot-or-persevere memo.

support_faq: one model call. Answers come from this run's product, user docs, price and draft
             policies. Anything about money goes to the founder: the system never refunds.
pivot_memo:  the metrics and the recommendation are made by code, not by the model. A metric
             comes only from a counter (demand.py), and in this prototype the counter is
             MOCKED and reads zero, so the metric is "not measured" and the system does not
             recommend persevering. "What this run showed" is a list of facts written by code.
             One model call adds only the riskiest assumptions and what to measure next.
             data["metrics"] feeds the Phase 6 gate.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

import demand
import gates
from agents import ask, chosen_opportunity, cite, focus_block
from agents.builder import product_name
from llm import ShortText

MOCKED = True  # the metrics (see demand.py)


class Answer(BaseModel):
    question: ShortText
    answer: ShortText


class Faq(BaseModel):
    faq: list[Answer] = Field(min_length=4, max_length=8)
    ask_the_founder: list[ShortText] = Field(min_length=2, max_length=4,
                                             description="kinds of request that must go to the founder, not an FAQ")


class Memo(BaseModel):
    riskiest_assumptions: list[ShortText] = Field(min_length=2, max_length=4)
    measure_next: list[ShortText] = Field(min_length=2, max_length=4, description="what the founder should measure next, and how")


def support_faq(ctx):
    """FAQ drafted from the product, the user docs and the draft policies."""
    from orchestrator import StepResult

    name = product_name(ctx)
    pricing = ctx.artifact("phase4_step1").get("data", {})
    sections = ctx.artifact("phase3_step2").get("data", {}).get("sections", [])
    refund = ctx.artifact("phase4_step2").get("data", {}).get("refund_excerpt", "")
    docs = ctx.read_output("phase3_step3")[:1500]
    body = (focus_block(ctx) + f"Product: {name}. Its sections: {'; '.join(sections) or 'see the user docs'}.\n"
            + (f"Price: ${pricing['price_usd']:,.2f}, {pricing.get('charged_as', 'one-time payment')}.\n" if pricing.get("price_usd") else "")
            + (f"Refund policy draft: {refund}\n" if refund else "")
            + (f"\nUser docs:\n{docs}\n" if docs else "")
            + "\nWrite a support FAQ for customers of this product.")
    f = ask(ctx, "operator", "support_faq", Faq, body)
    lines = [f"# Support FAQ for {name}", "",
             "A draft for the founder to check before it is used. It answers only from this run's product, user docs "
             "and draft policies.", "", "## Questions", ""]
    for a in f.faq:
        lines += [f"**{a.question}**", "", a.answer, ""]
    lines += ["## Send these to the founder", ""] + [f"- {x}" for x in f.ask_the_founder]
    lines += ["", "Refunds and anything else about money are decided and handled by the founder. This system never moves money."]
    return StepResult("\n".join(lines) + "\n", data={"questions": len(f.faq)})


def read_metrics(ctx) -> list[dict]:
    """Every metric with a target set before launch, read from its counter. Code only."""
    target = gates.DEMAND_TARGET
    count = demand.read_counter(ctx.run_id)
    status, detail = demand.evaluate(target, count.visitors, count.signups)
    return [{"name": "landing page sign-up rate",
             "target": f"at least {target['min_signup_rate']:.0%} of at least {target['min_cold_visitors']} cold visitors "
                       f"within {target['window_days']} days",
             "result": f"{count.signups} sign-ups from {count.visitors} cold visitors",
             "status": status, "detail": detail, "source": count.source}]


def pivot_memo(ctx):
    """Compare metrics with the targets set before launch; recommend persevere, pivot or stop."""
    from orchestrator import StepResult

    metrics = read_metrics(ctx)
    gate = gates.phase6_gate(metrics)  # the recommendation is the gate's, made in code
    recommendation = {"go": "persevere", "pivot": "pivot"}.get(gate.outcome.value, "stop")
    canvas = ctx.artifact("phase2_step1").get("claims", [])
    assumptions = [c["text"] for c in canvas if c.get("label") != "SOURCED"][:8]
    p1 = [c for c in ctx.artifact("phase1_step1").get("claims", []) + ctx.artifact("phase1_step3").get("claims", [])
          if c.get("label") == "SOURCED"]
    tests = ctx.artifact("phase3_step2").get("data", {}).get("tests", {})
    price = ctx.artifact("phase4_step1").get("data", {})
    facts = [f"Chosen opportunity: {chosen_opportunity(ctx).get('title') or ctx.brief['idea']}.",
             f"Phase 1 cited {len({c['url'] for c in p1})} fetched pages for {len(p1)} claims. That is evidence the "
             "problem is talked about, not that people will buy.",
             f"Product acceptance tests: {sum(1 for v in tests.values() if v)} of {len(tests)} passed.",
             (f"Recommended price: ${price['price_usd']:,.2f}, {price.get('charged_as', '')}." if price.get("price_usd") else "No price set."),
             "Nothing was published, posted or sold: publishing, posting and payments are MOCKED.",
             "No visitor was sent to the landing page, so the absence of sign-ups says nothing about interest."]
    facts += [f"Metric '{m['name']}': {m['status'].replace('_', ' ')} ({m['detail']})." for m in metrics]
    body = (focus_block(ctx) + "Facts from this run:\n" + "\n".join(f"- {x}" for x in facts)
            + "\nAssumptions in the Lean Canvas that no source supports:\n" + ("\n".join(f"- {a}" for a in assumptions) or "- none")
            + f"\n\nThe recommendation is already made by code: {recommendation.upper()}. Do not change it or argue with it. "
              "Name the riskiest assumptions and say what to measure next.")
    m = ask(ctx, "operator", "pivot_memo", Memo, body)

    lines = ["# Pivot or persevere memo", "",
             "> **MOCKED metrics.** Nothing was published or sold in this run, so the counters read zero. "
             "No number below is a real result, and none is invented.", "",
             "## Metrics against targets set before launch", "",
             "| Metric | Target | Result | Status |", "|---|---|---|---|"]
    lines += [f"| {x['name']} | {x['target']} | {x['result']} | {x['status'].replace('_', ' ')} |" for x in metrics]
    lines += ["", "Source of the numbers: " + "; ".join(sorted({x["source"] for x in metrics})) + ".", "",
              "## Recommendation (made by code from the metrics)", "", f"**{recommendation.upper()}**", ""]
    lines += [f"- {r}" for r in gate.reasons]
    lines += ["", "## What this run showed (facts, written by code)", ""] + [f"- {x}" for x in facts]
    lines += ["", "## Riskiest assumptions to test", ""] + [cite(ctx, x, None) for x in m.riskiest_assumptions]
    lines += ["", "## What to measure next", ""] + [f"- {x}" for x in m.measure_next]
    return StepResult("\n".join(lines) + "\n", data={"recommendation": recommendation, "metrics": metrics})

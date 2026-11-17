"""Phase 2 strategist: Lean Canvas, landing page, demand test.

lean_canvas:  one model call. Every entry either cites a numbered line of Phase 1 evidence
              (code supplies the URL) or is printed as ASSUMPTION.
landing_page: one model call for the copy; the page itself is a fixed template in this file.
              The template has no testimonial block, no countdown and no earnings figures,
              and says on the page that it is a draft whose form is not connected.
demand_test:  no model call. The target comes from gates.DEMAND_TARGET, set before any result.
              The sign-up count comes only from the MOCKED counter in demand.py, which reads
              zero: the system never invents sign-ups, so demand is reported as not measured.
"""

from __future__ import annotations

import html
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

import demand
import gates
import sources
from agents import ask, chosen_opportunity, cite, focus_block
from llm import ShortText
from sources import Claim

MOCKED = True  # sign-up results (see demand.py)

BOXES = [("problem", "Problem"), ("existing_alternatives", "Existing alternatives"),
         ("customer_segments", "Customer segments"), ("unique_value_proposition", "Unique value proposition"),
         ("solution", "Solution"), ("channels", "Channels"), ("revenue_streams", "Revenue streams"),
         ("cost_structure", "Cost structure"), ("key_metrics", "Key metrics"), ("unfair_advantage", "Unfair advantage")]


class Entry(BaseModel):
    text: ShortText
    evidence: int | None = Field(default=None, description="number of the Phase 1 evidence line that shows this; null if none does")


Box = Annotated[list[Entry], Field(min_length=1, max_length=3)]


class Canvas(BaseModel):
    problem: Box
    existing_alternatives: Box
    customer_segments: Box
    unique_value_proposition: Box
    solution: Box
    channels: Box
    revenue_streams: Box
    cost_structure: Box
    key_metrics: Box
    unfair_advantage: Box


Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=40)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=60)]


class Landing(BaseModel):
    product_name: Name
    headline: ShortText = Field(description="what the product is and who it is for, in plain words")
    subheadline: ShortText
    who_for: ShortText
    helps_with: list[ShortText] = Field(min_length=3, max_length=4, description="what the product helps the customer do")
    includes: list[ShortText] = Field(min_length=2, max_length=5, description="what is in the product")
    cta_label: Label = Field(description="text on the sign-up button, e.g. 'Tell me when it is ready'")


def phase1_evidence(ctx) -> list[dict]:
    """Sourced Phase 1 claims the canvas may cite: the problem evidence and the market claims."""
    pick = str(ctx.artifact("phase1_step2").get("data", {}).get("pick") or "")
    out = [c for c in ctx.artifact("phase1_step1").get("claims", [])
           if c.get("label") == "SOURCED" and c.get("group", "") in ("", pick)]
    out += [c for c in ctx.artifact("phase1_step3").get("claims", []) if c.get("label") == "SOURCED"]
    return out


def lean_canvas(ctx):
    """Ten-box Lean Canvas; each entry cites Phase 1 evidence or is labelled ASSUMPTION."""
    from orchestrator import StepResult

    chosen = chosen_opportunity(ctx)
    evidence = phase1_evidence(ctx)
    body = (focus_block(ctx) + "\nPhase 1 evidence (cite a line by its number):\n"
            + ("\n".join(f"E{i} [{c['kind']}] {c['text']}" for i, c in enumerate(evidence, start=1)) or "none")
            + "\n\nFill in the Lean Canvas. Give an entry an evidence number only if that line shows it; otherwise null.")
    canvas = ask(ctx, "strategist", "lean_canvas", Canvas, body)

    claims: list[Claim] = []
    lines = ["# Lean Canvas", "", f"For: {chosen.get('title') or ctx.brief['idea']}", "",
             "Every entry cites a source fetched in Phase 1 or is labelled ASSUMPTION. Entries labelled ASSUMPTION "
             "are what the demand test and the interviews have to check.", ""]
    boxes: dict[str, list[str]] = {}
    for n, (key, title) in enumerate(BOXES, start=1):
        lines += [f"## {n}. {title}", ""]
        for e in getattr(canvas, key):
            src = evidence[e.evidence - 1] if e.evidence and 1 <= e.evidence <= len(evidence) else None
            url = src["url"] if src else None
            claims.append(Claim(e.text, url, src["kind"] if src else "other", group=key))
            lines.append(cite(ctx, e.text, url))
            boxes.setdefault(key, []).append(e.text)
        lines.append("")
    return StepResult("\n".join(lines), claims=claims, data={"boxes": boxes})


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  body {{ margin: 0; font: 17px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d1d1b; background: #fbfaf7; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 1.9rem; line-height: 1.2; margin: 1rem 0 .5rem; }}
  h2 {{ font-size: 1.15rem; margin: 1.8rem 0 .4rem; }}
  .draft {{ background: #fff1c2; border: 2px solid #d9a400; border-radius: 8px; padding: 8px 12px; font-size: .9rem; }}
  .sub {{ font-size: 1.1rem; color: #4b4b46; }}
  form {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: .5rem; }}
  input, button {{ font: inherit; padding: 10px 12px; border-radius: 6px; border: 1px solid #b9b7ae; }}
  input {{ flex: 1 1 220px; min-width: 0; }}
  button {{ background: #1d1d1b; color: #fbfaf7; cursor: pointer; }}
  .small {{ font-size: .85rem; color: #6b6b66; }}
</style>
</head>
<body>
<main>
  <p class="draft">Draft preview. This page is not published. The sign-up form is not connected, so no email is collected.</p>
  <h1>{headline}</h1>
  <p class="sub">{subheadline}</p>
  <h2>Who it is for</h2>
  <p>{who_for}</p>
  <h2>What it helps with</h2>
  <ul>
{helps}
  </ul>
  <h2>What you get</h2>
  <ul>
{includes}
  </ul>
  <h2>Hear when it is ready</h2>
  <form action="#" method="post">
    <label for="email" class="small">Email address</label>
    <input id="email" name="email" type="email" autocomplete="email" required>
    <button type="submit">{cta}</button>
  </form>
  <p class="small">{name} is still being made. No payment is taken on this page. What you get out of it depends on how you use it.</p>
</main>
</body>
</html>
"""


def landing_page(ctx):
    """Single static HTML page: who it is for, what it helps with, honest call to action."""
    from orchestrator import StepResult

    boxes = ctx.artifact("phase2_step1").get("data", {}).get("boxes", {})
    body = (focus_block(ctx)
            + "".join(f"{title} (from the Lean Canvas): {'; '.join(boxes[key])}\n"
                      for key, title in BOXES if key in ("unique_value_proposition", "solution") and boxes.get(key))
            + "\nWrite the copy for a one-page landing page that asks visitors to leave an email address.")
    c = ask(ctx, "strategist", "landing_page", Landing, body)
    esc = html.escape
    page = _PAGE.format(name=esc(c.product_name), headline=esc(c.headline), subheadline=esc(c.subheadline),
                        who_for=esc(c.who_for), cta=esc(c.cta_label),
                        helps="\n".join(f"    <li>{esc(x)}</li>" for x in c.helps_with),
                        includes="\n".join(f"    <li>{esc(x)}</li>" for x in c.includes))
    return StepResult(page, data={"product_name": c.product_name, "headline": c.headline, "cta_label": c.cta_label})


def demand_test(ctx):
    """State the target and the method; the result comes only from the MOCKED counter, which reads zero."""
    from orchestrator import StepResult

    t = gates.DEMAND_TARGET
    count = demand.read_counter(ctx.run_id)
    status, detail = demand.evaluate(t, count.visitors, count.signups)
    # A count the founder reported for their own test replaces the MOCKED counter. The target is the same.
    reported = demand.founder_reported((ctx.rec.artifacts.get("founder_evidence") or {}).get("items", []))
    if reported:
        count = demand.SignupCount(int(reported["visitors"]), int(reported["signups"]), demand.FOUNDER_REPORTED)
        status, detail = demand.evaluate_reported(t, count.visitors, count.signups, int(reported["days"]))
    channels = ctx.artifact("phase2_step1").get("data", {}).get("boxes", {}).get("channels", [])
    basis = []
    for url in t["sources"]:  # cite the basis only if this run could fetch it
        try:
            ctx.registry.fetch(url)
            basis.append(f"- [{url}]({url})")
        except sources.FetchError:
            basis.append("- a guide named in ARCHITECTURE.md could not be fetched in this run: ASSUMPTION")
    lines = [
        "# Demand test", "",
        ("> **Founder-reported, not verified by the system.** The numbers below were reported by the founder for a "
         "test the founder ran. The system did not publish the page, did not count anything and cannot confirm them."
         if reported else
         "> **MOCKED.** The landing page is not published and the sign-up counter is a mock that reads zero. "
         "No visitors were sent, no sign-ups were collected, and none are invented."), "",
        "## Target (set by code before any result)", "",
        f"At least {t['min_signup_rate']:.0%} of at least {t['min_cold_visitors']} cold visitors leave their email "
        f"within {t['window_days']} days.", "", "Basis for the target:", "", *basis, "",
        "## How the founder runs it", "",
        "1. Approve publishing the landing page (a separate decision at the milestone review).",
        "2. Send cold visitors to it: people who do not know the founder. Channels from the Lean Canvas:",
        *(["   " + sources.claim_line(c, None) for c in channels] or ["   " + sources.claim_line("none listed", None)]),
        f"3. Count unique visitors and sign-ups for {t['window_days']} days.",
        f"4. Read the rate only once {t['min_cold_visitors']} cold visitors have arrived; below that it is noise.", "",
        "## Result", "",
        "| Cold visitors | Sign-ups | Status |", "|---|---|---|",
        f"| {count.visitors} | {count.signups} | {status.replace('_', ' ')} |", "",
        f"{detail[0].upper()}{detail[1:]}. Source of the numbers: {count.source}.", "",
        ("The result rests on the founder's report alone (ASSUMPTION (founder-stated)); the system has not verified it."
         if reported else
         "Demand is not validated. Nothing in this file is evidence that people want the product."),
    ]
    return StepResult("\n".join(lines) + "\n", data={"status": status, "detail": detail, "visitors": count.visitors,
                                                      "signups": count.signups, "mocked": not reported,
                                                      "founder_reported": bool(reported)})

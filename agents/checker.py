"""Checker (verification role): verifies artefacts against the plan. It never edits them.

The plan review is code, not a model call (orchestrator, gates.check_plan): every criterion
needs an id, a check, a threshold and a method of "code" or "source", and at most 8 criteria.

verify(ctx): ONE model call for the whole step. It gets the approved criteria, facts that
  code measured from this phase's artefacts, the list of fetched source URLs and output
  files, and (unless FAST_MODE) excerpts of the artefacts. It returns pass, fail or
  unverified per criterion with evidence. Code then downgrades any evidence that is not
  a fetched URL, a test name, an output file of this run or ASSUMPTION, and turns a
  "pass" without valid evidence into "unverified". The gate, not the checker, decides.

reconcile_roadmap(ctx): optional (N_ARCHITECTS=2 only); STUBBED.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from agents import ask, domain, stubbed

EXCERPT_CHARS = 900


class Row(BaseModel):
    id: str = Field(min_length=1, max_length=12)
    result: Literal["pass", "fail", "unverified"]
    evidence: str = Field(max_length=300, description="a source URL, an output file name, a test name, or ASSUMPTION")


class Verification(BaseModel):
    rows: list[Row] = Field(min_length=1, max_length=12)


def measured_facts(ctx, steps) -> list[str]:
    """Facts code can measure from this phase's artefacts: the gate's inputs, in words."""
    facts = [f"Founder budget: ${ctx.brief['budget_usd']}; hours per week: {ctx.brief['hours_per_week']}."]
    pick = str(ctx.artifact("phase1_step2").get("data", {}).get("pick") or "")
    for s in steps:
        art = ctx.artifact(s.key)
        if not art:
            facts.append(f"{s.output}: NOT WRITTEN.")
            continue
        claims = art.get("claims", [])
        if pick and s.key == "phase1_step1":
            claims = [c for c in claims if c.get("group", "") in ("", pick)]
            facts.append(f"Chosen opportunity: {pick}. Counts below are for its claims.")
        sourced = [c for c in claims if c.get("label") == "SOURCED"]
        say = {c["url"] for c in sourced if c.get("kind") == "SAY"}
        pay = {c["url"] for c in sourced if c.get("kind") == "PAY"}
        line = f"{s.output}: written, {len(ctx.read_output(s.key))} characters"
        if claims:
            line += (f"; {len(claims)} claims, {len(claims) - len(sourced)} labelled ASSUMPTION; "
                     f"{len(say)} SAY sources from {len({domain(u) for u in say})} domains; {len(pay)} PAY sources")
        data = {k: v for k, v in (art.get("data") or {}).items() if isinstance(v, (int, float, str, bool, dict))}
        if data:
            line += "; data: " + json.dumps(data, ensure_ascii=False)[:400]
        facts.append(line + ".")
    return facts


def verify(ctx):
    """Verify each plan criterion against the phase's artefacts and report pass, fail or unverified."""
    import orchestrator as orch
    from orchestrator import StepResult

    steps = orch.STEPS[orch.PHASE_STATES[ctx.phase - 1]]
    urls = ctx.registry.urls()
    tests = sorted({str(k) for s in steps for k in (ctx.artifact(s.key).get("data", {}).get("tests") or {})})
    parts = ["Criteria to verify:"] + [f"- {c['id']} ({c['method']}): {c['check']} Threshold: {c['threshold']}"
                                       for c in ctx.criteria]
    state = orch.PHASE_STATES[ctx.phase - 1]
    gate = orch.SIGN_OFF_GATES[state](ctx, ctx.attempt)  # the same code checks the sign-off will run
    parts += ["", "Gate checks measured by code (use these for criteria with method code):"]
    parts += [f"- {c.name}: {'MET' if c.passed else 'NOT MET'} ({c.detail})" for c in gate.checks] or ["- none"]
    parts += ["", "Other facts measured by code from this phase's artefacts:"] + [f"- {f}" for f in measured_facts(ctx, steps)]
    parts += ["", "Fetched source URLs (the only URLs you may give as evidence):"] + ([f"- {u}" for u in urls] or ["- none"])
    parts += ["", "Output files: " + ", ".join(s.output for s in steps)]
    if tests:
        parts += ["Test names: " + ", ".join(tests)]
    if not ctx.settings.fast_mode:  # fast mode verifies claims and gate inputs only
        for s in steps:
            text = ctx.read_output(s.key)
            if text:
                parts += ["", f"Excerpt of {s.output}:", text[:EXCERPT_CHARS]]
    v = ask(ctx, "checker", "verify", Verification, "\n".join(parts))
    rows = [r.model_dump() for r in v.rows]
    content = "\n".join(f"- {r['id']}: {r['result']} ({r['evidence']})" for r in rows) + "\n"
    return StepResult(content, data={"rows": rows})


@stubbed
def reconcile_roadmap(ctx):
    """Optional (N_ARCHITECTS=2 only): merge the planners' roadmap proposals into one roadmap of six milestones."""

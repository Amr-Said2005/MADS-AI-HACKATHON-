"""Planner (architect role): writes the plan and its pass criteria before any maker runs.

plan(ctx):    one model call per planner. Criteria for the current phase (ctx.phase), each with a
              check, a threshold and a method of "code" or "source". Ids are assigned by code.
              The plan review is code (gates.check_plan); ctx.feedback carries its notes on a revise.
roadmap(ctx): one model call. Six milestones, one per phase. Code sets each milestone's
              deliverables (the real output file names) and its gate, and rejects any
              milestone that promises income, sales or sign-up numbers (gates.validate_roadmap).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

import gates
from agents import ask
from llm import ShortText

Threshold = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

# What each phase's sign-off gate measures, so the plan's criteria line up with it.
GATE_NOTES = {
    1: ("Evidence gate (code): at least 3 fetched SAY sources from 3 different domains describe the problem; "
        "at least 1 fetched PAY source shows someone already pays for a comparable solution; the founder's "
        "budget and weekly hours cover the scorer's estimate of the smallest version; fewer than half of the "
        "chosen opportunity's claims are ASSUMPTION."),
    2: ("Code checks: the Lean Canvas Problem box and Customer segments box each cite at least 1 line of Phase 1 "
        "evidence; the landing page is written and has an email sign-up form. The demand target is fixed in advance "
        "(at least 5% sign-ups from at least 200 cold visitors within 14 days), but the sign-up counter is MOCKED, "
        "so no demand result exists and none may be claimed."),
    3: "Product tests (code): every test the builder ran on the product passes.",
    4: "Code checks: a price is set, the product is named, and the policies say they are not legal advice.",
    5: "Code checks: a store page and at least one launch post draft exist. Nothing is published without the founder.",
    6: "Metrics gate: the operator's memo recommends persevere, pivot or stop against targets set before launch.",
}


class Criterion(BaseModel):
    check: ShortText = Field(description="what is measured, in one sentence")
    threshold: Threshold = Field(description="the pass mark, e.g. 'at least 3'")
    method: Literal["code", "source"] = Field(description="'code' if a count or test decides it, 'source' if a fetched page does")


class Plan(BaseModel):
    criteria: list[Criterion] = Field(min_length=3, max_length=gates.MAX_PLAN_CRITERIA)


class Milestone(BaseModel):
    phase: int = Field(ge=1, le=6)
    title: ShortText
    founder_sees: ShortText = Field(description="what the founder will be shown at this milestone")
    decision_asked: ShortText = Field(description="the decision the founder will be asked to make")


class Roadmap(BaseModel):
    milestones: list[Milestone] = Field(min_length=6, max_length=6)


def _phase_outputs(n: int) -> list[str]:
    import orchestrator as orch
    return [row["output"] for row in orch.catalog() if row["phase"] == n]


def plan(ctx):
    """Plan this phase's work and list measurable pass criteria with thresholds."""
    import orchestrator as orch
    from orchestrator import StepResult

    n = ctx.phase
    steps = orch.STEPS[orch.PHASE_STATES[n - 1]]
    body = (f"Write the pass criteria for Phase {n}: {gates.PHASE_NAMES[n]}.\n"
            "The makers will produce these files:\n" + "\n".join(f"- {s.output}: {s.label}" for s in steps)
            + f"\nThe sign-off gate for this phase: {GATE_NOTES[n]}")
    p = ask(ctx, "planner", "plan", Plan, body)
    criteria = [{"id": f"C{i}", "check": c.check, "threshold": c.threshold, "method": c.method}
                for i, c in enumerate(p.criteria, start=1)]
    content = "\n".join(f"- {c['id']} ({c['method']}): {c['check']} Threshold: {c['threshold']}" for c in criteria) + "\n"
    return StepResult(content, data={"criteria": criteria})


def roadmap(ctx):
    """Propose six milestones, one per phase, each saying what the founder sees and decides."""
    from orchestrator import StepResult

    body = ("Write the roadmap: exactly six milestones, phases 1 to 6 in order.\n"
            "Facts about this system: it cannot run interviews, user tests or ads. Sign-up results, publishing, "
            "posting and payments are MOCKED, and nothing goes out without the founder's approval. Do not write "
            "that anything was tested with users, or that anyone signed up or paid.\n"
            + "\n".join(f"- Phase {n}: {gates.PHASE_NAMES[n]}. Gate: {gates.PHASE_GATES[n]}. "
                        f"Files: {', '.join(_phase_outputs(n))}" for n in range(1, 7)))
    r = ask(ctx, "planner", "roadmap", Roadmap, body)
    milestones = [{"id": f"M{m.phase}", "phase": m.phase, "title": m.title, "deliverables": _phase_outputs(m.phase),
                   "founder_sees": m.founder_sees, "decision_asked": m.decision_asked} for m in r.milestones]
    content = "\n".join(f"- M{m['phase']}: {m['title']}" for m in milestones) + "\n"
    return StepResult(content, data={"milestones": milestones})

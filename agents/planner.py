"""Planner (architect role): writes the plan and its pass criteria before any maker runs.

plan(ctx):    one small model call per planner (reply capped at PLAN_MAX_TOKENS). The sign-off gate's own
              checks are written into every plan by code (gates.gate_criteria). The quality criteria come
              from a template in gates.py (gates.CRITERIA_TEMPLATE): modules that code can measure. The
              model may only move a module's threshold inside the module's range and add at most two
              criteria of the same fixed kinds for a file of this phase. Code builds the criteria, drops
              duplicates, and uses the template defaults when the reply is invalid. In FAST_MODE the
              orchestrator does not call plan() at all and uses the template defaults.
              The plan review is code (gates.check_plan); ctx.feedback carries its notes on a revise.
roadmap(ctx): one model call. Six milestones, one per phase. Code sets each milestone's
              deliverables (the real output file names) and its gate, and rejects any
              milestone that promises income, sales or sign-up numbers (gates.validate_roadmap).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model

import gates
from agents import ask
from llm import ModelOutputInvalid, ShortText

# What each phase's sign-off gate measures, so the plan's criteria line up with it.
GATE_NOTES = {
    1: ("Evidence gate (code): at least 3 fetched SAY sources from 3 different domains describe the problem; "
        "at least 1 fetched PAY source, with a price that code finds in its text, shows someone already pays for a "
        "comparable solution; the founder's "
        "budget and weekly hours cover the scorer's estimate of the smallest version; fewer than half of the "
        "chosen opportunity's claims are ASSUMPTION."),
    2: ("Code checks: the Lean Canvas Problem box and Customer segments box each cite at least 1 line of Phase 1 "
        "evidence; the landing page is written and has an email sign-up form. The demand target is fixed in advance "
        "(at least 5% sign-ups from at least 200 cold visitors within 14 days), but the sign-up counter is MOCKED, "
        "so no demand result exists and none may be claimed."),
    3: "Product tests (code): every test the builder ran on the product passes.",
    4: "Code checks: a price is set, the product is named, and the policies say they are not legal advice.",
    5: "Code checks: a store page and at least one launch post draft exist. Nothing is published without the founder.",
    6: ("Metrics gate (code): persevere only if every metric with a pre-set target is measured and met; a measured "
        "miss means pivot. The counters are MOCKED and read zero, so nothing is measured and no result may be claimed."),
}


PLAN_MAX_TOKENS = 300  # the reply is a few numbers and at most two short objects


def plan_schema(n: int, files: list[str]) -> type[BaseModel]:
    """The reply schema for one phase: thresholds for the template's module ids, and at most two extra criteria
    whose kind and file are picked from fixed lists. There is no free text, so a reply cannot restate a gate check."""
    ids = tuple(m.id for m in gates.CRITERIA_TEMPLATE.get(n, ())) or ("none",)
    adjust = create_model("Adjust", id=(Literal[ids], ...), value=(int, Field(ge=1, le=20_000)))
    extra = create_model("Extra", kind=(Literal[gates.CRITERION_KINDS], ...), file=(Literal[tuple(files)], ...),
                         value=(int, Field(default=1, ge=1, le=20_000)),
                         field=(str, Field(default="", max_length=60, description="min_count: the data key to count; "
                                                                                  "required_fields: the data keys")))
    return create_model("Plan", thresholds=(list[adjust], Field(default_factory=list, max_length=len(ids))),
                        extra=(list[extra], Field(default_factory=list, max_length=gates.MAX_EXTRA_CRITERIA)))


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
    files = [s.output for s in steps]
    modules = gates.CRITERIA_TEMPLATE.get(n, ())
    body = (f"Phase {n}: {gates.PHASE_NAMES[n]}. Files of this phase: {', '.join(files)}.\n"
            f"The sign-off gate is code and is already in the plan: {GATE_NOTES[n]}\n"
            "Quality criteria of the template (id, file, what is checked, default threshold, allowed range):\n"
            + "\n".join(f"- {m.id}: {m.file}; {m.kind}; {m.check}; default {m.default}; range {m.lo} to {m.hi}" for m in modules)
            + f"\nKinds for an extra criterion: {', '.join(gates.CRITERION_KINDS)}.\n"
            "Reply with `thresholds` only for the ids you change, and at most two `extra` criteria. "
            "An empty reply keeps the template as it is.")
    source = "model"
    try:
        p = ask(ctx, "planner", "plan", plan_schema(n, files), body, max_tokens=PLAN_MAX_TOKENS)
        thresholds = {a.id: a.value for a in p.thresholds}
        extras = [e.model_dump() for e in p.extra]
    except ModelOutputInvalid:  # an invalid reply never fails the phase: the template defaults are the plan
        thresholds, extras, source = {}, [], "template"
    criteria = gates.template_criteria(n, thresholds, extras, files)
    content = "\n".join(f"- {c['id']} ({c['method']}): {c['check']} Threshold: {c['threshold']}" for c in criteria) + "\n"
    return StepResult(content, data={"criteria": criteria, "source": source})


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

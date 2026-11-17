"""Planner (architect role): writes the plan and its pass criteria before any maker runs.

Two entry points:

plan(ctx): one plan for the current phase (ctx.phase, 1-6). In Phase 1, N_ARCHITECTS
  planners each propose a plan; ctx.architect is this planner's index (0-based).
  Return StepResult with data["criteria"] = [{"id", "check", "threshold", "method"}],
  where method is "code" or "source". The plan is not edited again in the same pass.
  ctx.feedback holds the plan review's notes when the plan was sent back.

roadmap(ctx): the six-milestone roadmap, one per phase, in order.
  Return StepResult with data["milestones"] = [{"id": "M1".."M6", "phase": 1..6, "title",
  "deliverables": [output file names], "founder_sees", "decision_asked"}].
  Code enforces the shape and keeps every gate, whatever the model wrote; no milestone may
  promise income, sales or sign-up numbers. ctx.feedback holds the founder's revision notes.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def plan(ctx):
    """Plan this phase's work and list measurable pass criteria with thresholds."""


@stubbed
def roadmap(ctx):
    """Propose six milestones, one per phase, each saying what the founder sees and decides."""

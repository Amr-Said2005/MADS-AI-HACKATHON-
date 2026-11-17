"""Checker (verification role): verifies artefacts against the plan. It never edits them.

The plan review is code, not a model call (orchestrator, gates.check_plan): every criterion
needs an id, a check, a threshold and a method of "code" or "source", and at most 8 criteria.

reconcile_roadmap(ctx): ctx.proposals is a list of milestone lists from the planners.
  Return data["milestones"] in the same shape as planner.roadmap.

verify(ctx): ONE model call for the whole step. ctx.criteria is the approved plan's criteria (8 at most). Check this phase's artefacts
  (ctx.read_output) against each one. Return data["rows"] = [{"id", "result": "pass" |
  "fail" | "unverified", "evidence"}], where evidence is a registered URL, a test name,
  an output file name of this run, or "ASSUMPTION". Code downgrades any other evidence to
  ASSUMPTION and a "pass" without valid evidence to "unverified". In FAST_MODE the checker
  verifies only claims and gate inputs.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def reconcile_roadmap(ctx):
    """Optional (N_ARCHITECTS=2 only): merge the planners' roadmap proposals into one roadmap of six milestones."""


@stubbed
def verify(ctx):
    """Verify each plan criterion against the phase's artefacts and report pass, fail or unverified."""

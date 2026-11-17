"""Feasibility assessor: idea sanity and founder fit, once per run, before any planning.

It does not rate market demand; that needs evidence and belongs to the Phase 1 gate.
The assessor only extracts and estimates. gates.feasibility_gate() decides, in code.

Contract: return StepResult with
  claims: at most 2 searches' worth of fetched pages (ctx.registry.fetch) showing the
          problem exists, as Claim(text, url, "problem"). Unfetched URLs become ASSUMPTION.
  data:   customer (str), problem (str), product_type (one of gates.SUPPORTED_PRODUCT_TYPES),
          needed_skills (list[str]), mvp_cost_usd (number), effort_hours (number, total for
          the smallest version), values_conflict (str: the brief value quoted, or ""),
          narrowed_idea (str: a narrower idea to try when one or two soft checks fail, or "").
Every estimate (skills, cost, effort) is shown as ASSUMPTION in the feasibility report.
ctx.feedback holds the previous report's failed checks when the check runs again.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def assess(ctx):
    """Extract customer and problem, classify the product type, estimate skills, cost and effort."""

"""Phase 1 scorer: score each opportunity and estimate the smallest viable version.

Contract: content is JSON text. data must include mvp_cost_usd and mvp_hours_per_week
(numbers), which the Phase 1 gate compares with the brief's budget and hours.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def score(ctx):
    """Score opportunities on pain, willingness to pay, reach and founder fit; pick one."""

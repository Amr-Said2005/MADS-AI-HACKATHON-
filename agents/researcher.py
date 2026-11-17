"""Phase 1 researcher: opportunities (step 1) and market/competitors (step 3).

Contract: fetch real pages through ctx.registry.fetch(url) only. Return claims as
sources.Claim(text, url, kind) with kind "problem", "paying", "market" or "other";
the orchestrator stamps any claim whose URL was not fetched as ASSUMPTION and puts the
list where CLAIMS_PLACEHOLDER appears in the content.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def find_opportunities(ctx):
    """Search public discussions for the brief's problem; return 3-5 opportunities with sourced claims."""


@stubbed
def market_research(ctx):
    """Competitors and what people already pay for comparable solutions (kind="paying" claims)."""

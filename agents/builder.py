"""Phase 3 builder: text digital products only (guide, template pack, checklist, mini-course).

Contract for build_product: data["tests"] is a dict of test name -> bool, computed by
code (e.g. every section in the spec is present, length limits, output check passed).
The Phase 3 gate passes only if every test passes.
"""

from __future__ import annotations

from agents import stubbed


@stubbed
def mvp_spec(ctx):
    """Smallest version that solves the chosen problem: sections, format, acceptance tests."""


@stubbed
def build_product(ctx):
    """Write the product itself following the spec, then run the spec's tests in code."""


@stubbed
def user_docs(ctx):
    """How to use the product, in plain language."""

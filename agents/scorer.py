"""Phase 1 scorer: score each opportunity and estimate the smallest viable version.

One model call gives 1-5 scores per opportunity and the estimates. Code adds the totals and
picks the highest (first listed wins a tie), so the choice is not the model's say-so.
The output file is JSON. data carries pick, mvp_cost_usd and mvp_hours_per_week, which the
Phase 1 gate compares with the brief's budget and hours. Estimates are labelled ASSUMPTION.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from agents import ask
from llm import ModelOutputInvalid, ShortText

Points = Annotated[int, Field(ge=1, le=5)]
OppId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=4)]
CRITERIA = ("pain", "willingness_to_pay", "reach", "founder_fit")


class Score(BaseModel):
    id: OppId = Field(description="the opportunity id, e.g. O1")
    pain: Points
    willingness_to_pay: Points
    reach: Points
    founder_fit: Points
    reason: ShortText


class Scoring(BaseModel):
    scores: list[Score] = Field(min_length=1, max_length=4)
    smallest_version: ShortText = Field(description="the smallest version of the product worth testing")
    mvp_cost_usd: float = Field(ge=0, le=100000)
    mvp_hours_per_week: float = Field(gt=0, le=168)


def score(ctx):
    """Score opportunities on pain, willingness to pay, reach and founder fit; pick one."""
    from orchestrator import StepResult

    art = ctx.artifact("phase1_step1")
    opps = art.get("data", {}).get("opportunities", [])
    by_group: dict[str, list[dict]] = {}
    for c in art.get("claims", []):
        by_group.setdefault(c.get("group", ""), []).append(c)
    lines = ["Score every opportunity below from 1 (low) to 5 (high) on pain, willingness_to_pay, reach and "
             "founder_fit. Then estimate the smallest version of the product for this founder.", ""]
    lines.append("Evidence that the problem exists:")
    lines += [f"  - {c['text']} [{'SOURCED' if c['label'] == 'SOURCED' else 'ASSUMPTION'}]"
              for c in by_group.get("", [])] or ["  - none"]
    lines.append("")
    for o in opps:
        lines.append(f"{o['id']}: {o['title']}. Customer: {o['customer']}. Problem: {o['problem']}. "
                     f"Supported by {len(o.get('supported_by', []))} fetched page(s).")
        for c in by_group.get(o["id"], []):
            lines.append(f"  - {c['text']} [ASSUMPTION]")
    s = ask(ctx, "scorer", "score", Scoring, "\n".join(lines))

    known = {o["id"]: o for o in opps}
    rows = []
    for sc in s.scores:
        if sc.id in known and sc.id not in {r["id"] for r in rows}:
            row = sc.model_dump()
            row["total"] = sum(row[k] for k in CRITERIA)
            rows.append(row)
    if not rows:
        raise ModelOutputInvalid("model_output_invalid: scorer gave no score for a known opportunity id")
    pick = max(rows, key=lambda r: r["total"])["id"]  # max() keeps the first of equal totals
    out = {
        "scale": "1 to 5 per criterion, given by the model from the Phase 1 evidence",
        "scores": rows,
        "pick": pick,
        "pick_title": known[pick]["title"],
        "pick_rule": "highest total, added up by code; the first listed wins a tie",
        "smallest_version": {"value": s.smallest_version, "basis": "ASSUMPTION"},
        "mvp_cost_usd": {"value": s.mvp_cost_usd, "basis": "ASSUMPTION"},
        "mvp_hours_per_week": {"value": s.mvp_hours_per_week, "basis": "ASSUMPTION"},
    }
    return StepResult(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                      data={"pick": pick, "pick_title": known[pick]["title"], "smallest_version": s.smallest_version,
                            "mvp_cost_usd": s.mvp_cost_usd,
                            "mvp_hours_per_week": s.mvp_hours_per_week})

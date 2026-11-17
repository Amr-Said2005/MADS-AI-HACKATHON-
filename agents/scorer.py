"""Phase 1 scorer: score each opportunity and estimate the smallest viable version.

One model call gives 1-5 scores for pain, willingness to pay and reach, and the estimates. Founder
fit is computed by code from the brief's skills. Code adds the totals and picks the highest; a tie
is reported as a tie and no narrower opportunity is picked, so the founder's own idea is kept.
When Phase 1 collected no evidence, nothing is scored from the web and no pick is made.
The output file is JSON. data carries pick, mvp_cost_usd and mvp_hours_per_week, which the
Phase 1 gate compares with the brief's budget and hours. Estimates are labelled ASSUMPTION.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

import gates
from agents import ask
from llm import ModelOutputInvalid, ShortText

Points = Annotated[int, Field(ge=1, le=5)]
OppId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=4)]
CRITERIA = ("pain", "willingness_to_pay", "reach", "founder_fit")
FROM_EVIDENCE = ("pain", "willingness_to_pay", "reach")
NOT_SCORED = "not scored"
NO_EVIDENCE_NOTE = ("Not scored: no evidence was collected in this run, so the opportunities cannot be compared. "
                    "No pick was made.")


def founder_fit(ctx) -> tuple[int, str]:
    """1 to 5, from the brief: how many of the skills the idea needs the founder already has. No web evidence is used."""
    d = ctx.artifact("feasibility").get("data", {})
    needed = [s for s in d.get("needed_skills") or [] if str(s).strip()]
    held = [s for s in needed if gates._skill_covered(s, list(ctx.brief["skills"]))]
    if not needed:
        return 3, "no needed skills were listed, so the middle of the scale is used (ASSUMPTION)"
    return 1 + round(4 * len(held) / len(needed)), (
        f"computed from the brief: the founder has {', '.join(held) or 'none'} of the needed skills ({', '.join(needed)})")


class Score(BaseModel):
    id: OppId = Field(description="the opportunity id, e.g. O1")
    pain: Points
    willingness_to_pay: Points
    reach: Points
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
    if any(not str(o.get("title") or "").strip() for o in opps):
        raise ModelOutputInvalid("model_output_invalid: an opportunity has no title")
    fit, fit_basis = founder_fit(ctx)
    if not any(c.get("label") == "SOURCED" for c in art.get("claims", [])):
        # Nothing was collected to compare the opportunities with: no score from the web, and no pick.
        rows = [{"id": o["id"], "title": o["title"], **{k: NOT_SCORED for k in FROM_EVIDENCE}, "founder_fit": fit,
                 "reason": "no evidence was collected, so pain, willingness to pay and reach were not scored",
                 "total": NOT_SCORED} for o in opps]
        out = {"result": NO_EVIDENCE_NOTE, "scale": "1 no sign, 3 some sign, 5 strong sign", "scores": rows, "pick": None,
               "pick_title": None, "pick_rule": "no pick without evidence; the founder's own idea is kept",
               "founder_fit_basis": fit_basis, "smallest_version": {"value": NOT_SCORED, "basis": "not estimated without evidence"},
               "mvp_cost_usd": {"value": None, "basis": "not estimated"}, "mvp_hours_per_week": {"value": None, "basis": "not estimated"}}
        return StepResult(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                          data={"pick": None, "pick_title": None, "smallest_version": "", "mvp_cost_usd": None,
                                "mvp_hours_per_week": None, "scored": False})
    lines = ["Score every opportunity below from 1 (no sign) to 5 (strong sign) on pain, willingness_to_pay and reach. "
             "Then estimate the smallest version of the product for this founder.", ""]
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
            row["title"] = known[sc.id]["title"]
            row["founder_fit"] = fit  # from the brief, the same for every narrowing of the founder's idea
            row["total"] = sum(row[k] for k in CRITERIA)
            rows.append(row)
    if not rows:
        raise ModelOutputInvalid("model_output_invalid: scorer gave no score for a known opportunity id")
    top = max(r["total"] for r in rows)
    leaders = [r["id"] for r in rows if r["total"] == top]
    tie = len(leaders) > 1
    pick = None if tie else leaders[0]
    pick_title = known[pick]["title"] if pick else None
    best = max(CRITERIA, key=lambda k: next(r for r in rows if r["id"] == leaders[0])[k])
    result = (f"Tie at {top} out of 20 between {', '.join(known[i]['title'] for i in leaders)}. No narrower opportunity "
              "was picked, so the founder's own idea is kept." if tie else
              f"Picked: {pick_title}, with {top} out of 20. Its highest score is for {best.replace('_', ' ')}.")
    out = {
        "result": result,
        "scale": "1 no sign, 3 some sign, 5 strong sign; pain, willingness to pay and reach are given by the model from the Phase 1 evidence",
        "scores": rows,
        "pick": pick,
        "pick_title": pick_title,
        "tie": leaders if tie else [],
        "founder_fit_basis": fit_basis,
        "pick_rule": "highest total, added up by code; a tie is reported as a tie and keeps the founder's own idea",
        "smallest_version": {"value": s.smallest_version, "basis": "ASSUMPTION"},
        "mvp_cost_usd": {"value": s.mvp_cost_usd, "basis": "ASSUMPTION"},
        "mvp_hours_per_week": {"value": s.mvp_hours_per_week, "basis": "ASSUMPTION"},
    }
    return StepResult(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                      data={"pick": pick, "pick_title": pick_title, "tie": leaders if tie else [], "scored": True,
                            "smallest_version": s.smallest_version,
                            "mvp_cost_usd": s.mvp_cost_usd,
                            "mvp_hours_per_week": s.mvp_hours_per_week})

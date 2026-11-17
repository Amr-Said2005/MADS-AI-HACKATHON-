"""Plain-language wording for the founder's screens.

Everything here is presentation: fixed lists and small text helpers for the roadmap, the milestone
review and the result pages. Run data and output files are not changed by anything in this module.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The six steps as a founder with no technical background would ask about them.
STEP_TITLES = {1: "Is the problem real?", 2: "Would people sign up?", 3: "Build the first version",
               4: "Price and policies", 5: "Launch (simulated)", 6: "Learn and decide"}
STEP_HAPPENS = {
    1: "The agents search public pages for people describing the problem and for products people already pay for.",
    2: "They draft a one-page business plan and a landing page, and set a sign-up target. Visitors and sign-ups are MOCKED.",
    3: "They write the first version of the product and its user guide, and code tests it.",
    4: "They suggest a price and draft refund, privacy and terms text for you to check.",
    5: "They draft a store page and launch posts. Publishing, posting and the payment test are MOCKED.",
    6: "They draft a support FAQ and a memo on whether to carry on. The numbers behind it are MOCKED.",
}
STEP_DECISION = {
    1: "Go on, ask for changes, change the idea, or stop.",
    2: "Go on, ask for changes, go back a step, or stop. Publishing the page is a separate yes or no.",
    3: "Go on, ask for changes, or stop.",
    4: "Accept the price and policies, ask for changes, or stop.",
    5: "Go on or stop. Each publish, post and payment test is a separate yes or no.",
    6: "Carry on, change direction, or stop.",
}

# Output files by what they are. File names are link targets, never the visible text.
PLAIN_FILES = {
    "phase1_gate_brief_screen.md": "scope check",
    "phase1_gate_feasibility.md": "feasibility warnings",
    "phase1_gate_roadmap.md": "the plan for this run",
    "phase1_step1_opportunities.md": "ranked opportunities",
    "phase1_step2_scoring.json": "scores for each opportunity",
    "phase1_step3_market.md": "what exists and what people pay",
    "phase1_step4_interview_guide.md": "customer interview guide",
    "phase2_step1_lean_canvas.md": "one-page business plan",
    "phase2_step2_landing_page.html": "landing page draft",
    "phase2_step3_demand_test.md": "sign-up test plan (results MOCKED)",
    "phase2_step4_publish_log.md": "landing page publish record (MOCKED)",
    "phase3_step1_mvp_spec.md": "what the first version contains",
    "phase3_step2_product.md": "the product draft",
    "phase3_step3_user_docs.md": "user guide draft",
    "phase3_gate_tests.md": "product test results",
    "phase4_step1_pricing.md": "price suggestion",
    "phase4_step2_policies.md": "draft policies (not legal advice)",
    "phase5_step1_store_page.html": "store page draft",
    "phase5_step2_launch_posts.md": "launch post drafts",
    "payment_test_log.md": "payment, delivery and refund test (MOCKED)",
    "phase5_step4_publish_log.md": "publish record (MOCKED)",
    "phase6_step1_support_faq.md": "support FAQ draft",
    "phase6_step2_pivot_memo.md": "carry on or change direction memo",
}
WARNING_WORDS = {
    "W-CUSTOMER": "the customer or the problem is not specific",
    "W-SIGNAL": "no sign of the problem was found in a quick search",
    "W-BUILD": "what the Build step can make for this product",
    "W-SKILLS": "a skill the idea needs is missing",
    "W-BUDGET": "the budget may not cover the cost",
    "W-TIME": "the time available may not cover the work",
    "W-VALUES": "the idea conflicts with one of your values",
}
RECOMMENDATION_WORDS = {"go": "go on", "repeat": "try again", "pivot": "change direction", "stop": "stop"}


def file_label(name: str) -> str:
    if name in PLAIN_FILES:
        return PLAIN_FILES[name]
    m = re.fullmatch(r"phase(\d)_gate_(plan_review|decision)\.md", name)
    if m:
        return ("checks set before the work started" if m.group(2) == "plan_review" else "the gate's findings and your decision")
    return name.rsplit(".", 1)[0].replace("_", " ")


def roadmap_rows(milestones: list[dict], files_by_phase: dict[int, list[str]] | None = None) -> list[dict]:
    """The roadmap as four plain columns: step, what happens, what you get, your decision."""
    rows = []
    for m in milestones:
        n = int(m.get("phase", 0))
        files = (files_by_phase or {}).get(n) or [f for f in m.get("deliverables") or [] if "_gate_" not in f]
        rows.append({"step": f"{n}. {STEP_TITLES.get(n, m.get('title', ''))}", "happens": STEP_HAPPENS.get(n, ""),
                     "gets": ", ".join(dict.fromkeys(file_label(f) for f in files)) or "the gate's findings",
                     "decision": STEP_DECISION.get(n, "")})
    return rows


def plain_reason(text: str) -> str:
    """A gate reason on one line with no nested brackets, in everyday words."""
    t = str(text).replace("threshold: ASSUMPTION", "our rule of thumb").replace("rule: ASSUMPTION", "our rule of thumb")
    t = t.replace("claims are ASSUMPTION", "claims have no source")
    t = re.sub(r"\bW-[A-Z]+\b", lambda m: WARNING_WORDS.get(m.group(0), m.group(0)), t)
    t = t.replace("no_evidence_collected", "No evidence was collected")
    t = re.sub(r"^missed: ", "Missed: ", t)
    depth, out = 0, []
    for ch in t:  # the first bracket becomes a colon, inner ones become commas
        if ch == "(":
            out.append(":" if depth == 0 else ",")
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        else:
            out.append(ch)
    t = re.sub(r"\s+([:,;])", r"\1", "".join(out))
    t = re.sub(r"([:,;])(?=\S)", r"\1 ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ;,")
    return t[:1].upper() + t[1:]


def carried(lines: list[str]) -> list[str]:
    """Lines carried from earlier decisions, with warning ids replaced by what they mean."""
    return [re.sub(r"\bW-[A-Z]+\b", lambda m: WARNING_WORDS.get(m.group(0), m.group(0)), x) for x in lines]


def headline(recommendation: str, reasons: list[str]) -> str:
    """One sentence: what the gate recommends and why."""
    word = RECOMMENDATION_WORDS.get(recommendation, recommendation)
    why = [plain_reason(r) for r in reasons if r and "recommend" not in r.lower() and not r.startswith("search:")]
    parts = why[0].split(": ") if why else [""]
    first = ": ".join(parts[:2]) if parts[0] == "Missed" and len(parts) > 1 else parts[0]
    if recommendation == "go":
        return "The gate recommends that you go on: every check it ran was met."
    if any("No evidence was collected" in w for w in why):
        return f"The gate recommends that you {word}: no evidence was collected, which says nothing about the idea itself."
    more = f" and {len(why) - 1} more" if len(why) > 1 else ""
    return f"The gate recommends that you {word}: {first[:1].lower() + first[1:]}{more}." if first else f"The gate recommends that you {word}."


def _whole(value: Any, unit: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "not estimated"
    return f"${round(value):,}" if unit == "$" else f"{round(value):,}"


def scoring_view(text: str, opportunities: list[dict]) -> dict | None:
    """The scoring file as a result page: a headline, one row per opportunity, the smallest-version card."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        return None
    titles = {o.get("id"): o.get("title") for o in opportunities}
    pick = data.get("pick")

    def points(v: Any, of: int) -> str:
        return f"{v} / {of}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "not scored"

    rows = []
    for r in data["scores"]:
        title = r.get("title") or titles.get(r.get("id")) or "an opportunity without a title"
        rows.append({"title": title, "kind": "a narrower version of your idea", "picked": bool(pick) and r.get("id") == pick,
                     "pain": points(r.get("pain"), 5), "pay": points(r.get("willingness_to_pay"), 5),
                     "reach": points(r.get("reach"), 5), "fit": points(r.get("founder_fit"), 5),
                     "total": points(r.get("total"), 20), "reason": " ".join(str(r.get("reason") or "").split())})
    result = data.get("result")
    if not result:  # a file written before the result line existed
        best = next((r for r in rows if r["picked"]), None)
        result = (f"Picked: {best['title']}, with {best['total'].replace(' / ', ' out of ')}." if best
                  else "No pick was made.")
    small = data.get("smallest_version")
    what = small.get("value") if isinstance(small, dict) else small
    scored = any(r["total"] != "not scored" for r in rows)
    return {"headline": result, "rows": rows, "scored": scored,
            "legend": "Scale: 1 no sign, 3 some sign, 5 strong sign.",
            "smallest": {"what": what if scored and what and what != "not scored" else "",
                         "cost": _whole(data.get("mvp_cost_usd"), "$"), "hours": _whole(data.get("mvp_hours_per_week"))},
            "not_measured": [] if scored else ["Pain, willingness to pay and reach were not scored, because no evidence was collected.",
                                              "The smallest version was not estimated."]}

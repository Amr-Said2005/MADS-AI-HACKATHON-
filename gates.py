"""Gate outcomes, thresholds and the rules the founder's choices must follow.

Every gate returns exactly one Outcome. Thresholds and budgets are constants fixed here
before any run; they are never tuned after seeing a run's result. Gates are code: agents
report findings, these functions compare them with the thresholds and decide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


# Requirement R-GATES: every gate in this module returns exactly one of these four outcomes;
# phase1_gate returns stop when two or more evidence thresholds are missed.
# Verified by: tests/test_gates.py::test_phase1_two_misses_stop_immediately
class Outcome(str, Enum):
    GO = "go"
    REPEAT = "repeat"
    PIVOT = "pivot"
    STOP = "stop"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class GateResult:
    gate: str
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    needs_approval: str | None = None  # human-readable summary when founder approval is required
    approval_payload: str = ""  # exact content the founder approves; hashed into the action id
    revised_brief: dict | None = None  # on PIVOT to SCREEN_BRIEF: the revised idea to screen

    def body_markdown(self, title: str, level: int = 1) -> str:
        """The checks table, then the reasons. `level` is the heading level of the title."""
        lines = [f"{'#' * level} {title}", ""]
        if self.checks:
            lines += ["| Check | Result | Detail |", "|---|---|---|"]
            for c in self.checks:
                lines.append(f"| {c.name} | {'pass' if c.passed else 'MISS'} | {c.detail} |")
            lines.append("")
        if self.reasons:
            lines += [f"{'#' * (level + 1)} Reasons", ""] + [f"- {r}" for r in self.reasons] + [""]
        lines.append("_Written by gates.py from this run's data; thresholds were set before the run._")
        return "\n".join(lines) + "\n"

    def to_markdown(self, title: str) -> str:
        """A gate file: the decision and its reasons on the first line, then the checks, then the reasons."""
        return decision_line(self.outcome, self.reasons) + "\n\n" + self.body_markdown(title)


DECISION_PREFIX = "Decision: "
_DECISION_REASON_CHARS = 400


def decision_line(outcome: "Outcome | str", reasons: list[str] | None = None) -> str:
    """The first line of every gate file: the decision in capitals, then the reasons in one sentence."""
    word = (outcome.value if isinstance(outcome, Outcome) else str(outcome)).upper()
    why = "; ".join(r.strip().rstrip(".") for r in (reasons or []) if r and r.strip())
    if len(why) > _DECISION_REASON_CHARS:
        why = why[:_DECISION_REASON_CHARS].rsplit(" ", 1)[0] + " ..."
    return f"{DECISION_PREFIX}{word}." + (f" Reasons: {why}." if why else "")


# ---------------------------------------------------------------- Phase 1 evidence gate

@dataclass(frozen=True)
class Phase1Thresholds:
    min_problem_sources: int = 3
    min_problem_domains: int = 3
    min_paying_sources: int = 1
    max_assumption_share: float = 0.5  # strictly fewer than half the claims may be ASSUMPTION


PHASE1 = Phase1Thresholds()
MAX_ATTEMPTS_PER_STATE = 2  # Phase 1 rule: first attempt plus one repeat
MAX_PIVOTS_PER_RUN = 1
MAX_BACK_EDGES = 2  # loop budget: each of revise, defects, repeat at most twice per phase
FEASIBILITY_MAX_REPEATS = 1  # one narrowed idea, approved by the founder
ROADMAP_MAX_REVISIONS = 2

UNCITED = "ASSUMPTION"  # printed next to every threshold that has no cited basis

# Phase 2 demand test target, fixed before any result exists. Results stay MOCKED.
# Basis: Kromatic, Landing Page Smoke Test (email sign-up 5-15% from targeted paid traffic;
# at least 200 unique visitors for a directional signal; runs of about 4 days to 2.5 weeks)
# https://kromatic.com/real-startup-book/2-evaluative-market-experiments/value-proposition-test/landing-page-smoke-test/
# and Unbounce, Conversion Benchmark Report 2024 (median landing page conversion 6.6%)
# https://unbounce.com/conversion-benchmark-report/
DEMAND_TARGET = {"min_signup_rate": 0.05, "min_cold_visitors": 200, "window_days": 14,
                 "sources": ["https://kromatic.com/real-startup-book/2-evaluative-market-experiments/value-proposition-test/landing-page-smoke-test/",
                             "https://unbounce.com/conversion-benchmark-report/"]}

PHASE_NAMES = {1: "Discover", 2: "Validate", 3: "Build", 4: "Commercial setup",
               5: "Sell/deliver/get paid", 6: "Operate and learn"}
PHASE_GATES = {1: "evidence threshold", 2: "evidence threshold, then founder review",
               3: "product tests pass", 4: "founder review of price and policies",
               5: "founder approval before any publish or send", 6: "metrics threshold"}


@dataclass
class Phase1Evidence:
    problem_source_urls: list[str]
    paying_source_urls: list[str]
    budget_usd: float
    hours_per_week: float
    mvp_cost_usd: float | None
    mvp_hours_per_week: float | None
    total_claims: int
    assumption_claims: int


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# Requirement R-PAY-EVIDENCE: the "someone already pays" check passes only with a paying source
# URL, and the caller passes in only fetched PAY claims whose page text holds a price found by code.
# Verified by: tests/test_agents.py::test_a_pay_claim_on_a_page_without_a_price_does_not_pass_the_gate
def phase1_gate(ev: Phase1Evidence, attempt: int, t: Phase1Thresholds = PHASE1) -> GateResult:
    domains = sorted({_domain(u) for u in ev.problem_source_urls})
    checks = [
        Check(
            "problem sources",
            len(set(ev.problem_source_urls)) >= t.min_problem_sources and len(domains) >= t.min_problem_domains,
            f"{len(set(ev.problem_source_urls))} fetched sources from {len(domains)} domains "
            f"(need {t.min_problem_sources} from {t.min_problem_domains}; threshold: ASSUMPTION)",
        ),
        Check(
            "someone already pays",
            len(set(ev.paying_source_urls)) >= t.min_paying_sources,
            f"{len(set(ev.paying_source_urls))} fetched PAY sources whose text contains a price found by code "
            f"(need {t.min_paying_sources})",
        ),
    ]
    if ev.mvp_cost_usd is None or ev.mvp_hours_per_week is None:
        checks.append(Check("founder can afford the smallest version", False, "scorer gave no estimate"))
    else:
        checks.append(
            Check(
                "founder can afford the smallest version",
                ev.budget_usd >= ev.mvp_cost_usd and ev.hours_per_week >= ev.mvp_hours_per_week,
                f"needs ${ev.mvp_cost_usd:g} and {ev.mvp_hours_per_week:g} h/week; "
                f"brief has ${ev.budget_usd:g} and {ev.hours_per_week:g} h/week",
            )
        )
    share = (ev.assumption_claims / ev.total_claims) if ev.total_claims else 1.0
    checks.append(
        Check(
            "claims mostly sourced",
            ev.total_claims > 0 and share < t.max_assumption_share,
            f"{ev.assumption_claims} of {ev.total_claims} claims are ASSUMPTION ({share:.0%}; must be under "
            f"{t.max_assumption_share:.0%}; threshold: ASSUMPTION)",
        )
    )
    misses = [c for c in checks if not c.passed]
    if not misses:
        return GateResult("phase1_evidence", Outcome.GO, ["all evidence thresholds met"], checks)
    reasons = [f"missed: {c.name} ({c.detail})" for c in misses]
    if len(misses) == 1 and attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase1_evidence", Outcome.REPEAT,
                          reasons + ["one miss: repeat once with new queries (rule: ASSUMPTION)"], checks)
    if len(misses) >= 2:
        reasons.append("two or more thresholds missed: the evidence is weak, recommend STOP")
    else:
        reasons.append("threshold still missed after the repeat: recommend STOP")
    return GateResult("phase1_evidence", Outcome.STOP, reasons, checks)


# ---------------------------------------------------------------- other gates

def brief_screen_gate(allowed: bool, reasons: list[str]) -> GateResult:
    return GateResult("brief_screen", Outcome.GO if allowed else Outcome.STOP, reasons)


def tests_gate(results: dict[str, bool], attempt: int) -> GateResult:
    """Phase 3: every product test must pass."""
    checks = [Check(name, ok, "pass" if ok else "fail") for name, ok in results.items()]
    if results and all(results.values()):
        return GateResult("phase3_tests", Outcome.GO, ["all product tests pass"], checks)
    failed = [n for n, ok in results.items() if not ok] or ["no tests were run"]
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase3_tests", Outcome.REPEAT, [f"failed: {', '.join(failed)}"], checks)
    return GateResult("phase3_tests", Outcome.STOP, [f"still failing after repeat: {', '.join(failed)}"], checks)


MAX_ASSUMPTION_SHARE = 0.5


def claims_gate(gate: str, labels: list[str], attempt: int, max_share: float = MAX_ASSUMPTION_SHARE) -> GateResult:
    """Fewer than max_share of the claims may be ASSUMPTION (used for the Phase 2 evidence check)."""
    total = len(labels)
    assumptions = sum(1 for l in labels if l == "ASSUMPTION")
    share = assumptions / total if total else 1.0
    check = Check("claims mostly sourced", total > 0 and share < max_share,
                  f"{assumptions} of {total} claims are ASSUMPTION ({share:.0%}; must be under {max_share:.0%}; "
                  "threshold: ASSUMPTION)")
    if check.passed:
        return GateResult(gate, Outcome.GO, ["evidence threshold met"], [check])
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult(gate, Outcome.REPEAT, [check.detail], [check])
    return GateResult(gate, Outcome.STOP, [check.detail, "still under threshold after repeat: recommend STOP"], [check])


MIN_SOURCED_PER_KEY_BOX = 1  # Lean Canvas: the Problem and Customer segments boxes must each cite Phase 1 evidence


def phase2_gate(problem_sourced: int, customer_sourced: int, landing_has_form: bool, demand_status: str,
                demand_detail: str, attempt: int) -> GateResult:
    """Phase 2 sign-off. The canvas and the page are checked by code. The demand result counts only
    when it was really measured; while the sign-up counter is MOCKED it is reported and not counted."""
    need = MIN_SOURCED_PER_KEY_BOX
    checks = [
        Check("canvas problem box cites Phase 1 evidence", problem_sourced >= need,
              f"{problem_sourced} sourced entries (need {need}; threshold: {UNCITED})"),
        Check("canvas customer box cites Phase 1 evidence", customer_sourced >= need,
              f"{customer_sourced} sourced entries (need {need}; threshold: {UNCITED})"),
        Check("landing page written with an email sign-up form", landing_has_form,
              "found" if landing_has_form else "the page is missing or has no email form"),
    ]
    notes = []
    if demand_status in ("met", "missed"):
        checks.append(Check("demand target", demand_status == "met", demand_detail))
    else:
        notes.append(f"demand test: not measured ({demand_detail}). The sign-up counter is MOCKED, so demand is NOT "
                     "validated and this check is not counted")
    failed = [c.name for c in checks if not c.passed]
    if not failed:
        return GateResult("phase2_evidence", Outcome.GO, ["canvas and landing page checks passed"] + notes, checks)
    reasons = [f"missed: {n}" for n in failed] + notes
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase2_evidence", Outcome.REPEAT, reasons + ["repeat once (rule: ASSUMPTION)"], checks)
    return GateResult("phase2_evidence", Outcome.STOP, reasons + ["still missed after the repeat: recommend STOP"], checks)


def approval_gate(gate: str, summary: str, payload: str, checks: list[Check] | None = None) -> GateResult:
    """Phases 2, 4 and 5 end in a founder decision. The orchestrator blocks until it exists."""
    return GateResult(gate, Outcome.GO, ["evidence checks passed; waiting for founder approval"], checks or [],
                      needs_approval=summary, approval_payload=payload)


def phase4_gate(price_usd: float | None, product_name: str, policies_text: str, attempt: int) -> GateResult:
    """Phase 4 sign-off before the founder review: a price, a name, and policies labelled as drafts."""
    checks = [
        Check("price set", bool(price_usd) and float(price_usd) > 0, f"price_usd = {price_usd!r}"),
        Check("product named", bool(str(product_name or "").strip()), f"product_name = {product_name!r}"),
        Check("policies labelled not legal advice", "not legal advice" in policies_text.lower(),
              "the draft policies must say they are not legal advice"),
    ]
    failed = [c.name for c in checks if not c.passed]
    if not failed:
        return GateResult("phase4_commercial", Outcome.GO, ["price, name and draft policies are in place"], checks)
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase4_commercial", Outcome.REPEAT, [f"missing: {', '.join(failed)}"], checks)
    return GateResult("phase4_commercial", Outcome.STOP, [f"still missing after repeat: {', '.join(failed)}"], checks)


def phase5_gate(store_page: str, posts: list, attempt: int) -> GateResult:
    """Phase 5 sign-off before the founder review: a store page and at least one post draft exist."""
    checks = [Check("store page drafted", bool(store_page.strip()), f"{len(store_page)} characters"),
              Check("launch posts drafted", bool(posts), f"{len(posts)} drafts")]
    failed = [c.name for c in checks if not c.passed]
    if not failed:
        return GateResult("phase5_launch", Outcome.GO, ["drafts ready; publishing waits for the founder"], checks)
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase5_launch", Outcome.REPEAT, [f"missing: {', '.join(failed)}"], checks)
    return GateResult("phase5_launch", Outcome.STOP, [f"still missing after repeat: {', '.join(failed)}"], checks)


def phase6_gate(metrics: list) -> GateResult:
    """Phase 6 sign-off: pivot or persevere, from metrics against targets set before launch.

    Each metric is {"name", "status": "met" | "missed" | "not_measured", "detail"}. Persevere needs every
    metric measured and met. A measured miss recommends pivot. With anything not measured the system does
    not recommend persevering: it recommends stop until the founder has measured it.
    """
    metrics = [m for m in metrics if isinstance(m, dict)] if isinstance(metrics, list) else []
    missed = [m for m in metrics if m.get("status") == "missed"]
    unmeasured = [m for m in metrics if m.get("status") not in ("met", "missed")]
    ok = bool(metrics) and not missed and not unmeasured
    detail = "; ".join(f"{m.get('name', 'metric')}: {str(m.get('status', 'not_measured')).replace('_', ' ')}" for m in metrics)
    checks = [Check("metrics measured and on target", ok, detail or "no metric was read")]
    if ok:
        return GateResult("phase6_metrics", Outcome.GO, ["every metric with a pre-set target is measured and meets it: persevere"], checks)
    if missed:
        return GateResult("phase6_metrics", Outcome.PIVOT,
                          [f"missed: {m.get('name')} ({m.get('detail', '')})" for m in missed]
                          + ["a measured metric missed its target: recommend PIVOT"], checks)
    return GateResult("phase6_metrics", Outcome.STOP,
                      ([f"not measured: {m.get('name')} ({m.get('detail', '')})" for m in unmeasured] or ["no metric was read"])
                      + ["no measured result supports persevering, so the system does not recommend it: recommend STOP "
                         "until the founder has measured demand for real",
                         "this is not a verdict on the idea: the counters are MOCKED in this prototype"], checks)


def with_open_defects(gate: GateResult, open_defects: list[str], can_pivot: bool) -> GateResult:
    """Defects budget used up: the artefact reaches sign-off with open defects; only pivot or stop."""
    if not open_defects or gate.outcome in (Outcome.PIVOT, Outcome.STOP):
        return gate
    outcome = Outcome.PIVOT if can_pivot else Outcome.STOP
    reasons = gate.reasons + [f"open defects after the defects budget: {', '.join(open_defects)}",
                              f"recommend {outcome.value.upper()}"]
    return GateResult(gate.gate, outcome, reasons, gate.checks)


# ---------------------------------------------------------------- feasibility (once per run)

# A digital product is anything delivered to the customer in digital form, with nothing shipped.
# `builds` says what the Build phase produces for that family; `noun` names the product in generated text.
@dataclass(frozen=True)
class Family:
    name: str
    builds: str
    noun: str
    synonyms: tuple[str, ...]

    @property
    def rx(self) -> re.Pattern[str]:
        return re.compile(r"\b(?:" + "|".join(self.synonyms) + r")\b", re.IGNORECASE)


PRODUCT_FAMILIES = (
    Family("Written content", "the content itself", "guide",
           (r"digital notes?", r"study notes?", r"lecture notes?", r"notes?", r"guides?", r"e-?books?", r"reports?",
            r"newsletters?", r"checklists?", r"worksheets?", r"printables?", r"handbooks?", r"cheat ?sheets?", r"workbooks?")),
    Family("Courses and learning", "the syllabus and lesson text; recording is left to the founder", "course",
           (r"online courses?", r"mini-?courses?", r"courses?", r"lesson series", r"lessons?", r"tutorials?", r"workshops?",
            r"quiz(?:zes)?", r"webinars?", r"masterclass(?:es)?", r"bootcamps?", r"curriculum")),
    Family("Templates and tools", "the templates as text or tables", "template pack",
           (r"template packs?", r"templates?", r"spreadsheets?", r"planners?", r"prompt packs?", r"swipe files?",
            r"trackers?", r"notion")),
    Family("Software", "a specification and the written content; this version ships no code", "software product",
           (r"mobile apps?", r"web ?apps?", r"apps?", r"web tools?", r"plug-?ins?", r"chatbots?", r"bots?", r"software",
            r"saas", r"browser extensions?", r"platforms?", r"web ?sites?", r"online services?", r"apis?")),
    Family("Media and design", "a specification and scripts; production is left to the founder", "media product",
           (r"videos?", r"audio", r"music", r"podcasts?", r"design assets?", r"stock (?:photos?|packs?|footage)",
            r"icon (?:packs?|sets?)", r"fonts?", r"presets?", r"illustrations?", r"sound effects?")),
    Family("Access", "the offer, structure and onboarding content", "membership",
           (r"memberships?", r"communit(?:y|ies)", r"paid groups?", r"private groups?", r"masterminds?", r"clubs?")),
)
FAMILY_NAMES = tuple(f.name for f in PRODUCT_FAMILIES)
UNSURE = "unsure"


def product_family(idea: str, model_family: str = "") -> tuple[str, str]:
    """(family, matched words). A word from the synonym lists wins over the model's answer; the family
    whose word comes first in the idea is taken. With no such word the model's answer is used, and an
    answer that names no family is "unsure". Nothing here stops a run."""
    best: tuple[int, Family, str] | None = None
    for fam in PRODUCT_FAMILIES:
        m = fam.rx.search(str(idea or ""))
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), fam, m.group(0))
    if best:
        return best[1].name, best[2]
    wanted = str(model_family or "").strip().lower()
    return next((f.name for f in PRODUCT_FAMILIES if f.name.lower() == wanted), UNSURE), ""


def family_of(name: str) -> Family | None:
    return next((f for f in PRODUCT_FAMILIES if f.name == name), None)


GENERIC_VALUES = {"", "everyone", "anyone", "people", "users", "customers", "businesses", "companies",
                  "the general public", "general public", "n/a", "na", "none", "unknown", "various", "anything"}
FEASIBILITY_WEEKS = 4
MIN_SKILL_COVERAGE = 0.5
MAX_SOFT_FAILS_FOR_REPEAT = 2  # one or two soft fails: repeat with a narrowed idea; three or more: stop


@dataclass
class FeasibilityInput:
    customer: str
    problem: str
    idea: str
    family: str  # the assessor's answer: one of FAMILY_NAMES or "unsure"
    physical_shipped: bool  # the assessor's answer to "is anything physical shipped to the customer?"
    problem_source_urls: list[str]  # registered (fetched) URLs only
    founder_skills: list[str]
    needed_skills: list[str]
    budget_usd: float
    mvp_cost_usd: float | None
    hours_per_week: float
    effort_hours: float | None
    founder_values: list[str]
    values_conflict: str


@dataclass
class FeasibilityRow:
    id: str
    criterion: str
    result: str  # pass, minor_warning, soft_fail, hard_fail
    measured: str
    threshold: str
    evidence: str


def _generic(text: str) -> bool:
    t = re.sub(r"[^a-z -]", "", str(text or "").lower()).strip()
    return t in GENERIC_VALUES or len(t) < 4


def _skill_covered(needed: str, founder: list[str]) -> bool:
    n = needed.lower().strip()
    return any(n in f.lower() or f.lower() in n for f in founder if f.strip())


def feasibility_rows(inp: FeasibilityInput) -> list[FeasibilityRow]:
    rows: list[FeasibilityRow] = []
    ok = not _generic(inp.customer) and not _generic(inp.problem)
    rows.append(FeasibilityRow("I1", "specific customer and problem", "pass" if ok else "soft_fail",
                               f"customer: {inp.customer or '-'}; problem: {inp.problem or '-'}",
                               "both named and not generic", "brief: idea, target_market"))
    family, word = product_family(inp.idea, inp.family)
    if inp.physical_shipped and not word:
        i2 = ("hard_fail", "something physical is shipped to the customer", "ASSUMPTION (assessor's answer)")
    elif family == UNSURE:
        i2 = ("minor_warning", "the format was not recognised; the founder should confirm it is delivered digitally",
              "ASSUMPTION (assessor's answer)")
    else:
        i2 = ("pass", f"{family}" + (f' (the idea says "{word}")' if word else ""),
              "brief: idea" if word else "ASSUMPTION (assessor's answer)")
    rows.append(FeasibilityRow("I2", "a digital product: delivered in digital form, nothing shipped", i2[0], i2[1],
                               "any of: " + ", ".join(FAMILY_NAMES) + "; an unrecognised format is a minor warning, never a stop",
                               i2[2]))
    urls = sorted(set(inp.problem_source_urls))
    rows.append(FeasibilityRow("I3", "quick signal that the problem exists", "pass" if urls else "soft_fail",
                               f"{len(urls)} fetched sources", "at least 1 (2 searches at most)",
                               urls[0] if urls else "ASSUMPTION"))
    needed = [s for s in inp.needed_skills if str(s).strip()]
    covered = [s for s in needed if _skill_covered(s, inp.founder_skills)]
    share = len(covered) / len(needed) if needed else 1.0
    rows.append(FeasibilityRow("F1", "founder skills cover the idea",
                               "pass" if share >= MIN_SKILL_COVERAGE else "soft_fail",
                               f"{len(covered)} of {len(needed)} needed skills ({share:.0%}); needed: "
                               f"{', '.join(needed) or 'none listed'}", f"at least {MIN_SKILL_COVERAGE:.0%} (threshold: ASSUMPTION)",
                               "brief: skills; needed skills are an ASSUMPTION"))
    if inp.mvp_cost_usd is None:
        f2 = ("hard_fail", "no estimate")
    else:
        f2 = ("pass" if inp.budget_usd >= inp.mvp_cost_usd else "hard_fail",
              f"budget ${inp.budget_usd:g}; estimate ${inp.mvp_cost_usd:g}")
    rows.append(FeasibilityRow("F2", "budget covers the smallest version", f2[0], f2[1],
                               "budget_usd >= estimated cost", "brief: budget_usd; cost is an ASSUMPTION"))
    available = inp.hours_per_week * FEASIBILITY_WEEKS
    if inp.effort_hours is None:
        f3 = ("hard_fail", "no estimate")
    elif available >= inp.effort_hours:
        f3 = ("pass", f"{available:g} h over {FEASIBILITY_WEEKS} weeks; estimate {inp.effort_hours:g} h")
    else:
        short_by_half = available < inp.effort_hours / 2
        f3 = ("hard_fail" if short_by_half else "soft_fail",
              f"{available:g} h over {FEASIBILITY_WEEKS} weeks; estimate {inp.effort_hours:g} h")
    rows.append(FeasibilityRow("F3", "time covers the effort", f3[0], f3[1],
                               f"hours_per_week x {FEASIBILITY_WEEKS} >= estimate (hard fail if short by more than half; "
                               "threshold: ASSUMPTION)",
                               "brief: hours_per_week; effort is an ASSUMPTION"))
    quote = str(inp.values_conflict or "").strip()
    if not quote:
        f4 = ("pass", "no conflict reported")
    elif any(quote.lower() == v.strip().lower() for v in inp.founder_values if v.strip()):
        f4 = ("hard_fail", f"conflicts with the stated value \"{quote}\"")
    else:
        f4 = ("soft_fail", f"conflict reported (\"{quote}\") but it matches no stated value")
    rows.append(FeasibilityRow("F4", "no conflict with the founder's values", f4[0], f4[1],
                               "no stated value in conflict", "brief: values"))
    return rows


def feasibility_gate(inp: FeasibilityInput, repeats_used: int) -> tuple[GateResult, list[FeasibilityRow]]:
    rows = feasibility_rows(inp)
    hard = [r for r in rows if r.result == "hard_fail"]
    soft = [r for r in rows if r.result == "soft_fail"]
    checks = [Check(f"{r.id} {r.criterion}", r.result in ("pass", "minor_warning"), r.measured) for r in rows]
    if hard:
        reasons = [f"hard fail {r.id}: {r.criterion} ({r.measured})" for r in hard] + ["any hard fail: STOP"]
        return GateResult("feasibility", Outcome.STOP, reasons, checks), rows
    if len(soft) > MAX_SOFT_FAILS_FOR_REPEAT:
        reasons = [f"soft fail {r.id}: {r.criterion}" for r in soft] + ["three or more soft fails: STOP (rule: ASSUMPTION)"]
        return GateResult("feasibility", Outcome.STOP, reasons, checks), rows
    if soft:
        reasons = [f"soft fail {r.id}: {r.criterion} ({r.measured})" for r in soft]
        if repeats_used >= FEASIBILITY_MAX_REPEATS:
            return GateResult("feasibility", Outcome.STOP,
                              reasons + ["still failing after the narrowed idea: STOP"], checks), rows
        return GateResult("feasibility", Outcome.REPEAT,
                          reasons + ["one or two soft fails: try a narrowed idea, if the founder approves it"],
                          checks), rows
    return GateResult("feasibility", Outcome.GO, ["idea is workable and the founder can carry it"], checks), rows


def feasibility_markdown(gate: GateResult, rows: list[FeasibilityRow], idea: str, narrowed_idea: str = "") -> str:
    lines = [decision_line(gate.outcome, gate.reasons), "", "# Feasibility check", "", f"Idea checked: {idea}", "",
             "Market demand is not judged here; it needs evidence and belongs to the Phase 1 gate.", "",
             "| Id | Criterion | Result | Measured | Threshold | Evidence |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.id} | {r.criterion} | {r.result} | {r.measured} | {r.threshold} | {r.evidence} |")
    lines += ["", "Estimates of needed skills, cost and effort come from the assessor and are an ASSUMPTION.", "",
              "## Reasons", ""] + [f"- {x}" for x in gate.reasons]
    if gate.outcome is Outcome.REPEAT and narrowed_idea:
        lines += ["", f"Narrowed idea proposed: {narrowed_idea}"]
    lines += ["", "_Decision made by gates.py; thresholds were set before the run._"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- roadmap (once per run)

_PROMISE = re.compile(
    r"(\$\s?\d|\d[\d,.]*\s*(?:k\s*)?(?:sales|sign-?ups|customers|subscribers|buyers|users|orders|downloads)\b"
    r"|\b(?:income|revenue|profit|earn(?:ings)?)\b)", re.IGNORECASE)


# Requirement R-NO-INCOME-CLAIMS: a roadmap milestone whose title or text names income, revenue,
# a money amount or a number of sales, sign-ups or customers is reported as a problem.
# Verified by: tests/test_gates.py::test_roadmap_validation
def validate_roadmap(milestones: list) -> tuple[list[dict], list[str]]:
    """Code-enforced roadmap shape. Returns the normalised milestones and any problems."""
    if not isinstance(milestones, list) or len(milestones) != 6:
        n = len(milestones) if isinstance(milestones, list) else 0
        return [], [f"need exactly six milestones, one per phase; got {n}"]
    problems: list[str] = []
    out: list[dict] = []
    for i, m in enumerate(milestones, start=1):
        m = m if isinstance(m, dict) else {}
        if m.get("phase") != i:
            problems.append(f"milestone {i} has phase {m.get('phase')!r}; milestones must be in phase order 1-6")
        for key in ("title", "founder_sees", "decision_asked"):
            if not str(m.get(key, "")).strip():
                problems.append(f"M{i} has no {key}")
        text = " ".join(str(m.get(k, "")) for k in ("title", "founder_sees", "decision_asked"))
        hit = _PROMISE.search(text)
        if hit:
            problems.append(f"M{i} promises income, sales or sign-up numbers: {hit.group(0)!r}")
        deliverables = m.get("deliverables") or []
        out.append({"id": f"M{i}", "phase": i, "title": str(m.get("title", "")).strip(),
                    "deliverables": [str(d) for d in deliverables] if isinstance(deliverables, list) else [],
                    "founder_sees": str(m.get("founder_sees", "")).strip(),
                    "decision_asked": str(m.get("decision_asked", "")).strip(),
                    "gate": PHASE_GATES[i]})  # set by code: no milestone loses its gate
    return out, problems


def roadmap_markdown(milestones: list[dict]) -> str:
    lines = ["# Milestone roadmap", "",
             "Six milestones, one per phase. Each keeps its gate and ends in a founder review.", "",
             "| Id | Phase | Title | Deliverables | Founder sees | Decision asked | Gate |",
             "|---|---|---|---|---|---|---|"]
    for m in milestones:
        lines.append(f"| {m['id']} | {m['phase']}: {PHASE_NAMES[m['phase']]} | {m['title']} | "
                     f"{', '.join(m['deliverables']) or '-'} | {m['founder_sees']} | {m['decision_asked']} | {m['gate']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- plans and milestone reviews

# Product acceptance tests for Phase 3, run by code on the finished product (agents/builder.py).
MIN_SECTION_CHARS = 250
MAX_PRODUCT_CHARS = 20_000
PRODUCT_ACCEPTANCE = {
    "every section in the spec is present": "each spec section title appears as a section of the product",
    "every section has enough content": f"each section has at least {MIN_SECTION_CHARS} characters (threshold: {UNCITED})",
    "the product is short enough to review": f"at most {MAX_PRODUCT_CHARS} characters in total (threshold: {UNCITED})",
    "no placeholder text": "no 'lorem ipsum', 'TODO', 'TBD' or '[insert ...]' left in the text",
}

# What each phase's sign-off gate checks, by the name of the Check it produces. Code writes these into
# every plan, so the numbers in a plan are the gate's numbers and a model never retypes them.
GATE_CRITERIA: dict[int, list[tuple[str, str]]] = {
    1: [("problem sources", f"at least {PHASE1.min_problem_sources} fetched SAY sources from {PHASE1.min_problem_domains} different domains"),
        ("someone already pays", f"at least {PHASE1.min_paying_sources} fetched PAY source whose text contains a price found by code"),
        ("founder can afford the smallest version", "the brief's budget and weekly hours cover the scorer's estimate"),
        ("claims mostly sourced", f"fewer than {PHASE1.max_assumption_share:.0%} of the counted claims are ASSUMPTION")],
    2: [("canvas problem box cites Phase 1 evidence", "at least 1 sourced entry"),
        ("canvas customer box cites Phase 1 evidence", "at least 1 sourced entry"),
        ("landing page written with an email sign-up form", "the page exists and has an email form")],
    3: [(name, "passes") for name in PRODUCT_ACCEPTANCE],
    4: [("price set", "a price above 0"), ("product named", "a product name"),
        ("policies labelled not legal advice", "the label is in the policies file")],
    5: [("store page drafted", "the page exists"), ("launch posts drafted", "at least 1 draft")],
    6: [("metrics measured and on target", "every metric with a pre-set target is measured and meets it")],
}


def gate_criteria(phase: int) -> list[dict]:
    """The sign-off gate's checks as plan criteria G1..Gn (method code)."""
    return [{"id": f"G{i}", "check": name, "threshold": threshold, "method": "code", "by": "gate"}
            for i, (name, threshold) in enumerate(GATE_CRITERIA.get(phase, []), start=1)]


PLAN_METHODS = ("code", "source")
MAX_PLAN_CRITERIA = 8


def check_plan(criteria: list) -> list[str]:
    """Plan review, code part: every criterion has an id, a check, a threshold and a method."""
    if not isinstance(criteria, list) or not criteria:
        return ["the plan has no criteria"]
    if len(criteria) > MAX_PLAN_CRITERIA:
        return [f"the plan has {len(criteria)} criteria; at most {MAX_PLAN_CRITERIA} (one checker call verifies them all)"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, c in enumerate(criteria, start=1):
        c = c if isinstance(c, dict) else {}
        cid = str(c.get("id", "")).strip()
        if not cid or cid in seen:
            problems.append(f"criterion {i} has a missing or duplicate id")
        seen.add(cid)
        for key in ("check", "threshold"):
            if not str(c.get(key, "")).strip():
                problems.append(f"criterion {cid or i} has no {key}")
        if c.get("method") not in PLAN_METHODS:
            problems.append(f"criterion {cid or i} must be measured by code or by a fetched source")
    return problems


CONTINUE, REQUEST_CHANGES, PIVOT, STOP = "continue", "request_changes", "pivot", "stop"


# Requirement R-FOUNDER-GATE: "continue" is among the choices offered at a milestone review only
# when the gate recommended go; after a pivot or stop recommendation it is left out.
# Verified by: tests/test_inner_loop.py::test_continue_is_never_offered_after_a_failed_gate
def milestone_choices(recommendation: Outcome, repeat_left: bool, can_pivot: bool) -> list[str]:
    """What the founder may choose at a milestone review.

    The founder can always be more cautious than the system, never less: "continue" is
    offered only when the gate recommended go.
    """
    if recommendation is Outcome.GO:
        order = [CONTINUE, REQUEST_CHANGES, PIVOT, STOP]
    elif recommendation is Outcome.PIVOT:
        order = [PIVOT, REQUEST_CHANGES, STOP]
    else:
        order = [STOP, PIVOT, REQUEST_CHANGES]
    return [c for c in order if (c != REQUEST_CHANGES or repeat_left) and (c != PIVOT or can_pivot)]

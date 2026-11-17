"""Gate outcomes, thresholds and the rules the founder's choices must follow.

Every gate returns exactly one Outcome. Thresholds and budgets are constants fixed here
before any run; they are never tuned after seeing a run's result. Gates are code: agents
report findings, these functions compare them with the thresholds and decide.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
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
    measured: bool = True  # False: there was nothing to measure. Such a check is unverified, never a miss.

    @property
    def result(self) -> str:
        return "not measured" if not self.measured else ("pass" if self.passed else "MISS")


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
                lines.append(f"| {c.name} | {c.result} | {c.detail} |")
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
FEASIBILITY_MAX_REPEATS = 2  # the founder may revise the idea or the inputs twice
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
    search_notes: list[str] = field(default_factory=list)  # each search adapter and why it returned nothing
    core_skill_missing: bool = False


NO_EVIDENCE = "no_evidence_collected"
NOT_A_VERDICT = "this is not a verdict on the idea: nothing was found to measure it with"


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# Requirement R-PAY-EVIDENCE: the "someone already pays" check passes only with a paying source
# URL, and the caller passes in only fetched PAY claims whose page text holds a price found by code.
# Verified by: tests/test_agents.py::test_a_pay_claim_on_a_page_without_a_price_does_not_pass_the_gate
def phase1_gate(ev: Phase1Evidence, attempt: int, t: Phase1Thresholds = PHASE1) -> GateResult:
    domains = sorted({_domain(u) for u in ev.problem_source_urls})
    if ev.total_claims - ev.assumption_claims <= 0 and not ev.problem_source_urls and not ev.paying_source_urls:
        # No fetched page supports any claim. That is one cause, not three missed thresholds, and it says
        # nothing about the idea: the thresholds are not evaluated.
        checks = [Check(name, False, "not measured: no evidence was collected", measured=False)
                  for name in ("problem sources", "someone already pays", "founder can afford the smallest version",
                               "claims mostly sourced")]
        reasons = [f"{NO_EVIDENCE}: no fetched page supports any claim, so the evidence thresholds were not evaluated"]
        reasons += [f"search: {n}" for n in ev.search_notes] or ["search: no adapter reported an error; the pages found did not describe the problem"]
        reasons.append(NOT_A_VERDICT)
        if attempt < MAX_ATTEMPTS_PER_STATE:
            return GateResult("phase1_evidence", Outcome.REPEAT, reasons + ["repeat once with new searches"], checks)
        return GateResult("phase1_evidence", Outcome.STOP, reasons + ["still no evidence after the repeat: recommend STOP"], checks)
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
                f"brief has ${ev.budget_usd:g} and {ev.hours_per_week:g} h/week"
                + ("; the cost estimate excludes paid help for the skills the founder lacks" if ev.core_skill_missing else ""),
            )
        )
    if ev.total_claims:
        share = ev.assumption_claims / ev.total_claims
        checks.append(Check("claims mostly sourced", share < t.max_assumption_share,
                            f"{ev.assumption_claims} of {ev.total_claims} claims are ASSUMPTION ({share:.0%}; must be under "
                            f"{t.max_assumption_share:.0%}; threshold: ASSUMPTION)"))
    else:  # nothing to take a share of: unverified, and not one of the misses
        checks.append(Check("claims mostly sourced", False, "not measured: no claims were counted", measured=False))
    misses = [c for c in checks if c.measured and not c.passed]
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
    skill_kinds: dict[str, str] = field(default_factory=dict)  # skill name -> "core" or "supporting"
    timeframe_weeks: float | None = None  # set by the founder; None means it was not given
    delegated_hours_per_week: float = 0.0


MAJOR, MINOR, NONE = "major", "minor", "none"
MAJOR_BUILD_FAMILIES = ("Software", "Media and design")  # the Build phase writes a specification for these, not the product
BUDGET_LITTLE_LEFT = 0.2  # "little left": less than a fifth of the budget remains after the estimated cost (ASSUMPTION)
FEASIBILITY_CHOICES = ("continue", "revise", "stop")
SOLO_NOTE = "The estimate assumes one person working alone; the founder's circumstances may differ."


@dataclass
class FeasibilityWarning:
    """One line of the feasibility check. It states a fact and ways to address it; it is never a verdict."""
    id: str
    level: str  # major, minor or none
    looks_at: str
    fact: str
    evidence: str  # a registered URL, the brief field used, or ASSUMPTION
    ways: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _generic(text: str) -> bool:
    t = re.sub(r"[^a-z -]", "", str(text or "").lower()).strip()
    return t in GENERIC_VALUES or len(t) < 4


def _skill_covered(needed: str, founder: list[str]) -> bool:
    n = needed.lower().strip()
    return any(n in f.lower() or f.lower() in n for f in founder if f.strip())


def missing_skills(inp: FeasibilityInput) -> tuple[list[str], list[str]]:
    """(missing core skills, missing supporting skills). A skill with no kind counts as core."""
    missing = [s for s in inp.needed_skills if str(s).strip() and not _skill_covered(s, inp.founder_skills)]
    core = [s for s in missing if inp.skill_kinds.get(s, "core") != "supporting"]
    return core, [s for s in missing if s not in core]


def feasibility_warnings(inp: FeasibilityInput) -> list[FeasibilityWarning]:
    """The seven warnings, in a fixed order. Market demand is not looked at: that belongs to the Phase 1 gate."""
    out: list[FeasibilityWarning] = []

    vague = [name for name, value in (("customer", inp.customer), ("problem", inp.problem)) if _generic(value)]
    out.append(FeasibilityWarning(
        "W-CUSTOMER", MINOR if vague else NONE, "the customer and the problem named in the brief",
        f"customer: {inp.customer or '-'}; problem: {inp.problem or '-'}"
        + (f"; the {' and the '.join(vague)} {'is' if len(vague) == 1 else 'are'} missing or generic" if vague else ""),
        "brief: idea, target_market",
        ["Revise the idea to name one specific kind of customer and the one problem they have."] if vague else []))

    urls = sorted(set(inp.problem_source_urls))
    out.append(FeasibilityWarning(
        "W-SIGNAL", NONE if urls else MINOR, "a quick search (2 at most) for a sign that the problem exists",
        f"{len(urls)} fetched page(s) describe the problem" if urls else "no fetched page describes the problem",
        urls[0] if urls else "ASSUMPTION",
        [] if urls else ["Nothing has to change now: Phase 1 searches more widely and its evidence gate decides.",
                         "Or revise the idea to say where these customers talk about the problem."]))

    family, word = product_family(inp.idea, inp.family)
    fam = family_of(family)
    if family in MAJOR_BUILD_FAMILIES:
        level = MAJOR
        ways = ["Plan who makes the product itself: the founder, a contractor or an AI tool. This system writes its "
                "specification and written content only.",
                "Or revise the idea to a first version made of written content, a course or templates."]
    elif family == UNSURE:
        level = MINOR
        ways = ["Confirm that the product is delivered in digital form, and revise the idea to name its format "
                "(for example notes, a course, templates, an app)."]
    else:
        level, ways = NONE, []
    out.append(FeasibilityWarning(
        "W-BUILD", level, "the product family",
        (f"family: {family}" + (f' (the idea says "{word}")' if word else "")
         + (f"; the Build phase produces {fam.builds}" if fam else "; the format was not recognised")),
        "brief: idea" if word else "ASSUMPTION (assessor's answer)", ways))

    core, supporting = missing_skills(inp)
    needed = [s for s in inp.needed_skills if str(s).strip()]
    held = [s for s in needed if s not in core and s not in supporting]
    out.append(FeasibilityWarning(
        "W-SKILLS", MAJOR if core else (MINOR if supporting else NONE), "the skills the idea needs, against the founder's skills",
        f"needed: {', '.join(needed) or 'none listed'}; held: {', '.join(held) or 'none'}; "
        f"missing to make the product: {', '.join(core) or 'none'}; missing supporting skills: {', '.join(supporting) or 'none'}",
        "brief: skills; the needed skills are an ASSUMPTION",
        skill_ways(inp, core + supporting)))

    budget, cost = inp.budget_usd, inp.mvp_cost_usd
    if cost is None:
        b = (MINOR, f"budget ${budget:g}; no cost estimate was produced")
    elif budget < cost:
        b = (MAJOR, f"budget ${budget:g}; estimated cost of the smallest version ${cost:g} (ASSUMPTION); short by ${cost - budget:g}")
    elif cost > 0 and budget - cost < BUDGET_LITTLE_LEFT * budget:
        b = (MINOR, f"budget ${budget:g}; estimated cost of the smallest version ${cost:g} (ASSUMPTION); ${budget - cost:g} left")
    else:
        b = (NONE, f"budget ${budget:g}; estimated cost of the smallest version ${cost:g} (ASSUMPTION)" if cost is not None else "")
    out.append(FeasibilityWarning(
        "W-BUDGET", b[0], "the budget, against the estimated cost of the smallest version", b[1],
        "brief: budget_usd; the cost is an ASSUMPTION",
        ["Raise the budget.", "Narrow the first version so it costs less.",
         "Use free tools where they are enough (ASSUMPTION that they are)."] if b[0] != NONE else []))

    available, frame = available_hours(inp)
    effort = inp.effort_hours
    if effort is None:
        t = (MINOR, f"{available:g} hours available ({frame}); no effort estimate was produced")
    else:
        gap = max(effort - available, 0)
        fact = f"{available:g} hours available ({frame}); estimated effort {effort:g} hours (ASSUMPTION); gap {gap:g} hours"
        t = (MAJOR if available < effort / 2 else (MINOR if available < effort else NONE), fact)
    out.append(FeasibilityWarning(
        "W-TIME", t[0], "the hours available in the founder's time frame, against the estimated effort",
        t[1] + ". " + SOLO_NOTE, "brief: hours_per_week, timeframe_weeks, delegated_hours_per_week; the effort is an ASSUMPTION",
        ["Delegate part of the work.", "Extend the time frame.", "Use AI tools for part of the work.",
         "Narrow the scope of the first version."] if t[0] != NONE else []))

    quote = str(inp.values_conflict or "").strip()
    conflict = bool(quote) and any(quote.lower() == v.strip().lower() for v in inp.founder_values if v.strip())
    out.append(FeasibilityWarning(
        "W-VALUES", MAJOR if conflict else NONE, "the idea, against the founder's stated values",
        f'the assessor selected the stated value "{quote}" as in conflict with the idea' if conflict else "no stated value is in conflict",
        "brief: values; the conflict is the assessor's answer (ASSUMPTION)",
        ["Revise the idea so it no longer conflicts with that value.", "Or stop."] if conflict else []))
    return out


def skill_ways(inp: FeasibilityInput, missing: list[str]) -> list[str]:
    """Three ways to cover each missing skill: people, AI tools, learning. Each says whether the stated budget
    and time frame leave room for it. No skill gets a score, and no option is described as sure to work."""
    if not missing:
        return []
    money_left = inp.budget_usd - (inp.mvp_cost_usd or 0)
    hours, _frame = available_hours(inp)
    hours_left = hours - (inp.effort_hours or 0)
    if money_left > 0:
        money = (f"After the estimated cost of the smallest version, ${money_left:g} of the ${inp.budget_usd:g} budget is left "
                 "(ASSUMPTION), so there is room only for help that costs less than that; what help costs is an ASSUMPTION.")
    else:
        money = ("The estimated cost of the smallest version already uses the whole budget (ASSUMPTION), so the budget "
                 "leaves no room for paid help unless it is raised.")
    if hours_left > 0:
        time = (f"After the estimated effort, {hours_left:g} of the {hours:g} hours in the time frame are left (ASSUMPTION), "
                "so there is room only if it takes less than that; how long it takes is an ASSUMPTION.")
    else:
        time = ("The estimated effort already uses all the hours in the time frame (ASSUMPTION), so the time frame "
                "leaves no room for it unless it is extended or work is delegated.")
    ways = []
    for skill in missing:
        role = "supporting" if inp.skill_kinds.get(skill, "core") == "supporting" else "needed to make the product"
        ways += [
            f"{skill} ({role}), people: contract or hire someone for that part, paid from the budget. {money}",
            f"{skill} ({role}), AI tools: use an AI tool to do or assist with that part. Its output needs the founder's "
            f"check and may not be enough. A paid tool comes out of the same budget. {money}",
            f"{skill} ({role}), learning: learn it, with the time that adds. {time}",
        ]
    return ways


def available_hours(inp: FeasibilityInput) -> tuple[float, str]:
    """(hours available in the time frame, how they were worked out)."""
    weeks = inp.timeframe_weeks or FEASIBILITY_WEEKS
    weekly = inp.hours_per_week + inp.delegated_hours_per_week
    frame = f"{inp.hours_per_week:g} of the founder's hours"
    frame += f" plus {inp.delegated_hours_per_week:g} delegated hours" if inp.delegated_hours_per_week else ""
    frame += f" a week over {weeks:g} weeks"
    if not inp.timeframe_weeks:
        frame += f"; no time frame was given, so {FEASIBILITY_WEEKS} weeks are used (ASSUMPTION)"
    return weekly * weeks, frame


def warning_counts(warnings: list[FeasibilityWarning]) -> tuple[int, int]:
    return sum(w.level == MAJOR for w in warnings), sum(w.level == MINOR for w in warnings)


def feasibility_recommendation(warnings: list[FeasibilityWarning]) -> str:
    """What the screen recommends. It is shown to the founder and never enforced."""
    majors, _ = warning_counts(warnings)
    return "Proceed" if majors == 0 else ("Proceed with care, or revise" if majors == 1 else "Revise or stop")


def continued_line(majors: int) -> str:
    """The line carried by the roadmap and every later milestone review after the founder continued."""
    return f"Continued with {majors} major feasibility warning{'' if majors == 1 else 's'}"


def feasibility_markdown(warnings: list[FeasibilityWarning], idea: str, inp: FeasibilityInput) -> str:
    """The feasibility report: the product family, one entry per warning, the ways to address each, and the
    recommendation shown. The founder's choice is added below it when it is made."""
    majors, minors = warning_counts(warnings)
    order = {MAJOR: 0, MINOR: 1, NONE: 2}
    ranked = sorted(warnings, key=lambda w: order[w.level])  # major first; the sort keeps the table order inside a level
    family, word = product_family(inp.idea, inp.family)
    fam = family_of(family)
    lines = [decision_line("pending", [f"{majors} major and {minors} minor warning{'' if minors == 1 else 's'}; the founder decides"]), "",
             "# Feasibility check", "", f"Idea checked: {idea}", "",
             f"Product family: {family}" + (f' (the idea says "{word}")' if word else "")
             + (f". The Build phase produces {fam.builds}." if fam else ". The format was not recognised."), "",
             "This check gives warnings, never a verdict. It does not assess market demand; that needs evidence and "
             "belongs to the Phase 1 gate. Estimates of needed skills, cost and effort come from the assessor and are "
             "an ASSUMPTION.", "",
             "| Id | Level | Looks at | Fact | Evidence |", "|---|---|---|---|---|"]
    for w in ranked:
        lines.append(f"| {w.id} | {w.level} | {w.looks_at} | {w.fact} | {w.evidence} |")
    flagged = [w for w in ranked if w.level != NONE]
    if flagged:
        lines += ["", "## Ways to address each warning", ""]
        for w in flagged:
            lines += [f"### {w.id} ({w.level})", ""] + [f"- {x}" for x in w.ways] + [""]
    lines += ["## Recommendation shown to the founder (not enforced)", "", feasibility_recommendation(warnings) + ".", "",
              "_Warnings computed by gates.py; the founder decides whether to continue, revise or stop._"]
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
# A waiver is the founder's signed decision to go on against a pivot or stop recommendation. It exists only
# at the evidence gates below; the Phase 3 test gate, the scope screen, the never-list and publish or send
# approvals cannot be waived. It changes no threshold and marks no criterion as passed.
WAIVER = "waiver"
EVIDENCE_GATE_PHASES = (1, 2, 6)


def waiver_line(recommendation: str, milestone: int, criteria: list[str]) -> str:
    return (f"Continued against a {recommendation.upper()} recommendation at M{milestone}: "
            f"{', '.join(criteria) or 'no criterion was named'}")


# Requirement R-FOUNDER-GATE: "continue" is among the choices offered at a milestone review only
# when the gate recommended go; after a pivot or stop recommendation it is left out.
# Verified by: tests/test_inner_loop.py::test_continue_is_never_offered_after_a_failed_gate
def milestone_choices(recommendation: Outcome, repeat_left: bool, can_pivot: bool, waivable: bool = False) -> list[str]:
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
    if waivable and recommendation is not Outcome.GO:
        order.append(WAIVER)  # plain "continue" is still not offered; the waiver needs a written reason
    return [c for c in order if (c != REQUEST_CHANGES or repeat_left) and (c != PIVOT or can_pivot)]

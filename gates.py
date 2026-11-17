"""Gate outcomes and thresholds.

Every orchestrator state ends in a gate that returns exactly one Outcome.
Thresholds are fixed here before any run; they are never tuned after seeing a run's result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


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

    def to_markdown(self, title: str) -> str:
        lines = [f"# {title}", "", f"**Outcome: {self.outcome.value.upper()}**", ""]
        if self.checks:
            lines += ["| Check | Result | Detail |", "|---|---|---|"]
            for c in self.checks:
                lines.append(f"| {c.name} | {'pass' if c.passed else 'MISS'} | {c.detail} |")
            lines.append("")
        if self.reasons:
            lines += ["## Reasons", ""] + [f"- {r}" for r in self.reasons] + [""]
        lines.append("_Written by gates.py from this run's data; thresholds were set before the run._")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Phase 1 evidence gate

@dataclass(frozen=True)
class Phase1Thresholds:
    min_problem_sources: int = 3
    min_problem_domains: int = 3
    min_paying_sources: int = 1
    max_assumption_share: float = 0.5  # strictly fewer than half the claims may be ASSUMPTION


PHASE1 = Phase1Thresholds()
MAX_ATTEMPTS_PER_STATE = 2  # first attempt plus one repeat
MAX_PIVOTS_PER_RUN = 1


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


def phase1_gate(ev: Phase1Evidence, attempt: int, t: Phase1Thresholds = PHASE1) -> GateResult:
    domains = sorted({_domain(u) for u in ev.problem_source_urls})
    checks = [
        Check(
            "problem sources",
            len(set(ev.problem_source_urls)) >= t.min_problem_sources and len(domains) >= t.min_problem_domains,
            f"{len(set(ev.problem_source_urls))} fetched sources from {len(domains)} domains "
            f"(need {t.min_problem_sources} from {t.min_problem_domains})",
        ),
        Check(
            "someone already pays",
            len(set(ev.paying_source_urls)) >= t.min_paying_sources,
            f"{len(set(ev.paying_source_urls))} fetched sources show paid comparable solutions "
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
            f"{t.max_assumption_share:.0%})",
        )
    )
    misses = [c for c in checks if not c.passed]
    if not misses:
        return GateResult("phase1_evidence", Outcome.GO, ["all evidence thresholds met"], checks)
    reasons = [f"missed: {c.name} ({c.detail})" for c in misses]
    if len(misses) == 1 and attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult("phase1_evidence", Outcome.REPEAT, reasons + ["one miss: repeat once with new queries"], checks)
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
                  f"{assumptions} of {total} claims are ASSUMPTION ({share:.0%}; must be under {max_share:.0%})")
    if check.passed:
        return GateResult(gate, Outcome.GO, ["evidence threshold met"], [check])
    if attempt < MAX_ATTEMPTS_PER_STATE:
        return GateResult(gate, Outcome.REPEAT, [check.detail], [check])
    return GateResult(gate, Outcome.STOP, [check.detail, "still under threshold after repeat: recommend STOP"], [check])


def approval_gate(gate: str, summary: str, payload: str, checks: list[Check] | None = None) -> GateResult:
    """Phases 2, 4 and 5 end in a founder decision. The orchestrator blocks until it exists."""
    return GateResult(gate, Outcome.GO, ["evidence checks passed; waiting for founder approval"], checks or [],
                      needs_approval=summary, approval_payload=payload)

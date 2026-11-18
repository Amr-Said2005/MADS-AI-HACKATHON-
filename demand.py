"""MOCKED sign-up counter for the Phase 2 demand test.

In this prototype the landing page is never really published, so no visitor can arrive
and no sign-up can be collected. This counter is the only place a sign-up number may come
from. It reads zero and nothing in the pipeline adds to it: the system never invents
sign-ups. `evaluate` is real code: it compares a count with the target set in gates.py,
and it reports "not_measured" until enough cold visitors have been counted.
"""

from __future__ import annotations

from dataclasses import dataclass

# Requirement R-MOCK-LABEL: this flag marks the sign-up counter as a mock; the count's source text
# says "MOCKED counter", and the README status table names the module.
# Verified by: tests/test_structure.py::test_every_mocked_module_is_named_in_the_readme_table
MOCKED = True


@dataclass(frozen=True)
class SignupCount:
    visitors: int = 0
    signups: int = 0
    source: str = "MOCKED counter (demand.py): the page is not published, so nothing is collected"


# Requirement R-NO-FAKE-PROOF (MOCKED): the counter returns zero visitors and zero sign-ups for
# every run, and no code adds to it, so no sign-up number is invented.
# Verified by: tests/test_phase2.py::test_signup_counter_is_mocked_and_reads_zero
def read_counter(run_id: str) -> SignupCount:
    """MOCKED: there is no published page and no collection, so the count is zero for every run."""
    return SignupCount()


FOUNDER_REPORTED = "founder-reported, not verified by the system"


def founder_reported(items: list[dict]) -> dict | None:
    """The newest count the founder reported for their own test (visitors, sign-ups, days), or None."""
    for e in reversed(items or []):
        if e.get("kind") == "demand" and e.get("status") == "founder-reported":
            return e
    return None


def evaluate_reported(target: dict, visitors: int, signups: int, days: int) -> tuple[str, str]:
    """Compare a founder-reported count with the same target. A test that ran longer than the target's
    window cannot be read against it, so it stays not measured. Every detail carries the label."""
    window = int(target["window_days"])
    if days > window:
        return "not_measured", (f"the reported test ran {days} days, longer than the {window}-day window of the target, "
                                f"so it is not read against it ({FOUNDER_REPORTED})")
    status, detail = evaluate(target, visitors, signups)
    return status, f"{detail}, in {days} days ({FOUNDER_REPORTED})"


def evaluate(target: dict, visitors: int, signups: int) -> tuple[str, str]:
    """Compare a count with the demand target. Returns (status, detail); status is not_measured, met or missed."""
    need = int(target["min_cold_visitors"])
    rate_needed = float(target["min_signup_rate"])
    if visitors < need:
        return "not_measured", f"{visitors} of the {need} cold visitors needed have been counted, so no rate can be read"
    rate = signups / visitors
    status = "met" if rate >= rate_needed else "missed"
    return status, f"{signups} sign-ups from {visitors} cold visitors ({rate:.1%}; target at least {rate_needed:.0%})"

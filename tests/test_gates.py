import pytest

import gates
from gates import Outcome, Phase1Evidence
from orchestrator import TERMINAL, TRANSITIONS, State, next_state

NON_TERMINAL = [s for s in State if s not in TERMINAL]


@pytest.mark.parametrize("state", NON_TERMINAL)
@pytest.mark.parametrize("outcome", list(Outcome))
def test_every_state_and_outcome_leads_to_a_defined_state(state, outcome):
    nxt, _ = next_state(state, outcome, attempts=1, pivots=0)
    assert isinstance(nxt, State)


def test_transition_table_covers_every_non_terminal_state():
    assert set(TRANSITIONS) == set(NON_TERMINAL)
    for state, row in TRANSITIONS.items():
        assert row[Outcome.STOP] is State.STOPPED, state
        assert Outcome.GO in row, state


def test_table_matches_plan():
    assert TRANSITIONS[State.SCREEN_BRIEF][Outcome.GO] is State.P1_DISCOVER
    assert TRANSITIONS[State.P1_DISCOVER][Outcome.PIVOT] is State.SCREEN_BRIEF
    assert TRANSITIONS[State.P2_VALIDATE][Outcome.PIVOT] is State.P1_DISCOVER
    assert TRANSITIONS[State.P6_OPERATE][Outcome.GO] is State.DONE
    assert TRANSITIONS[State.P6_OPERATE][Outcome.PIVOT] is State.P1_DISCOVER


def test_undefined_outcome_stops():
    assert next_state(State.SCREEN_BRIEF, Outcome.REPEAT, 1, 0)[0] is State.STOPPED
    assert next_state(State.P6_OPERATE, Outcome.REPEAT, 1, 0)[0] is State.STOPPED


def test_repeat_and_pivot_limits():
    assert next_state(State.P1_DISCOVER, Outcome.REPEAT, 1, 0)[0] is State.P1_DISCOVER
    assert next_state(State.P1_DISCOVER, Outcome.REPEAT, gates.MAX_ATTEMPTS_PER_STATE, 0)[0] is State.STOPPED
    assert next_state(State.P2_VALIDATE, Outcome.PIVOT, 1, gates.MAX_PIVOTS_PER_RUN)[0] is State.STOPPED


def test_terminal_states_have_no_transitions():
    for s in TERMINAL:
        with pytest.raises(ValueError):
            next_state(s, Outcome.GO, 1, 0)


def _ev(**kw):
    base = dict(
        problem_source_urls=["https://a.example/1", "https://b.example/2", "https://c.example/3"],
        paying_source_urls=["https://d.example/pricing"],
        budget_usd=50, hours_per_week=10, mvp_cost_usd=20, mvp_hours_per_week=5,
        total_claims=10, assumption_claims=2,
    )
    base.update(kw)
    return Phase1Evidence(**base)


def test_phase1_go_when_all_thresholds_met():
    assert gates.phase1_gate(_ev(), attempt=1).outcome is Outcome.GO


def test_phase1_one_miss_repeats_once_then_stops():
    ev = _ev(paying_source_urls=[])
    assert gates.phase1_gate(ev, attempt=1).outcome is Outcome.REPEAT
    assert gates.phase1_gate(ev, attempt=2).outcome is Outcome.STOP


def test_phase1_two_misses_stop_immediately():
    ev = _ev(paying_source_urls=[], budget_usd=0)
    res = gates.phase1_gate(ev, attempt=1)
    assert res.outcome is Outcome.STOP
    assert any("STOP" in r for r in res.reasons)


def test_phase1_domains_must_differ():
    ev = _ev(problem_source_urls=["https://www.a.example/1", "https://a.example/2", "https://a.example/3"])
    res = gates.phase1_gate(ev, attempt=1)
    assert not res.checks[0].passed


def test_phase1_half_assumptions_is_a_miss():
    res = gates.phase1_gate(_ev(total_claims=10, assumption_claims=5), attempt=1)
    assert res.outcome is Outcome.REPEAT


def test_tests_gate():
    assert gates.tests_gate({"a": True, "b": True}, 1).outcome is Outcome.GO
    assert gates.tests_gate({"a": True, "b": False}, 1).outcome is Outcome.REPEAT
    assert gates.tests_gate({"a": False}, 2).outcome is Outcome.STOP
    assert gates.tests_gate({}, 1).outcome is Outcome.REPEAT

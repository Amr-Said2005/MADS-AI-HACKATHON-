import pytest

import gates
from gates import Outcome, Phase1Evidence
from orchestrator import TERMINAL, TRANSITIONS, State, next_state

NON_TERMINAL = [s for s in State if s not in TERMINAL]


def test_transition_table_covers_every_non_terminal_state():
    assert set(TRANSITIONS) == set(NON_TERMINAL)
    for state, row in TRANSITIONS.items():
        assert row[Outcome.STOP] is State.STOPPED, state
        assert Outcome.GO in row, state


def test_table_matches_plan():
    assert TRANSITIONS[State.SCREEN_BRIEF][Outcome.GO] is State.FEASIBILITY
    assert TRANSITIONS[State.FEASIBILITY][Outcome.GO] is State.ROADMAP
    assert TRANSITIONS[State.ROADMAP][Outcome.GO] is State.P1_DISCOVER
    assert TRANSITIONS[State.P1_DISCOVER][Outcome.PIVOT] is State.SCREEN_BRIEF
    assert TRANSITIONS[State.P2_VALIDATE][Outcome.PIVOT] is State.P1_DISCOVER
    assert TRANSITIONS[State.P5_LAUNCH][Outcome.PIVOT] is State.P4_COMMERCIAL
    assert TRANSITIONS[State.P6_OPERATE][Outcome.GO] is State.DONE
    assert TRANSITIONS[State.P6_OPERATE][Outcome.PIVOT] is State.P1_DISCOVER


def test_undefined_outcome_stops():
    assert next_state(State.SCREEN_BRIEF, Outcome.REPEAT, 0, 0)[0] is State.STOPPED
    assert next_state(State.P6_OPERATE, Outcome.REPEAT, 0, 0)[0] is State.STOPPED
    assert next_state(State.FEASIBILITY, Outcome.PIVOT, 0, 0)[0] is State.STOPPED


def test_repeat_and_pivot_limits():
    assert next_state(State.FEASIBILITY, Outcome.REPEAT, 0, 0)[0] is State.FEASIBILITY
    assert next_state(State.FEASIBILITY, Outcome.REPEAT, gates.FEASIBILITY_MAX_REPEATS, 0)[0] is State.STOPPED
    assert next_state(State.ROADMAP, Outcome.REPEAT, gates.ROADMAP_MAX_REVISIONS - 1, 0)[0] is State.ROADMAP
    assert next_state(State.ROADMAP, Outcome.REPEAT, gates.ROADMAP_MAX_REVISIONS, 0)[0] is State.STOPPED
    assert next_state(State.P2_VALIDATE, Outcome.PIVOT, 0, gates.MAX_PIVOTS_PER_RUN)[0] is State.STOPPED


def test_terminal_states_have_no_transitions():
    for s in TERMINAL:
        with pytest.raises(ValueError):
            next_state(s, Outcome.GO, 0, 0)


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


def _feas(**kw):
    base = dict(customer="freelance translators", problem="tracking word counts", product_type="template pack",
                problem_source_urls=["https://forum.example/a"], founder_skills=["Notion", "writing"],
                needed_skills=["Notion"], budget_usd=50, mvp_cost_usd=20, hours_per_week=10, effort_hours=30,
                founder_values=["honest marketing"], values_conflict="")
    base.update(kw)
    return gates.FeasibilityInput(**base)


def test_feasibility_go():
    gate, rows = gates.feasibility_gate(_feas(), 0)
    assert gate.outcome is Outcome.GO and [r.id for r in rows] == ["I1", "I2", "I3", "F1", "F2", "F3", "F4"]


@pytest.mark.parametrize("kw", [dict(product_type="physical kit"), dict(budget_usd=0), dict(effort_hours=100),
                                dict(values_conflict="honest marketing"), dict(mvp_cost_usd=None)])
def test_feasibility_hard_fail_stops(kw):
    assert gates.feasibility_gate(_feas(**kw), 0)[0].outcome is Outcome.STOP


def test_feasibility_soft_fails():
    one = _feas(customer="everyone")
    assert gates.feasibility_gate(one, 0)[0].outcome is Outcome.REPEAT
    assert gates.feasibility_gate(one, 1)[0].outcome is Outcome.STOP  # one narrowed idea only
    three = _feas(customer="everyone", problem_source_urls=[], needed_skills=["Python", "design"])
    assert gates.feasibility_gate(three, 0)[0].outcome is Outcome.STOP
    short = _feas(effort_hours=60)  # 40 h available: short, but not by more than half
    assert gates.feasibility_gate(short, 0)[1][5].result == "soft_fail"


def test_feasibility_estimates_are_labelled_assumption():
    md = gates.feasibility_markdown(*gates.feasibility_gate(_feas(), 0), idea="x")
    assert md.count("ASSUMPTION") >= 4


def _milestones(**override):
    ms = [{"phase": i, "title": f"T{i}", "deliverables": [], "founder_sees": "artefacts",
           "decision_asked": "continue or stop"} for i in range(1, 7)]
    for k, v in override.items():
        ms[0][k] = v
    return ms


def test_roadmap_validation():
    ms, problems = gates.validate_roadmap(_milestones())
    assert not problems and [m["id"] for m in ms] == [f"M{i}" for i in range(1, 7)]
    assert all(m["gate"] == gates.PHASE_GATES[m["phase"]] for m in ms)  # every gate kept by code
    assert gates.validate_roadmap(_milestones()[:5])[1]
    assert gates.validate_roadmap(list(reversed(_milestones())))[1]
    assert gates.validate_roadmap(_milestones(title="Reach 100 sales"))[1]
    assert gates.validate_roadmap(_milestones(founder_sees="our income"))[1]
    assert gates.validate_roadmap(_milestones(decision_asked=""))[1]


def test_plan_check():
    good = [{"id": "C1", "check": "x", "threshold": ">= 3", "method": "code"}]
    assert gates.check_plan(good) == []
    assert gates.check_plan([])
    assert gates.check_plan([{"id": "C1", "check": "x", "threshold": "", "method": "code"}])
    assert gates.check_plan([{"id": "C1", "check": "x", "threshold": "1", "method": "vibes"}])
    assert gates.check_plan(good + good)  # duplicate id


def test_milestone_choices_follow_the_table():
    assert gates.milestone_choices(Outcome.GO, True, True) == ["continue", "request_changes", "pivot", "stop"]
    assert gates.milestone_choices(Outcome.PIVOT, True, True) == ["pivot", "request_changes", "stop"]
    assert gates.milestone_choices(Outcome.STOP, True, True) == ["stop", "pivot", "request_changes"]
    assert gates.milestone_choices(Outcome.STOP, False, False) == ["stop"]


def test_open_defects_turn_go_into_pivot_or_stop():
    go = gates.GateResult("g", Outcome.GO, ["ok"])
    assert gates.with_open_defects(go, ["C1"], can_pivot=True).outcome is Outcome.PIVOT
    assert gates.with_open_defects(go, ["C1"], can_pivot=False).outcome is Outcome.STOP
    assert gates.with_open_defects(go, [], can_pivot=True).outcome is Outcome.GO

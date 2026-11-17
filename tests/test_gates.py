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
    assert next_state(State.FEASIBILITY, Outcome.REPEAT, 0, 0)[0] is State.SCREEN_BRIEF  # a revise is screened again
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
    base = dict(customer="freelance translators", problem="tracking word counts", idea="A Notion template pack for translators",
                family="Templates and tools", physical_shipped=False,
                problem_source_urls=["https://forum.example/a"], founder_skills=["Notion", "writing"],
                needed_skills=["Notion"], budget_usd=50, mvp_cost_usd=20, hours_per_week=10, effort_hours=30,
                founder_values=["honest marketing"], values_conflict="")
    base.update(kw)
    return gates.FeasibilityInput(**base)


def levels(**kw):
    return {w.id: w.level for w in gates.feasibility_warnings(_feas(**kw))}


def test_feasibility_gives_seven_warnings_and_no_verdict():
    warnings = gates.feasibility_warnings(_feas())
    assert [w.id for w in warnings] == ["W-CUSTOMER", "W-SIGNAL", "W-BUILD", "W-SKILLS", "W-BUDGET", "W-TIME", "W-VALUES"]
    assert {w.level for w in warnings} == {"none"} and gates.feasibility_recommendation(warnings) == "Proceed"
    assert not hasattr(gates, "feasibility_gate")  # nothing here returns go, repeat or stop


@pytest.mark.parametrize("kw,wid,level", [
    (dict(customer="everyone"), "W-CUSTOMER", "minor"),
    (dict(problem_source_urls=[]), "W-SIGNAL", "minor"),
    (dict(idea="A mobile app for translators", family="Software"), "W-BUILD", "major"),
    (dict(idea="A pack of podcast intro music"), "W-BUILD", "major"),
    (dict(idea="Something for translators", family="unsure"), "W-BUILD", "minor"),
    (dict(needed_skills=["Notion", "video editing"]), "W-SKILLS", "major"),  # a skill with no kind counts as core
    (dict(needed_skills=["Notion", "marketing"], skill_kinds={"marketing": "supporting"}), "W-SKILLS", "minor"),
    (dict(budget_usd=10), "W-BUDGET", "major"),
    (dict(budget_usd=22), "W-BUDGET", "minor"),  # covers the $20 estimate with little left
    (dict(effort_hours=100), "W-TIME", "major"),  # 40 hours available: less than half
    (dict(effort_hours=60), "W-TIME", "minor"),  # between half and all
    (dict(values_conflict="honest marketing"), "W-VALUES", "major"),
])
def test_each_warning_has_its_level_a_fact_and_ways_to_address_it(kw, wid, level):
    warning = next(w for w in gates.feasibility_warnings(_feas(**kw)) if w.id == wid)
    assert warning.level == level and warning.fact and warning.evidence and warning.ways
    assert levels(**kw) == dict(levels(), **{wid: level})  # nothing else changed


def test_the_recommendation_follows_the_number_of_major_warnings():
    assert gates.feasibility_recommendation(gates.feasibility_warnings(_feas(customer="everyone"))) == "Proceed"
    one = gates.feasibility_warnings(_feas(budget_usd=10))
    assert gates.feasibility_recommendation(one) == "Proceed with care, or revise"
    two = gates.feasibility_warnings(_feas(budget_usd=10, effort_hours=100))
    assert gates.feasibility_recommendation(two) == "Revise or stop" and gates.warning_counts(two) == (2, 0)
    assert gates.continued_line(2) == "Continued with 2 major feasibility warnings"


def test_feasibility_report_lists_major_warnings_first_and_labels_estimates():
    inp = _feas(budget_usd=10, customer="everyone")
    md = gates.feasibility_markdown(gates.feasibility_warnings(inp), "x", inp)
    assert md.startswith("Decision: PENDING. Reasons: 1 major and 1 minor warning; the founder decides.")
    assert md.index("| W-BUDGET | major |") < md.index("| W-CUSTOMER | minor |") < md.index("| W-SIGNAL | none |")
    assert md.count("ASSUMPTION") >= 4 and "Proceed with care, or revise." in md
    assert "### W-BUDGET (major)" in md and "hard_fail" not in md and "soft_fail" not in md


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


def test_gate_criteria_in_plans_use_the_gates_own_check_names():
    produced = {
        1: gates.phase1_gate(_ev(), 1).checks,
        2: gates.phase2_gate(1, 1, True, "not_measured", "x", 1).checks,
        3: gates.tests_gate({name: True for name in gates.PRODUCT_ACCEPTANCE}, 1).checks,
        4: gates.phase4_gate(9, "name", "not legal advice", 1).checks,
        5: gates.phase5_gate("page", ["post"], 1).checks,
        6: gates.phase6_gate([{"name": "sign-up rate", "status": "met", "detail": "x"}]).checks,
    }
    for phase, checks in produced.items():
        assert [c.name for c in checks] == [name for name, _ in gates.GATE_CRITERIA[phase]], phase
    plan = gates.gate_criteria(1)
    assert [c["id"] for c in plan] == ["G1", "G2", "G3", "G4"] and gates.check_plan(plan) == []
    assert "3 fetched SAY sources from 3 different domains" in plan[0]["threshold"]


def test_phase6_gate_needs_measured_metrics_to_persevere():
    met = {"name": "sign-up rate", "status": "met", "detail": "12 of 200"}
    missed = {"name": "sign-up rate", "status": "missed", "detail": "3 of 200"}
    unmeasured = {"name": "sign-up rate", "status": "not_measured", "detail": "0 of 200"}
    assert gates.phase6_gate([met]).outcome is Outcome.GO
    assert gates.phase6_gate([met, missed]).outcome is Outcome.PIVOT
    stop = gates.phase6_gate([unmeasured])
    assert stop.outcome is Outcome.STOP and any("not a verdict on the idea" in r for r in stop.reasons)
    assert gates.phase6_gate([met, unmeasured]).outcome is Outcome.STOP  # one unmeasured metric is enough
    assert gates.phase6_gate([]).outcome is Outcome.STOP and gates.phase6_gate("persevere").outcome is Outcome.STOP


# ---------------------------------------------------------------- what counts as a digital product

@pytest.mark.parametrize("idea,family", [
    ("Digital notes for first-year chemistry students", "Written content"),
    ("An online course on public speaking for engineers", "Courses and learning"),
    ("An e-book about moving to a new city", "Written content"),
    ("A Notion template for tracking job applications", "Templates and tools"),
    ("An education platform that sells university notes", "Software"),
    ("A paid community for first-time managers", "Access"),
    ("A pack of podcast intro music", "Media and design"),
])
def test_every_kind_of_digital_product_is_accepted(idea, family):
    # the model answers "unsure", or even the wrong family: a word in the idea wins over it
    for answer in ("unsure", "Access", ""):
        assert gates.product_family(idea, answer)[0] == family
    build = next(w for w in gates.feasibility_warnings(_feas(idea=idea, family="unsure")) if w.id == "W-BUILD")
    assert build.level in ("none", "major") and f"family: {family}" in build.fact  # never "unsure", never a stop


def test_the_model_decides_the_family_only_when_the_idea_names_none():
    assert gates.product_family("Something for dog owners", "Courses and learning") == ("Courses and learning", "")
    assert gates.product_family("Something for dog owners", "nonsense") == ("unsure", "")


def test_a_format_nobody_can_place_is_a_minor_warning_and_never_a_stop():
    warnings = gates.feasibility_warnings(_feas(idea="Something for dog owners", family="unsure"))
    build = next(w for w in warnings if w.id == "W-BUILD")
    assert build.level == "minor" and "Confirm" in build.ways[0]
    assert gates.feasibility_recommendation(warnings) == "Proceed"  # a minor warning changes nothing


# ---------------------------------------------------------------- skills: no number, three ways to cover each missing skill

def test_skills_carry_no_number_and_each_missing_skill_lists_people_ai_tools_and_learning():
    import re

    inp = _feas(needed_skills=["Notion", "video editing", "marketing"], skill_kinds={"marketing": "supporting"})
    skills = next(w for w in gates.feasibility_warnings(inp) if w.id == "W-SKILLS")
    assert skills.level == "major"
    assert skills.fact == ("needed: Notion, video editing, marketing; held: Notion; "
                           "missing to make the product: video editing; missing supporting skills: marketing")
    assert not re.search(r"\d+ ?%|\d+ of \d+|score", skills.fact + skills.looks_at)  # no percentage, count or score
    for skill, role in (("video editing", "needed to make the product"), ("marketing", "supporting")):
        mine = [w for w in skills.ways if w.startswith(f"{skill} ({role}), ")]
        assert [w.split(", ", 1)[1].split(":")[0] for w in mine] == ["people", "AI tools", "learning"]
    for way in skills.ways:
        assert "ASSUMPTION" in way  # every cost or duration is labelled
        assert not re.search(r"guarantee|will work|certain|recommend", way, re.I)
    md = gates.feasibility_markdown(gates.feasibility_warnings(inp), "x", inp)
    assert "- video editing (needed to make the product), people: contract or hire someone" in md
    assert not re.search(r"\d+ ?%", md.split("W-BUDGET")[0])  # nothing about skills is a percentage


def test_the_ways_say_whether_budget_and_time_leave_room():
    tight = _feas(needed_skills=["video editing"], budget_usd=20, effort_hours=40)  # $20 estimate, 40 hours available
    ways = next(w for w in gates.feasibility_warnings(tight) if w.id == "W-SKILLS").ways
    assert "leaves no room for paid help unless it is raised" in ways[0]
    assert "leaves no room for it unless it is extended or work is delegated" in ways[2]
    roomy = _feas(needed_skills=["video editing"], budget_usd=200, effort_hours=10)
    ways = next(w for w in gates.feasibility_warnings(roomy) if w.id == "W-SKILLS").ways
    assert "$180 of the $200 budget is left (ASSUMPTION)" in ways[0] and "30 of the 40 hours in the time frame are left (ASSUMPTION)" in ways[2]


# ---------------------------------------------------------------- time: the founder's time frame and delegated hours

def test_available_hours_follow_the_time_frame_and_the_delegated_hours():
    assert gates.available_hours(_feas())[0] == 40  # 10 hours a week, no time frame given: 4 weeks
    assert "no time frame was given, so 4 weeks are used (ASSUMPTION)" in gates.available_hours(_feas())[1]
    hours, frame = gates.available_hours(_feas(timeframe_weeks=8))
    assert hours == 80 and "over 8 weeks" in frame and "ASSUMPTION" not in frame
    hours, frame = gates.available_hours(_feas(timeframe_weeks=8, delegated_hours_per_week=5))
    assert hours == 120 and "plus 5 delegated hours" in frame
    assert levels(effort_hours=100)["W-TIME"] == "major"
    assert levels(effort_hours=100, timeframe_weeks=8)["W-TIME"] == "minor"
    assert levels(effort_hours=100, timeframe_weeks=8, delegated_hours_per_week=5)["W-TIME"] == "none"
    time = next(w for w in gates.feasibility_warnings(_feas(effort_hours=100)) if w.id == "W-TIME")
    assert "gap 60 hours" in time.fact and "one person working alone" in time.fact
    assert time.ways == ["Delegate part of the work.", "Extend the time frame.", "Use AI tools for part of the work.",
                         "Narrow the scope of the first version."]

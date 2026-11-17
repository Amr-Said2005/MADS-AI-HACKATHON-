"""The two-level machine (plan section 8, checks 1-6). No model needed."""

import pytest

import gates
import orchestrator as orch
from gates import Outcome
from orchestrator import BACK_EDGES, INNER, TRANSITIONS, Exit, Signal, Stage, State, inner_next, next_state
from test_pipeline import drive, first_choice, install_fakes, registry, store, to_roadmap

NON_TERMINAL = [s for s in State if s not in orch.TERMINAL]
NO_BUDGET = {k: 0 for k in orch.BUDGET_KEYS}


# 1. Every (state, outcome) pair in 4.1 and 4.2 has a defined next state.

@pytest.mark.parametrize("state", NON_TERMINAL)
@pytest.mark.parametrize("outcome", list(Outcome))
def test_every_outer_pair_has_a_next_state(state, outcome):
    nxt, _ = next_state(state, outcome, repeats_used=0, pivots=0)
    assert isinstance(nxt, State)


@pytest.mark.parametrize("stage", list(Stage))
@pytest.mark.parametrize("signal", list(Signal))
def test_every_inner_pair_has_a_next_state(stage, signal):
    target, eff, _, _ = inner_next(stage, signal, dict(NO_BUDGET))
    assert isinstance(target, (Stage, Exit)) and isinstance(eff, Signal)
    if signal not in INNER[stage]:
        assert target is Exit.STOPPED


# 2. STOPPED is reachable from SCREEN_BRIEF, FEASIBILITY, ROADMAP, PLAN_REVIEW and MILESTONE_REVIEW
#    (after feasibility, only on the founder's choice: see tests/test_no_auto_stop.py).

def test_stop_exits_exist():
    for s in (State.SCREEN_BRIEF, State.FEASIBILITY, State.ROADMAP):
        assert TRANSITIONS[s][Outcome.STOP] is State.STOPPED
    assert INNER[Stage.PLAN_REVIEW][Signal.STOP] is Exit.STOPPED
    assert INNER[Stage.MILESTONE_REVIEW][Signal.STOP] is Exit.STOPPED


# 3. No transition leads from SIGN_OFF to VERIFY.

def test_sign_off_never_returns_to_verify():
    for signal in Signal:
        for used in range(gates.MAX_BACK_EDGES + 2):
            target, *_ = inner_next(Stage.SIGN_OFF, signal, {k: used for k in orch.BUDGET_KEYS})
            assert target is not Stage.VERIFY
    assert Stage.VERIFY not in INNER[Stage.SIGN_OFF].values()


# 4. A third revise, defects or repeat in one phase does not loop again, and does not end the run either.

@pytest.mark.parametrize("edge", list(BACK_EDGES))
def test_third_back_edge_does_not_loop(edge):
    stage, signal = edge
    counter = BACK_EDGES[edge]
    budget = dict(NO_BUDGET)
    loops = 0
    for _ in range(5):
        target, eff, spent, _ = inner_next(stage, signal, budget)
        if spent is None:
            break
        budget[spent] += 1
        loops += 1
    assert loops == gates.MAX_BACK_EDGES
    assert target is not INNER[stage][signal]
    assert target is not Exit.STOPPED  # a used-up budget never ends the run by itself
    if counter == "defects":
        assert target is Stage.SIGN_OFF  # goes on with its open defects listed
    elif counter == "revise":
        assert target is Stage.MAKE and eff is Signal.APPROVED  # goes on with the gate's own criteria only
    elif stage is Stage.SIGN_OFF:
        assert target is Stage.MILESTONE_REVIEW and eff is Signal.STOP  # a recommendation; the founder decides
    else:
        assert target is Stage.MILESTONE_REVIEW  # the founder is asked again


# 5. No phase starts before the roadmap has a founder approval record, and no phase
#    advances without a founder decision at MILESTONE_REVIEW.

def test_no_phase_starts_without_roadmap_approval(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = to_roadmap(rid, registry())
    assert rec.state == "ROADMAP" and rec.status == "awaiting_approval"
    # Skipping the approval by editing the saved state does not get a phase started.
    rec.state, rec.pending, rec.status = State.P1_DISCOVER.value, None, "running"
    orch.save(rec)
    rec = orch.advance(rid, registry=registry())
    assert rec.state == "STOPPED" and "no founder approval of the roadmap" in rec.stop_reasons[0]
    assert "P1_DISCOVER" not in rec.passed


def test_no_phase_advances_without_a_founder_decision(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")  # roadmap
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "P1_DISCOVER" and rec.stage == "MILESTONE_REVIEW" and rec.status == "awaiting_approval"
    for _ in range(3):  # resuming without a decision changes nothing
        rec = orch.advance(rid, registry=reg)
        assert rec.state == "P1_DISCOVER" and rec.stage == "MILESTONE_REVIEW" and rec.passed == []


# 6. When the gate's recommendation is pivot or stop, the next phase is unreachable whatever the founder chooses.

@pytest.mark.parametrize("rec", [Outcome.PIVOT, Outcome.STOP])
def test_continue_is_never_offered_after_a_failed_gate(rec):
    for repeat_left in (True, False):
        for can_pivot in (True, False):
            assert gates.CONTINUE not in gates.milestone_choices(rec, repeat_left, can_pivot)


def test_founder_cannot_continue_after_stop(monkeypatch, strong_brief):
    install_fakes(monkeypatch, problem=1, paying=0)  # Phase 1 gate recommends stop
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["recommendation"] == "stop"
    with pytest.raises(ValueError, match="not offered"):
        store(rid).choose(rec.pending["action_id"], "continue")
    for choice in rec.pending["choices"]:
        assert choice != "continue"
    rec, _ = drive(rid, reg, first_choice)
    assert "P2_VALIDATE" not in {h["state"] for h in rec.history}


# ---------------------------------------------------------------- source criteria are decided against the registry

def test_a_criterion_about_a_cited_source_never_passes_without_a_registered_url(monkeypatch, strong_brief):
    from orchestrator import RunContext, _about_sources, _normalise_rows
    import settings

    install_fakes(monkeypatch)
    rec = orch.new_run(strong_brief)
    ctx = RunContext(rec, settings.load(), registry=registry())
    criteria = [{"id": "C1", "check": "every opportunity cites a fetched URL", "threshold": "at least 1", "method": "code"},
                {"id": "C2", "check": "the guide has five questions", "threshold": "5", "method": "code"},
                {"id": "C3", "check": "problem evidence", "threshold": "3", "method": "source"}]
    assert [_about_sources(c) for c in criteria] == [True, False, True]
    (ctx.outputs_dir).mkdir(parents=True, exist_ok=True)
    (ctx.outputs_dir / "phase1_step1_opportunities.md").write_text("x", encoding="utf-8")
    given = [{"id": "C1", "result": "pass", "evidence": "phase1_step1_opportunities.md"},
             {"id": "C2", "result": "pass", "evidence": "phase1_step1_opportunities.md"},
             {"id": "C3", "result": "pass", "evidence": "https://never-fetched.example/page"}]
    rows = {r["id"]: r for r in _normalise_rows(ctx, given, criteria)}
    assert rows["C1"]["result"] == "unverified" and "no fetched source" in rows["C1"]["evidence"]  # a file name is not evidence
    assert rows["C2"]["result"] == "pass"  # not about sources: an output file is fine
    assert rows["C3"]["result"] == "unverified"  # empty registry: nothing can pass
    ctx.registry.fetch("https://forum.example/translators")
    given[0]["evidence"] = "https://forum.example/translators"
    assert {r["id"]: r for r in _normalise_rows(ctx, given, criteria)}["C1"] == {
        "id": "C1", "result": "pass", "evidence": "https://forum.example/translators"}


# ---------------------------------------------------------------- founder waiver at every phase gate

def test_waiver_is_offered_only_after_a_failed_gate_and_at_every_phase():
    for recommendation in (Outcome.PIVOT, Outcome.STOP):
        assert gates.milestone_choices(recommendation, True, True, waivable=True)[-1] == "waiver"
        assert "waiver" not in gates.milestone_choices(recommendation, True, True)  # only where the caller allows it
        assert "continue" not in gates.milestone_choices(recommendation, True, True, waivable=True)
    assert "waiver" not in gates.milestone_choices(Outcome.GO, True, True, waivable=True)
    assert gates.WAIVER_PHASES == (1, 2, 3, 4, 5, 6)


def test_the_next_phase_is_reachable_after_a_stop_only_with_a_waiver_and_a_reason(monkeypatch, strong_brief):
    from decision_log import DecisionLog
    from test_pipeline import outputs

    install_fakes(monkeypatch, problem=1, paying=0)  # the Phase 1 gate recommends stop
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["recommendation"] == "stop" and rec.pending["choices"][-1] == "waiver"
    before = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    for empty in ("", "   "):
        with pytest.raises(ValueError, match="written reason"):
            store(rid).choose(rec.pending["action_id"], "waiver", note=empty)
    assert store(rid).choice(rec.pending["action_id"]) is None and orch.advance(rid, registry=reg).state == "P1_DISCOVER"
    store(rid).choose(rec.pending["action_id"], "waiver", note="I interviewed twelve translators myself last month")
    rec = orch.advance(rid, registry=reg)
    assert "P2_VALIDATE" in {h["next"] for h in rec.history} and rec.history[-1]["state"] != "P1_DISCOVER" or rec.state == "P2_VALIDATE"
    line = "Continued against a STOP recommendation at M1: problem sources, someone already pays, claims mostly sourced"
    decision = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert decision.startswith(before.rstrip("\n").split("\n")[0])  # the header is as it was
    assert before.split("\n")[2].split(" Founder chose")[0] in decision  # the STOP decision and its reasons are unchanged
    assert "| problem sources | MISS |" in decision and "Founder chose: **waiver**" in decision
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    waiver = next(e for e in entries if e["action"] == "waiver_m1")
    assert waiver["agent"] == "founder" and waiver["approved_by"] == "founder" and waiver["outcome"] == "waiver"
    assert waiver["reason"].startswith("I interviewed twelve") and waiver["category"] == "problem sources; someone already pays; claims mostly sourced"
    later = (outputs(rid) / "phase2_gate_plan_review.md").read_text(encoding="utf-8")
    assert later.split("\n")[0].endswith(f"| {line}") and orch.has_header(later)  # every later file header carries it
    assert orch.strip_header(later).startswith("Decision: ")
    from fastapi.testclient import TestClient
    from app.main import app
    assert line in TestClient(app).get(f"/runs/{rid}").text  # and the run view


def test_the_waiver_is_offered_at_the_phase_3_test_gate_and_never_before_phase_1():
    assert all(n in gates.WAIVER_PHASES for n in (3, 4, 5))
    assert gates.milestone_choices(Outcome.STOP, False, False, waivable=3 in gates.WAIVER_PHASES) == ["stop", "waiver"]
    # The scope screen, the feasibility check and the roadmap are not phases: no waiver exists there.
    assert 0 not in gates.WAIVER_PHASES and set(gates.WAIVER_PHASES) == set(orch.PHASE_OF.values())

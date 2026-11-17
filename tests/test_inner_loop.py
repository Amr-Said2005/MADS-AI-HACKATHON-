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


# 2. STOPPED is reachable from SCREEN_BRIEF, FEASIBILITY, ROADMAP, PLAN_REVIEW and MILESTONE_REVIEW.

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


# 4. A third revise, defects or repeat in one phase does not loop again.

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
    if counter == "defects":
        assert target is Stage.SIGN_OFF  # goes on with its open defects listed
    else:
        assert eff is Signal.STOP


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

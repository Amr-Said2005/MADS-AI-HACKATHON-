"""After the founder continues past the feasibility check, only the founder ends a run (or the scope screen,
for a revised idea, note or pivot). Where the run used to stop by itself it pauses and asks."""

import guardrails
import orchestrator as orch
from decision_log import DecisionLog
from orchestrator import RunStatus, StepResult
from test_pipeline import BAD_CRITERIA, MILESTONES, drive, first_choice, install_fakes, outputs, registry, store, to_roadmap

FOUNDER_STOP_STARTS = ("founder chose stop", "founder stopped", "the founder stopped", "founder pressed stop")


def entries(rid):
    return DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()


def stops_without_a_founder_decision(rid):
    """History entries after feasibility that lead to STOPPED with no founder decision record before them."""
    rec = orch.load(rid)
    start = next((i for i, h in enumerate(rec.history) if h["state"] == "FEASIBILITY" and h["next"] == "ROADMAP"), None)
    if start is None:
        return []
    founder = [e for e in entries(rid) if e["agent"] == "founder" and e["approved_by"] == "founder"]
    bad = []
    for h in rec.history[start + 1:]:
        if h["next"] != "STOPPED" or h["reasons"] == [guardrails.SCOPE_REPLY]:
            continue
        before = [e for e in founder if e["timestamp"] <= h["at"]]
        if not (before and before[-1]["outcome"] == "stop" and h["reasons"][0].startswith(FOUNDER_STOP_STARTS)):
            bad.append(h)
    return bad


def approved_run(monkeypatch, brief, **fakes):
    install_fakes(monkeypatch, **fakes)
    rid = orch.new_run(brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    return rid, reg


def spend(rid, **usage):
    rec = orch.load(rid)
    rec.usage = dict(rec.usage or {}, **usage)
    orch.save(rec)


def test_a_plan_that_fails_review_twice_goes_on_with_the_gate_criteria(monkeypatch, strong_brief):
    rid, reg = approved_run(monkeypatch, strong_brief, plans={0: BAD_CRITERIA})
    rec = orch.advance(rid, registry=reg)
    assert (rec.state, rec.stage, rec.status) == ("P1_DISCOVER", "MILESTONE_REVIEW", RunStatus.AWAITING_APPROVAL.value)
    assert rec.pending["milestone"] == "M1" and rec.pending["recommendation"] == "go"
    assert [c["id"][0] for c in rec.phase_data["criteria"]] == ["G"] * len(rec.phase_data["criteria"])
    assert any(e["action"] == "phase1_plan_review" and e["outcome"] == "gate_criteria_only" for e in entries(rid))
    assert stops_without_a_founder_decision(rid) == [] and rec.status != RunStatus.STOPPED.value


def test_the_model_call_budget_pauses_and_extend_grants_it_again(monkeypatch, strong_brief):
    rid, reg = approved_run(monkeypatch, strong_brief)
    spend(rid, calls=1)
    monkeypatch.setenv("MAX_MODEL_CALLS", "1")
    rec = orch.advance(rid, registry=reg)
    assert rec.status == RunStatus.AWAITING_APPROVAL.value and rec.state == "P1_DISCOVER"
    assert rec.pending["gate"] == "pause" and rec.pending["pause"] == "run_budget"
    assert rec.pending["choices"] == ["extend", "stop"]
    store(rid).choose(rec.pending["action_id"], "extend")
    rec = orch.advance(rid, registry=reg)
    assert rec.artifacts["budget"] == {"extensions": 1}
    assert (rec.state, rec.stage) == ("P1_DISCOVER", "MILESTONE_REVIEW") and rec.pending["milestone"] == "M1"
    assert (outputs(rid) / "phase1_step1_opportunities.md").exists()
    assert any(e["agent"] == "founder" and e["action"] == "pause_run_budget" and e["outcome"] == "extend" for e in entries(rid))
    rec, _ = drive(rid, reg, first_choice)
    assert rec.status == RunStatus.DONE.value


def test_the_model_call_budget_pause_ends_only_when_the_founder_chooses_stop(monkeypatch, strong_brief):
    rid, reg = approved_run(monkeypatch, strong_brief)
    spend(rid, calls=1)
    monkeypatch.setenv("MAX_MODEL_CALLS", "1")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["choices"] == ["extend", "stop"]
    store(rid).choose(rec.pending["action_id"], "stop")
    rec = orch.advance(rid, registry=reg)
    assert rec.status == RunStatus.STOPPED.value and rec.state == "STOPPED"
    assert rec.stop_reasons[0].startswith("the founder stopped the run") and "MAX_MODEL_CALLS=1" in rec.stop_reasons[0]
    stop = next(e for e in entries(rid) if e["action"] == "pause_run_budget" and e["agent"] == "founder")
    assert stop["approved_by"] == "founder" and stop["outcome"] == "stop"
    assert stops_without_a_founder_decision(rid) == []


def test_the_time_budget_pauses_after_feasibility_and_keeps_the_open_question(monkeypatch, strong_brief):
    rid, reg = approved_run(monkeypatch, strong_brief)
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1"
    milestone_id = rec.pending["action_id"]
    spend(rid, seconds=1.5)
    monkeypatch.setenv("MAX_RUN_SECONDS", "1")
    rec = orch.advance(rid, registry=reg)  # an open, unanswered question is left as it is
    assert rec.pending["action_id"] == milestone_id
    store(rid).choose(milestone_id, "continue")
    for aid in rec.pending["outbound"].values():
        store(rid).decide(aid, approve=True)
    rec = orch.advance(rid, registry=reg)  # the answer is taken first; no new work starts past the budget
    assert rec.state == "P2_VALIDATE" and rec.stage is None and rec.status == RunStatus.AWAITING_APPROVAL.value
    assert rec.pending["pause"] == "run_budget" and "MAX_RUN_SECONDS=1" in rec.pending["summary"]
    store(rid).choose(rec.pending["action_id"], "extend")
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "P2_VALIDATE" and rec.stage == "MILESTONE_REVIEW" and rec.artifacts["budget"]["extensions"] == 1


def test_the_time_budget_before_feasibility_still_stops_the_run(monkeypatch, strong_brief):
    monkeypatch.setenv("MAX_RUN_SECONDS", "1")
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    spend(rid, seconds=5.0)
    rec = orch.advance(rid, registry=registry())
    assert rec.status == RunStatus.STOPPED.value and rec.pending is None


def test_a_roadmap_that_fails_twice_asks_and_retry_writes_it_again(monkeypatch, strong_brief):
    from agents import planner

    install_fakes(monkeypatch)
    monkeypatch.setattr(planner, "roadmap", lambda ctx: StepResult("roadmap", data={"milestones": MILESTONES[:2]}))
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    assert rec.state == "ROADMAP" and rec.status == RunStatus.AWAITING_APPROVAL.value
    assert rec.pending["gate"] == "pause" and rec.pending["pause"] == "roadmap" and rec.pending["choices"] == ["retry", "stop"]
    monkeypatch.setattr(planner, "roadmap", lambda ctx: StepResult("roadmap", data={"milestones": MILESTONES}))
    store(rid).choose(rec.pending["action_id"], "retry")
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "ROADMAP" and rec.pending["gate"] == "roadmap" and rec.pending["choices"][0] == "approve"
    assert stops_without_a_founder_decision(rid) == []


def test_after_feasibility_every_stop_follows_a_founder_decision(monkeypatch, strong_brief):
    install_fakes(monkeypatch, problem=1, paying=0)  # weak evidence: the Phase 1 gate recommends stop
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, registry(), first_choice)
    assert rec.status == RunStatus.STOPPED.value and seen[-1][1:3] == ("M1", "stop") and seen[-1][4] == "stop"
    assert [h["next"] for h in rec.history].count("STOPPED") == 1 and rec.history[-1]["next"] == "STOPPED"
    assert stops_without_a_founder_decision(rid) == []
    # The helper does find a stop that no founder decision stands behind.
    rec.history[-1]["reasons"] = ["repeat budget used up; stopping"]
    orch.save(rec)
    assert len(stops_without_a_founder_decision(rid)) == 1


def test_the_pause_page_shows_the_question_and_plain_buttons(monkeypatch, strong_brief):
    from fastapi.testclient import TestClient
    from app.main import app

    rid, reg = approved_run(monkeypatch, strong_brief)
    spend(rid, calls=1)
    monkeypatch.setenv("MAX_MODEL_CALLS", "1")
    rec = orch.advance(rid, registry=reg)
    page = TestClient(app).get(f"/runs/{rid}").text
    assert "MAX_MODEL_CALLS=1" in page and "Allow more time and model calls" in page and "Stop the run" in page
    assert rec.pending["action_id"] in page

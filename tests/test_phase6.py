"""Phase 6 (operator) with the real agents, offline, and the end of a full run."""

import demand
import orchestrator as orch
from agents import operator
from decision_log import DecisionLog
from orchestrator import RunStatus
from test_agents import web  # noqa: F401  (a fixture)
from test_phase345 import ALL, drive
from test_pipeline import outputs


def test_a_full_run_ends_with_an_honest_stop_because_nothing_was_measured(web, strong_brief):
    rec, seen = drive(strong_brief, ALL)
    assert seen[-1] == ("M6", "stop", "stop")  # the gate does not recommend persevering on unmeasured metrics
    assert rec.status == RunStatus.STOPPED.value and rec.passed == ["P1_DISCOVER", "P2_VALIDATE", "P3_BUILD", "P4_COMMERCIAL", "P5_LAUNCH"]
    out = outputs(rec.run_id)
    faq = (out / "phase6_step1_support_faq.md").read_text(encoding="utf-8")
    assert "Question 1 about the board?" in faq and "never moves money" in faq and "- Refund requests" in faq
    memo = (out / "phase6_step2_pivot_memo.md").read_text(encoding="utf-8")
    assert "MOCKED metrics" in memo and "| landing page sign-up rate |" in memo and "| not measured |" in memo
    assert "**STOP**" in memo and "not a verdict on the idea" in memo
    assert "translators buy templates at this price **ASSUMPTION**" in memo
    assert "the absence of sign-ups says nothing about interest" in memo  # the facts section is written by code
    decision = (out / "phase6_gate_decision.md").read_text(encoding="utf-8")
    assert "Outcome: STOP" in decision and "| G1 | metrics measured and on target |" in decision
    assert "Founder chose: **stop**" in decision
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert any(e["action"] == "pivot_memo" and e["model"] == "MOCKED-llm" for e in entries)


def test_continue_is_not_offered_at_the_last_milestone_without_measured_metrics(web, strong_brief):
    rec, _ = drive(strong_brief, ALL, choose=lambda rec: None if rec.pending.get("milestone") == "M6" else rec.pending["choices"][0])
    assert rec.pending["recommendation"] == "stop" and "continue" not in rec.pending["choices"]
    assert rec.state == "P6_OPERATE" and rec.state != "DONE"


def test_measured_and_met_metrics_let_the_run_finish(web, strong_brief, monkeypatch):
    monkeypatch.setattr(demand, "read_counter", lambda run_id: demand.SignupCount(visitors=240, signups=18, source="test counter"))
    rec, seen = drive(strong_brief, ALL)
    assert seen[-1] == ("M6", "go", "continue") and rec.status == RunStatus.DONE.value
    memo = (outputs(rec.run_id) / "phase6_step2_pivot_memo.md").read_text(encoding="utf-8")
    assert "**PERSEVERE**" in memo and "18 sign-ups from 240 cold visitors" in memo


def test_a_measured_miss_counts_and_stops_the_run_at_phase_2(web, strong_brief, monkeypatch):
    assert demand.read_counter("x").visitors == 0  # the shipped counter is the mock that reads zero
    monkeypatch.setattr(demand, "read_counter", lambda run_id: demand.SignupCount(visitors=300, signups=3, source="test counter"))
    rec, seen = drive(strong_brief, ALL)
    assert seen[-1] == ("M2", "stop", "stop") and rec.status == RunStatus.STOPPED.value
    decision = (outputs(rec.run_id) / "phase2_gate_decision.md").read_text(encoding="utf-8")
    assert "| demand target | MISS | 3 sign-ups from 300 cold visitors" in decision


def test_metrics_come_only_from_the_mocked_counter(strong_brief):
    class Ctx:
        run_id = "any"

    m = operator.read_metrics(Ctx())
    assert operator.MOCKED is True and len(m) == 1
    assert m[0]["status"] == "not_measured" and "MOCKED" in m[0]["source"] and m[0]["result"] == "0 sign-ups from 0 cold visitors"

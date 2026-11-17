"""Phase 2 (strategist) with the real agents, offline: scripted model, faked search, MockTransport pages."""

import json

import pytest

import demand
import gates
import orchestrator as orch
from agents import strategist
from decision_log import DecisionLog
from gates import Outcome
from orchestrator import RunStatus
from test_agents import run, scripted, web  # noqa: F401  (web is a fixture)
from test_pipeline import outputs, store

CANVAS = {
    "problem": [{"text": "deadlines from several agencies are hard to keep in one place", "evidence": 1}],
    "existing_alternatives": [{"text": "spreadsheets and paper notes", "evidence": 3},
                              {"text": "a paid tracker template", "evidence": 4}],
    "customer_segments": [{"text": "freelance translators working with agencies", "evidence": 2}],
    "unique_value_proposition": [{"text": "one page that shows every client deadline and word count", "evidence": None}],
    "solution": [{"text": "a client table with deadline and word count columns", "evidence": None}],
    "channels": [{"text": "translator forums", "evidence": None}],
    "revenue_streams": [{"text": "a one-time price", "evidence": None}],
    "cost_structure": [{"text": "the founder's hours; no cash cost", "evidence": 99}],
    "key_metrics": [{"text": "sign-ups on the landing page", "evidence": None}],
    "unfair_advantage": [{"text": "none yet", "evidence": None}],
}
LANDING = {"product_name": "Translator Deadline Board", "headline": "A Notion board for translators who juggle agencies",
           "subheadline": "Keep every client deadline and word count on one page.",
           "who_for": "Freelance translators who work with several agencies.",
           "helps_with": ["See every deadline in one list", "Log word counts per client", "Plan the week's workload"],
           "includes": ["A client table", "A deadline calendar view"], "cta_label": "Tell me when it is ready"}
PHASE2 = {"Canvas": CANVAS, "Landing": LANDING}


def stop_at(milestone):
    return lambda rec: None if rec.pending.get("milestone") == milestone else rec.pending["choices"][0]


def test_phase2_slice_reaches_the_milestone_and_says_demand_is_not_validated(web, strong_brief):
    calls = []
    rec, seen = run(strong_brief, scripted(PHASE2, calls=calls), stop_at("M2"))
    assert seen[-1] == ("M2", "go"), rec.stop_reasons or rec.blocked_reason
    out = outputs(rec.run_id)
    canvas = (out / "phase2_step1_lean_canvas.md").read_text(encoding="utf-8")
    assert "deadlines from several agencies are hard to keep in one place. Source: https://forum0.example/page" in canvas
    assert "a paid tracker template. Source: https://shop0.example/page" in canvas  # market evidence from Phase 1
    assert "translator forums. ASSUMPTION" in canvas
    assert "no cash cost. ASSUMPTION" in canvas  # evidence line 99 does not exist
    page = (out / "phase2_step2_landing_page.html").read_text(encoding="utf-8")
    assert "<h1>A Notion board for translators who juggle agencies</h1>" in page and 'type="email"' in page
    assert "not published" in page and "not connected" in page
    for banned in ("testimonial", "countdown", "guarantee", "★"):
        assert banned not in page.lower()
    test = (out / "phase2_step3_demand_test.md").read_text(encoding="utf-8")
    assert "MOCKED" in test and "| 0 | 0 | not measured |" in test and "Demand is not validated" in test
    assert "At least 5% of at least 200 cold visitors" in test
    decision = (out / "phase2_gate_decision.md").read_text(encoding="utf-8")
    assert "Decision: GO." in decision and "demand is NOT validated" in decision
    # Phase 2 makes four model calls: plan, canvas, landing copy, verify. The demand test makes none.
    assert [c[1] for c in calls][8:] == ["Plan", "Canvas", "Landing", "Verification"]
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    mocked = [e for e in entries if e["action"] == "mocked_demand_test"]
    assert mocked and mocked[0]["model"] is None
    assert set(rec.pending["outbound"]) == {"publish_landing_page"}  # publishing is its own decision


def test_publishing_the_landing_page_needs_its_own_approval(web, strong_brief):
    rec, _ = run(strong_brief, scripted(PHASE2), stop_at("M2"))
    rid = rec.run_id
    st = store(rid)
    st.choose(rec.pending["action_id"], "continue")
    rec = orch.advance(rid)
    assert rec.state == "P2_VALIDATE" and rec.status == RunStatus.AWAITING_APPROVAL.value  # waits for the publish decision
    assert not (outputs(rid) / "phase2_step4_publish_log.md").exists()
    st.decide(rec.pending["outbound"]["publish_landing_page"], approve=False)
    rec = orch.advance(rid)
    assert "P2_VALIDATE" in rec.passed and not (outputs(rid) / "phase2_step4_publish_log.md").exists()


def test_canvas_without_sourced_problem_repeats_then_recommends_stop(web, strong_brief):
    weak = json.loads(json.dumps(CANVAS))
    weak["problem"] = [{"text": "translators probably lose track of deadlines", "evidence": None}]
    rec, seen = run(strong_brief, scripted({"Canvas": weak, "Landing": LANDING}), stop_at("M2"))
    assert seen[-1] == ("M2", "stop")
    assert rec.budget["P2_VALIDATE"]["repeat"] == 1
    decision = (outputs(rec.run_id) / "phase2_gate_decision.md").read_text(encoding="utf-8")
    assert "canvas problem box cites Phase 1 evidence | MISS" in decision


def test_pushy_landing_copy_is_blocked_and_never_written(web, strong_brief):
    pushy = dict(LANDING, subheadline="Hurry, this limited-time offer ends tonight.")
    rec, seen = run(strong_brief, scripted({"Canvas": CANVAS, "Landing": pushy}), stop_at("M2"))
    assert not (outputs(rec.run_id) / "phase2_step2_landing_page.html").exists()
    assert seen[-1][0] == "M2" and seen[-1][1] in ("pivot", "stop")


def test_landing_copy_is_escaped(web, strong_brief):
    odd = dict(LANDING, headline="Boards <script>alert(1)</script> for translators")
    rec, _ = run(strong_brief, scripted({"Canvas": CANVAS, "Landing": odd}), stop_at("M2"))
    page = (outputs(rec.run_id) / "phase2_step2_landing_page.html").read_text(encoding="utf-8")
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_signup_counter_is_mocked_and_reads_zero():
    assert demand.MOCKED is True and strategist.MOCKED is True
    count = demand.read_counter("any-run")
    assert (count.visitors, count.signups) == (0, 0) and "MOCKED" in count.source


def test_demand_evaluation_against_the_preset_target():
    t = gates.DEMAND_TARGET
    assert (t["min_signup_rate"], t["min_cold_visitors"], t["window_days"]) == (0.05, 200, 14)
    assert demand.evaluate(t, 0, 0)[0] == "not_measured"
    assert demand.evaluate(t, 199, 50)[0] == "not_measured"  # too few visitors to read a rate
    assert demand.evaluate(t, 200, 10)[0] == "met"
    assert demand.evaluate(t, 400, 19)[0] == "missed"


def test_phase2_gate_rules():
    go = gates.phase2_gate(1, 1, True, "not_measured", "0 of 200", 1)
    assert go.outcome is Outcome.GO and any("NOT validated" in r for r in go.reasons)
    assert len(go.checks) == 3  # a mocked demand result is reported, not counted
    assert gates.phase2_gate(0, 1, True, "not_measured", "x", 1).outcome is Outcome.REPEAT
    assert gates.phase2_gate(0, 1, True, "not_measured", "x", 2).outcome is Outcome.STOP
    assert gates.phase2_gate(1, 1, False, "not_measured", "x", 2).outcome is Outcome.STOP
    measured = gates.phase2_gate(1, 1, True, "missed", "3 of 200", 2)  # a real, missed result does count
    assert measured.outcome is Outcome.STOP and len(measured.checks) == 4
    assert gates.phase2_gate(1, 1, True, "met", "12 of 200", 1).outcome is Outcome.GO

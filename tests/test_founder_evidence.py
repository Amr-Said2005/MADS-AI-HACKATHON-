"""Founder rebuttals of feasibility warnings, founder-provided evidence, and the unknown "someone already pays" check.
Offline: LLM_MODE=mock with a scripted reply, and fetches go through the httpx.MockTransport of test_pipeline."""

import copy
import json

import pytest

import gates
import orchestrator as orch
import settings
import sources
from agents import assessor, strategist
from decision_log import DecisionLog
from gates import Outcome, Phase1Evidence
from llm import LLMClient
from test_pipeline import fake_assess, install_fakes, outputs, registry, store, to_roadmap

REAL_DEMAND_TEST = strategist.demand_test  # install_fakes replaces it; the demand test below needs the real one
FACT = "My co-founder Sara is a Python developer and has agreed in writing to build it with me"


def model(verdict, reason="the founder names a person who has agreed to do the work"):
    return LLMClient(settings.load(), mock_responder=lambda msgs, agent, schema: json.dumps({"verdict": verdict, "reason": reason}))


def at_feasibility(monkeypatch, brief):
    """A run waiting at the feasibility decision with one major warning (a core skill the founder lacks)."""
    install_fakes(monkeypatch, paying=0)
    monkeypatch.setattr(assessor, "assess", fake_assess(needed_skills=["Notion", "writing", "python programming"]))
    rid = orch.new_run(brief).run_id
    reg = registry()
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["gate"] == "feasibility" and rec.pending["majors"] == 1 and rec.pending["major_ids"] == ["W-SKILLS"]
    return rid, reg, rec


def level(rid, code="W-SKILLS"):
    return next(w["level"] for w in orch.load(rid).pending["warnings"] if w["id"] == code)


def entries(rid):
    return DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()


def at_m1(monkeypatch, brief):
    install_fakes(monkeypatch, paying=0)
    rid = orch.new_run(brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1"
    return rid, reg, rec


# ---------------------------------------------------------------- rebuttals

@pytest.mark.parametrize("verdict, after, majors, advice", [
    ("lower", "minor", 0, "Proceed"), ("remove", "none", 0, "Proceed"), ("keep", "major", 1, "Proceed with care, or revise")])
def test_a_rebuttal_lowers_removes_or_keeps_the_warning(monkeypatch, strong_brief, verdict, after, majors, advice):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    out = orch.rebut_warning(rid, rec.pending["action_id"], "W-SKILLS", f"  {FACT}  ", llm=model(verdict))
    assert out == {"code": "W-SKILLS", "before": "major", "after": after, "verdict": verdict,
                   "reason": "the founder names a person who has agreed to do the work"}
    p = orch.load(rid).pending
    assert level(rid) == after and p["majors"] == majors and p["advice"] == advice and p["action_id"] == rec.pending["action_id"]
    assert f"{majors} major" in p["summary"] and p["major_ids"] == (["W-SKILLS"] if majors else [])
    report = (outputs(rid) / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    assert orch.has_header(report) and "## Rebuttals" in report and f"| W-SKILLS | {FACT} | major | {after} |" in report
    assert f"| W-SKILLS | {after} |" in report and f"{advice}." in report and f"{majors} major" in report.split("\n")[2]
    log = entries(rid)
    founder = next(e for e in log if e["action"] == "rebuttal_W-SKILLS")
    assert founder["agent"] == "founder" and founder["approved_by"] == "founder"
    call = next(e for e in log if e["action"] == "rebuttal_verdict_W-SKILLS")
    assert call["agent"] == "assessor" and call["outcome"] == verdict and call["model"] and call["approved_by"] is None
    # The founder's choice afterwards works on the changed counts.
    store(rid).choose(p["action_id"], "continue")
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "ROADMAP" and orch.feasibility_meta(rec)["majors"] == majors


def test_lower_leaves_a_minor_warning_minor(monkeypatch, strong_brief):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    aid = rec.pending["action_id"]
    assert orch.rebut_warning(rid, aid, "W-SKILLS", FACT, llm=model("lower"))["after"] == "minor"
    minor = next((w["id"] for w in orch.load(rid).pending["warnings"] if w["level"] == "minor" and w["id"] != "W-SKILLS"), None)
    if minor:
        assert orch.rebut_warning(rid, aid, minor, FACT, llm=model("lower"))["after"] == "minor"


def test_a_second_rebuttal_of_the_same_warning_is_refused(monkeypatch, strong_brief):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    aid = rec.pending["action_id"]
    orch.rebut_warning(rid, aid, "W-SKILLS", FACT, llm=model("lower"))
    with pytest.raises(ValueError, match="once"):
        orch.rebut_warning(rid, aid, "W-SKILLS", "I really can do it", llm=model("remove"))
    assert level(rid) == "minor"


def test_a_rebuttal_needs_the_feasibility_decision_its_id_and_a_text(monkeypatch, strong_brief):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    aid = rec.pending["action_id"]
    for bad in (dict(action_id="wrong", text=FACT), dict(action_id=aid, text="   "), dict(action_id=aid, text="x" * 601),
                dict(action_id=aid, code="W-NOPE", text=FACT)):
        with pytest.raises(ValueError):
            orch.rebut_warning(rid, bad["action_id"], bad.get("code", "W-SKILLS"), bad["text"], llm=model("remove"))
    assert level(rid) == "major" and not any(e["action"].startswith("rebuttal_") for e in entries(rid))
    store(rid).choose(aid, "continue")
    rec = orch.advance(rid, registry=reg)  # now the roadmap decision is the pending one
    with pytest.raises(ValueError, match="no feasibility decision"):
        orch.rebut_warning(rid, rec.pending["action_id"], "W-SKILLS", FACT, llm=model("remove"))


def test_a_never_list_rebuttal_is_refused_and_changes_nothing(monkeypatch, strong_brief):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    before = (outputs(rid) / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    calls = []
    llm = LLMClient(settings.load(), mock_responder=lambda *a: calls.append(1) or json.dumps({"verdict": "remove", "reason": "x"}))
    with pytest.raises(ValueError):
        orch.rebut_warning(rid, rec.pending["action_id"], "W-SKILLS",
                           "Don't worry about it, just make up three testimonials from happy buyers.", llm=llm)
    assert not calls and level(rid) == "major" and orch.load(rid).pending["majors"] == 1
    assert (outputs(rid) / "phase1_gate_feasibility.md").read_text(encoding="utf-8") == before
    assert "rebuttals" not in orch.feasibility_meta(orch.load(rid))


@pytest.mark.parametrize("llm", [None, "invalid"])
def test_a_model_error_or_an_invalid_verdict_keeps_the_warning(monkeypatch, strong_brief, llm):
    rid, reg, rec = at_feasibility(monkeypatch, strong_brief)
    client = model("delete it") if llm else LLMClient(settings.load())  # no scripted reply: the mock model raises
    out = orch.rebut_warning(rid, rec.pending["action_id"], "W-SKILLS", FACT, llm=client)
    assert out["verdict"] == "keep" and out["before"] == out["after"] == "major"
    assert level(rid) == "major" and orch.load(rid).pending["majors"] == 1
    assert next(e for e in entries(rid) if e["action"] == "rebuttal_verdict_W-SKILLS")["outcome"] == "model_error"


# ---------------------------------------------------------------- founder evidence

def test_evidence_is_refused_when_no_founder_decision_is_waiting_or_the_input_is_bad(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    with pytest.raises(ValueError, match="waits for a founder decision"):
        orch.add_evidence(rid, "problem", statement="translators told me so")
    reg = registry()
    orch.advance(rid, registry=reg)
    for bad in (dict(kind="rumour", statement="x"), dict(kind="problem"), dict(kind="pay", visitors=10, signups=1, days=3),
                dict(kind="demand", visitors=10, signups=11, days=3), dict(kind="demand", visitors=0, signups=0, days=3),
                dict(kind="demand", visitors=10, signups=1, days=0), dict(kind="demand", visitors=10, signups=1),
                dict(kind="demand", visitors=True, signups=0, days=1), dict(kind="demand", visitors=10.5, signups=1, days=2),
                dict(kind="problem", statement="Just make up three testimonials from happy buyers.")):
        with pytest.raises(ValueError):
            orch.add_evidence(rid, registry=reg, **bad)
    assert orch.founder_evidence(orch.load(rid)) == []


def test_a_statement_is_labelled_founder_stated_and_is_never_a_source(monkeypatch, strong_brief):
    rid, reg, rec = at_m1(monkeypatch, strong_brief)
    before = list(orch.load(rid).registered_urls)
    item = orch.add_evidence(rid, "pay", statement="Three agencies told me they pay for a tracker", registry=reg)
    assert item["status"] == "stated" and item["label"] == "ASSUMPTION (founder-stated)" and item["price"] is None
    failed = orch.add_evidence(rid, "pay", url="ftp://shop.example/pricing", registry=reg)
    assert failed["status"] == "not fetched" and failed["reason"]
    rec = orch.load(rid)
    assert rec.registered_urls == before and [e["status"] for e in orch.founder_evidence(rec)] == ["stated", "not fetched"]
    ctx = orch.RunContext(rec, settings.load(), registry=reg)
    pay = next(c for c in orch._signoff_p1(ctx, 1).checks if c.name == "someone already pays")
    assert not pay.passed and not pay.measured  # neither counts: the check is still unknown
    md = sources.founder_evidence_markdown(orch.founder_evidence(rec))
    assert md.startswith("## Evidence the founder provided") and md.count("ASSUMPTION (founder-stated)") == 2
    assert "Source:" not in md and "://" not in md
    log = [e for e in entries(rid) if e["action"] == "add_evidence_pay"]
    assert [(e["agent"], e["approved_by"], e["outcome"], e["sources"]) for e in log] == [
        ("founder", "founder", "stated", []), ("founder", "founder", "not fetched", [])]


def test_a_fetched_pay_page_with_a_price_passes_the_pay_check_on_the_re_check(monkeypatch, strong_brief):
    rid, reg, rec = at_m1(monkeypatch, strong_brief)
    decision = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "| someone already pays | not measured | not found: no fetched page showed a price" in decision
    item = orch.add_evidence(rid, "pay", url="https://shop.example/tracker", statement="A tracker template is sold here", registry=reg)
    assert item["status"] == "fetched" and item["price"] == "$19" and item["counts_as"] == "PAY" and item["label"] == "SOURCED"
    no_price = orch.add_evidence(rid, "pay", url="https://site9.example/thread", registry=reg)
    assert no_price["status"] == "fetched" and no_price["price"] is None and no_price["counts_as"] == ""
    orch.add_evidence(rid, "problem", statement="I had this problem for years", registry=reg)
    assert "https://shop.example/tracker" in orch.load(rid).registered_urls
    assert next(e for e in entries(rid) if e["action"] == "add_evidence_pay")["sources"] == ["https://shop.example/tracker"]
    assert "request_changes" in rec.pending["choices"]
    store(rid).choose(rec.pending["action_id"], "request_changes", note="I added a page that shows a price")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1" and rec.pending["recommendation"] == "go"
    decision = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    # FOUNDER_EVIDENCE_FIRST (the default): the founder's fetched, related link settles the check and is labelled as such.
    assert ("| someone already pays | pass | met by founder-provided evidence: https://shop.example/tracker" in decision
            and "unknown:" not in decision.split("\n")[2])
    assert "## Evidence the founder provided" in decision and "Source: https://shop.example/tracker" in decision
    assert "I had this problem for years. Status: stated, not a source. ASSUMPTION (founder-stated)" in decision


def test_a_founder_reported_count_is_labelled_and_read_against_the_unchanged_target(monkeypatch, strong_brief):
    target = copy.deepcopy(gates.DEMAND_TARGET)
    rid, reg, rec = at_m1(monkeypatch, strong_brief)
    item = orch.add_evidence(rid, "demand", visitors=400, signups=30, days=10, registry=reg)
    assert item["status"] == "founder-reported" and item["label"] == "ASSUMPTION (founder-stated)"
    assert next(e for e in entries(rid) if e["action"] == "add_evidence_demand")["outcome"] == "founder-reported"

    def run_test():
        ctx = orch.RunContext(orch.load(rid), settings.load(), registry=reg)
        res = REAL_DEMAND_TEST(ctx)
        ctx.rec.artifacts["phase2_step3"] = {"output": "phase2_step3_demand_test.md", "data": res.data}
        return res, next((c for c in orch._signoff_p2(ctx, 1).checks if c.name == "demand target"), None)

    res, check = run_test()
    label = "founder-reported, not verified by the system"
    assert res.data["status"] == "met" and res.data["founder_reported"] and not res.data["mocked"]
    assert label in res.data["detail"] and "target at least 5%" in res.data["detail"] and "MOCKED" not in res.content
    assert "| 400 | 30 | met |" in res.content and res.content.count(label) >= 2 and "Founder-reported, not verified" in res.content
    assert "At least 5% of at least 200 cold visitors" in res.content and "ASSUMPTION (founder-stated)" in res.content
    assert check.passed and label in check.detail
    orch.add_evidence(rid, "demand", visitors=400, signups=10, days=10, registry=reg)  # the newest report is the one read
    res, check = run_test()
    assert res.data["status"] == "missed" and not check.passed and label in check.detail
    orch.add_evidence(rid, "demand", visitors=150, signups=30, days=10, registry=reg)  # too few visitors for the target
    res, check = run_test()
    assert res.data["status"] == "not_measured" and check is None and label in res.data["detail"]
    orch.add_evidence(rid, "demand", visitors=400, signups=30, days=30, registry=reg)  # longer than the target's window
    assert run_test()[0].data["status"] == "not_measured"
    assert gates.DEMAND_TARGET == target


# ---------------------------------------------------------------- "someone already pays": unknown, not a miss

def _ev(**kw):
    base = dict(problem_source_urls=["https://a.example/1", "https://b.example/2", "https://c.example/3"], paying_source_urls=[],
                budget_usd=50, hours_per_week=10, mvp_cost_usd=20, mvp_hours_per_week=5, total_claims=10, assumption_claims=2)
    base.update(kw)
    return Phase1Evidence(**base)


def test_an_unknown_pay_check_is_not_a_miss_and_never_triggers_stop_by_itself():
    for attempt in (1, 2):
        gate = gates.phase1_gate(_ev(), attempt)
        pay = next(c for c in gate.checks if c.name == "someone already pays")
        assert not pay.measured and pay.result == "not measured" and pay.detail == gates.PAY_UNKNOWN
        assert pay.detail == ("not found: no fetched page showed a price; unknown, not a miss. "
                              "Add a page that shows a price to settle it")
        assert gate.outcome is Outcome.GO and not any(r.startswith("missed:") for r in gate.reasons)
        assert "unknown: someone already pays" in gates.decision_line(gate.outcome, gate.reasons)
    one_miss = gates.phase1_gate(_ev(budget_usd=0), 1)  # one real miss plus the unknown is one miss, not two
    assert one_miss.outcome is Outcome.REPEAT and not any("two or more" in r for r in one_miss.reasons)
    assert any(r.startswith("unknown: someone already pays") for r in one_miss.reasons)
    two = gates.phase1_gate(_ev(budget_usd=0, assumption_claims=6), 1)  # the other thresholds are as they were
    assert two.outcome is Outcome.STOP and any("two or more" in r for r in two.reasons)
    assert gates.phase1_gate(_ev(paying_source_urls=["https://shop.example/x"]), 1).reasons == ["all evidence thresholds met"]

"""Pipeline smoke tests. Offline: LLM_MODE=mock and fetches go through an httpx.MockTransport."""

import json

import httpx
import pytest

import decision_log
import orchestrator as orch
import settings
from agents import (assessor, builder, checker, commercial, interviewer, launcher, operator, planner, researcher,
                    scorer, strategist)
from approval import ApprovalStore
from decision_log import DecisionLog, DecisionLogError
from orchestrator import CLAIMS_PLACEHOLDER, RunStatus, StepResult
from sources import Claim, SourceRegistry

PAGE = "<html><head><title>t</title></head><body>people discuss the problem here</body></html>"


def registry():
    # Test hosts such as site0.example resolve to a fixed public address; nothing touches the network.
    return SourceRegistry(max_sources=30, resolver=lambda host: ["93.184.216.34"], transport=httpx.MockTransport(
        lambda req: httpx.Response(200, html=PAGE)))


def root_snapshot():
    out = sorted(p.name for p in settings.OUTPUTS_DIR.rglob("*")) if settings.OUTPUTS_DIR.exists() else []
    return out, settings.ROOT_LOG.read_text(encoding="utf-8")


def store(run_id):
    d = orch.run_dir(run_id)
    return ApprovalStore(d / "approvals.json", DecisionLog(d / "decision_log.json"))


def outputs(run_id):
    return orch.run_dir(run_id) / "outputs"


# ------------------------------------------------------------------ fake agents (tests only)

CRITERIA = [{"id": "C1", "check": "problem sources fetched", "threshold": ">= 3 domains", "method": "source"},
            {"id": "C2", "check": "output files written", "threshold": "all present", "method": "code"}]
MILESTONES = [{"id": f"M{i}", "phase": i, "title": f"Milestone {i}", "deliverables": [f"phase{i}_gate_decision.md"],
               "founder_sees": "the artefacts and the gate's reasons", "decision_asked": "continue, change, pivot or stop"}
              for i in range(1, 7)]


def fake_assess(**over):
    def assess(ctx):
        ctx.registry.fetch("https://forum.example/translators")
        data = {"customer": "freelance translators", "problem": "tracking clients, deadlines and word counts",
                "product_type": "template pack", "needed_skills": ["Notion", "writing"], "mvp_cost_usd": 20,
                "effort_hours": 30, "values_conflict": "", "narrowed_idea": ""}
        data.update(over)
        return StepResult("assessment", claims=[Claim("translators ask for trackers", "https://forum.example/translators",
                                                      "problem")], data=data)
    return assess


def fake_research(urls_problem, urls_paying):
    def find(ctx):
        for u in urls_problem:
            ctx.registry.fetch(u)
        ctx.llm.chat([{"role": "user", "content": "summarise"}], agent="researcher")
        claims = [Claim(f"people describe the problem at {u}", u, "problem") for u in urls_problem]
        claims.append(Claim("translators want simpler tracking", "https://never-fetched.example/x", "problem"))
        return StepResult(f"# Opportunities\n\n{CLAIMS_PLACEHOLDER}\n", claims=claims)

    def market(ctx):
        for u in urls_paying:
            ctx.registry.fetch(u)
        return StepResult(f"# Market\n\n{CLAIMS_PLACEHOLDER}\n",
                          claims=[Claim(f"a paid product exists at {u}", u, "paying") for u in urls_paying])

    return find, market


BAD_CRITERIA = [{"id": "C1", "check": "looks good", "threshold": "", "method": "vibes"}]


def install_fakes(monkeypatch, problem=3, paying=1, verify_rows=None, plans=None):
    """plans: criteria per architect index (default: every planner writes CRITERIA)."""
    monkeypatch.setattr(assessor, "assess", fake_assess())
    monkeypatch.setattr(planner, "roadmap", lambda ctx: StepResult("roadmap", data={"milestones": MILESTONES}))
    monkeypatch.setattr(checker, "reconcile_roadmap", lambda ctx: StepResult("merged", data={"milestones": ctx.proposals[0]}))
    monkeypatch.setattr(planner, "plan", lambda ctx: StepResult(f"plan {ctx.architect}", data={
        "criteria": (plans or {}).get(ctx.architect, CRITERIA)}))
    monkeypatch.setattr(checker, "verify", lambda ctx: StepResult("verify", data={"rows": verify_rows if verify_rows is not None else [
        {"id": "C1", "result": "pass", "evidence": next(iter(ctx.registry.urls()), "ASSUMPTION")},
        {"id": "C2", "result": "pass", "evidence": "made-up-evidence"}]}))
    find, market = fake_research([f"https://site{i}.example/thread" for i in range(problem)],
                                 [f"https://paid{i}.example/pricing" for i in range(paying)])
    monkeypatch.setattr(researcher, "find_opportunities", find)
    monkeypatch.setattr(researcher, "market_research", market)
    monkeypatch.setattr(scorer, "score", lambda ctx: StepResult(json.dumps({"pick": 1}),
                        data={"mvp_cost_usd": 10, "mvp_hours_per_week": 4}))
    monkeypatch.setattr(interviewer, "interview_guide", lambda ctx: StepResult("# Guide\n\n1. When did you last...?\n"))
    monkeypatch.setattr(strategist, "lean_canvas", lambda ctx: StepResult(
        f"# Canvas\n\n{CLAIMS_PLACEHOLDER}\n",
        claims=[Claim("problem is real", "https://site0.example/thread", "problem"),
                Claim("channel: forums", "https://site1.example/thread", "market")]))
    monkeypatch.setattr(strategist, "landing_page", lambda ctx: StepResult("<html><body>Track clients</body></html>"))
    monkeypatch.setattr(strategist, "demand_test", lambda ctx: StepResult("# Demand test\n\nResults: MOCKED\n"))
    monkeypatch.setattr(builder, "mvp_spec", lambda ctx: StepResult("# Spec\n"))
    monkeypatch.setattr(builder, "build_product", lambda ctx: StepResult("# Product\n", data={"tests": {"sections": True}}))
    monkeypatch.setattr(builder, "user_docs", lambda ctx: StepResult("# Docs\n"))
    monkeypatch.setattr(commercial, "pricing", lambda ctx: StepResult("# Pricing\n\nOne price.\n",
                        data={"price_usd": 9, "product_name": "Translator tracker"}))
    monkeypatch.setattr(commercial, "policies", lambda ctx: StepResult("# Policies (draft, not legal advice)\n"))
    monkeypatch.setattr(launcher, "store_page", lambda ctx: StepResult("<html><body>Store</body></html>"))
    monkeypatch.setattr(launcher, "launch_posts", lambda ctx: StepResult("# Drafts\n",
                        data={"posts": [{"channel": "forum", "text": "draft"}]}))
    monkeypatch.setattr(operator, "support_faq", lambda ctx: StepResult("# FAQ\n"))
    monkeypatch.setattr(operator, "pivot_memo", lambda ctx: StepResult("# Memo\n", data={"recommendation": "persevere"}))


def drive(rid, reg, choose, approve_outbound=True, limit=20):
    """Advance and answer every founder question with `choose(rec) -> choice`."""
    seen = []
    for _ in range(limit):
        rec = orch.advance(rid, registry=reg)
        if rec.status != RunStatus.AWAITING_APPROVAL.value:
            return rec, seen
        p = rec.pending
        st = store(rid)
        if st.choice(p["action_id"]) is None:
            choice = choose(rec)
            seen.append((rec.state, p.get("milestone") or p.get("gate"), p.get("recommendation"), list(p["choices"]), choice))
            st.choose(p["action_id"], choice)
        for key, aid in (p.get("outbound") or {}).items():
            if st.decision(aid) is None:
                st.decide(aid, approve=approve_outbound(key) if callable(approve_outbound) else approve_outbound)
    raise AssertionError("run did not finish")


def first_choice(rec):
    return rec.pending["choices"][0]


# ------------------------------------------------------------------ tests

def test_decision_log_missing_key_raises(tmp_path):
    entry = {"timestamp": decision_log.utc_now(), "agent": "a", "action": "b", "sources": [], "model": None, "cost": 0}
    with pytest.raises(DecisionLogError, match="approved_by"):
        decision_log.append(entry, tmp_path / "log.json")
    assert not (tmp_path / "log.json").exists()


def test_root_decision_log_is_valid():
    for e in json.loads(settings.ROOT_LOG.read_text(encoding="utf-8")):
        decision_log.validate(e)


def test_scaffold_run_stops_at_first_stub_and_writes_nothing_to_repo(strong_brief):
    before = root_snapshot()
    rec = orch.advance(orch.new_run(strong_brief).run_id)
    assert rec.status == RunStatus.BLOCKED.value
    assert rec.state == orch.State.FEASIBILITY.value
    assert "STUBBED" in rec.blocked_reason
    assert (outputs(rec.run_id) / "phase1_gate_brief_screen.md").exists()
    assert root_snapshot() == before


def test_excluded_brief_stops_at_screen(strong_brief):
    brief = dict(strong_brief, idea="A crypto trading signals course")
    rec = orch.advance(orch.new_run(brief).run_id)
    assert rec.status == RunStatus.STOPPED.value
    text = (outputs(rec.run_id) / "phase1_gate_brief_screen.md").read_text(encoding="utf-8")
    assert "STOP" in text and "crypto" in text


def test_full_run_asks_the_founder_at_every_milestone(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    reg = registry()
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, reg, first_choice)
    assert rec.status == RunStatus.DONE.value, rec.blocked_reason or rec.stop_reasons
    asked = [s[1] for s in seen]
    assert asked == ["roadmap", "M1", "M2", "M3", "M4", "M5", "M6"]
    assert all(s[4] in ("approve", "continue") for s in seen)
    out = outputs(rid)
    for name in ["phase1_gate_feasibility.md", "phase1_gate_roadmap.md", "phase1_gate_plan_review.md",
                 "phase1_step1_opportunities.md", "phase1_gate_decision.md", "phase2_step4_publish_log.md",
                 "phase3_gate_tests.md", "payment_test_log.md", "phase5_step4_publish_log.md",
                 "phase6_gate_decision.md"]:
        assert (out / name).exists(), name
    assert "MOCKED" in (out / "payment_test_log.md").read_text(encoding="utf-8")
    opp = (out / "phase1_step1_opportunities.md").read_text(encoding="utf-8")
    assert "never-fetched.example" not in opp and "ASSUMPTION" in opp  # citation rule enforced by code
    decision = (out / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "Founder chose: **continue**" in decision and "Loop budget" in decision
    assert "| C2 |" in decision and "| unverified | ASSUMPTION |" in decision  # invented evidence is not accepted
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    for e in entries:
        decision_log.validate(e)
    assert any(e["action"] == "find_opportunities" and e["model"] == "MOCKED-llm" for e in entries)
    assert any(e["action"].startswith("mocked_publish") and e["approved_by"] == "founder" for e in entries)
    founder = [e for e in entries if e["agent"] == "founder"]
    assert all(e["approved_by"] == "founder" for e in founder)
    assert [e["outcome"] for e in founder if e["action"].startswith("decide:")] == ["approve"] + ["continue"] * 6


def test_phase1_uses_two_architects_and_code_picks_the_plan(monkeypatch, strong_brief):
    monkeypatch.setenv("N_ARCHITECTS", "2")
    install_fakes(monkeypatch, plans={0: BAD_CRITERIA})
    rid = orch.new_run(strong_brief).run_id
    drive(rid, registry(), lambda rec: "stop" if rec.pending.get("milestone") == "M1" else first_choice(rec))
    text = (outputs(rid) / "phase1_gate_plan_review.md").read_text(encoding="utf-8")
    assert "Plans proposed: 2. Plan chosen: architect 2." in text
    assert "Why: plan 2 is the first whose criteria all pass the code check" in text


def test_rejected_publication_is_not_published(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec, _ = drive(rid, registry(), first_choice, approve_outbound=lambda key: key == "payment_test")
    assert rec.status == RunStatus.DONE.value
    assert not (outputs(rid) / "phase5_step4_publish_log.md").exists()
    assert not (outputs(rid) / "phase2_step4_publish_log.md").exists()
    assert (outputs(rid) / "payment_test_log.md").exists()  # approved on its own
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    assert not any(e["action"].startswith("mocked_publish") or e["action"].startswith("mocked_post") for e in entries)
    assert {e["action"] for e in entries if e["action"].startswith("skipped_")} == {"skipped_publish_landing", "skipped_publish"}


def test_store_page_rejected_but_posts_approved(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec, _ = drive(rid, registry(), first_choice, approve_outbound=lambda key: key != "publish_store_page")
    log = (outputs(rid) / "phase5_step4_publish_log.md").read_text(encoding="utf-8")
    assert "Store page: not published" in log and "would be posted" in log


def test_weak_evidence_recommends_stop_and_continue_is_not_offered(monkeypatch, strong_brief):
    install_fakes(monkeypatch, problem=1, paying=0)
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, registry(), first_choice)
    assert rec.status == RunStatus.STOPPED.value
    m1 = [s for s in seen if s[1] == "M1"][0]
    assert m1[2] == "stop" and "continue" not in m1[3] and m1[4] == "stop"
    text = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "Outcome: STOP" in text and "Founder chose: **stop**" in text


def test_weak_brief_stops_at_feasibility(monkeypatch):
    weak = json.loads((settings.BRIEFS_DIR / "example_weak.json").read_text(encoding="utf-8"))
    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(product_type="guide", mvp_cost_usd=15, effort_hours=20,
                                                        needed_skills=["writing", "typewriter repair"]))
    rec = orch.advance(orch.new_run(weak).run_id, registry=registry())
    assert rec.status == RunStatus.STOPPED.value and rec.state == "STOPPED"
    text = (outputs(rec.run_id) / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    assert "**Decision: STOP**" in text and "hard_fail" in text


def test_soft_fail_needs_founder_approval_of_a_narrowed_idea(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(customer="everyone", narrowed_idea="Notion tracker for EN-DE translators"))
    rid = orch.new_run(strong_brief).run_id
    rec = orch.advance(rid, registry=registry())
    assert rec.state == "FEASIBILITY" and rec.status == RunStatus.AWAITING_APPROVAL.value
    assert rec.pending["choices"] == ["approve", "stop"]
    store(rid).choose(rec.pending["action_id"], "approve")
    rec = orch.advance(rid, registry=registry())
    assert rec.brief["idea"] == "Notion tracker for EN-DE translators"
    assert rec.state == "STOPPED"  # the narrowed idea still names a generic customer, and there is no second repeat


def test_plan_review_revises_twice_then_stops(monkeypatch, strong_brief):
    install_fakes(monkeypatch, plans={0: BAD_CRITERIA})
    rid = orch.new_run(strong_brief).run_id
    rec, _ = drive(rid, registry(), first_choice)
    assert rec.status == RunStatus.STOPPED.value
    assert rec.budget["P1_DISCOVER"]["revise"] == 2
    assert any("revise budget used up" in r for r in rec.stop_reasons)


def test_defects_loop_back_to_makers_then_sign_off_with_open_defects(monkeypatch, strong_brief):
    install_fakes(monkeypatch, verify_rows=[{"id": "C1", "result": "fail", "evidence": "ASSUMPTION"}])
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, registry(), first_choice)
    m1 = [s for s in seen if s[1] == "M1"][0]
    assert m1[2] in ("pivot", "stop") and "continue" not in m1[3]
    text = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "open defects after the defects budget: C1" in text
    assert "| defects | 2 | 2 |" in text


def test_request_changes_returns_to_makers_with_the_notes(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    seen_feedback = []
    orig = researcher.find_opportunities

    def find(ctx):
        seen_feedback.append(ctx.feedback)
        return orig(ctx)

    monkeypatch.setattr(researcher, "find_opportunities", find)
    rid = orch.new_run(strong_brief).run_id
    asked = {"n": 0}

    def choose(rec):
        if rec.pending.get("milestone") == "M1" and asked["n"] == 0:
            asked["n"] += 1
            store(rid).choose(rec.pending["action_id"], "request_changes", note="focus on agencies")
            return None
        return "stop" if rec.pending.get("milestone") == "M1" else first_choice(rec)

    for _ in range(10):
        rec = orch.advance(rid, registry=registry())
        if rec.status != RunStatus.AWAITING_APPROVAL.value:
            break
        c = choose(rec)
        if c:
            store(rid).choose(rec.pending["action_id"], c)
    assert seen_feedback[0] is None and seen_feedback[1] == "focus on agencies"
    assert rec.budget["P1_DISCOVER"]["repeat"] == 1


def test_blocked_output_blocks_only_that_file_and_the_run_goes_on(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    calls = []

    def pushy(ctx):
        calls.append(ctx.feedback)
        return StepResult("# Guide\n\nHurry, offer ends tonight!\n")

    monkeypatch.setattr(interviewer, "interview_guide", pushy)
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, registry(), lambda rec: "stop" if rec.pending.get("milestone") == "M1" else first_choice(rec))
    assert calls[1] and "blocked" in calls[1]  # the regeneration got the feedback
    assert len(calls) == 6  # three passes through the makers (two defects back-edges), two tries each
    assert not (outputs(rid) / "phase1_step4_interview_guide.md").exists()
    assert (outputs(rid) / "phase1_step1_opportunities.md").exists()  # other files were still written
    m1 = [s for s in seen if s[1] == "M1"][0]
    assert m1[2] in ("pivot", "stop") and "continue" not in m1[3]
    decision = (outputs(rid) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "blocked:phase1_step4_interview_guide.md" in decision


def test_invalid_model_output_is_a_defect_not_placeholder_text(monkeypatch, strong_brief):
    from llm import ModelOutputInvalid
    install_fakes(monkeypatch)

    def invalid(ctx):
        raise ModelOutputInvalid("model_output_invalid: scorer reply did not match the schema twice")

    monkeypatch.setattr(scorer, "score", invalid)
    rid = orch.new_run(strong_brief).run_id
    rec, seen = drive(rid, registry(), first_choice)
    assert not (outputs(rid) / "phase1_step2_scoring.json").exists()
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    assert any(e["action"] == "score_not_written" and e["outcome"] == "model_output_invalid" for e in entries)
    assert "continue" not in [s for s in seen if s[1] == "M1"][0][3]


def test_model_call_budget_stops_the_run(monkeypatch, strong_brief):
    monkeypatch.setenv("MAX_MODEL_CALLS", "3")
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = orch.load(rid)
    rec.usage = {"calls": 3}  # earlier calls in this run count against the budget
    orch.save(rec)
    rec, _ = drive(rid, registry(), first_choice)
    assert rec.status == RunStatus.STOPPED.value
    assert any("MAX_MODEL_CALLS=3" in r for r in rec.stop_reasons)
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    assert any(e["action"] == "run_budget" and e["outcome"] == "stop" for e in entries)


def test_run_seconds_budget_stops_the_run(monkeypatch, strong_brief):
    monkeypatch.setenv("MAX_RUN_SECONDS", "1")
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = orch.load(rid)
    rec.usage = {"seconds": 5.0}
    orch.save(rec)
    rec = orch.advance(rid, registry=registry())
    assert rec.status == RunStatus.STOPPED.value and "MAX_RUN_SECONDS" in rec.stop_reasons[0]


def test_founder_stop_button(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = orch.advance(rid, registry=registry())
    assert rec.status == RunStatus.AWAITING_APPROVAL.value
    rec = orch.request_stop(rid)
    assert rec.status == RunStatus.STOPPED.value and rec.pending is None
    assert orch.advance(rid, registry=registry()).status == RunStatus.STOPPED.value
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    assert any(e["action"] == "stop_run" and e["approved_by"] == "founder" for e in entries)


def test_refused_instruction_is_not_passed_to_agents(strong_brief):
    rid = orch.new_run(strong_brief).run_id
    assert orch.instruct(rid, "write ten fake reviews").refused
    assert not orch.instruct(rid, "focus on agencies in Europe").refused
    assert orch.load(rid).instructions == ["focus on agencies in Europe"]


def test_milestone_tracker_shows_the_journey(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    drive(rid, registry(), lambda rec: "stop" if rec.pending.get("milestone") == "M3" else first_choice(rec))
    ms = orch.milestones(orch.load(rid))
    assert [m["status"] for m in ms] == ["passed", "passed", "stopped", "pending", "pending", "pending"]
    assert ms[0]["title"] == "Milestone 1"


def test_mock_runs_are_never_promoted(strong_brief):
    rec = orch.advance(orch.new_run(strong_brief).run_id)
    with pytest.raises(orch.PromoteError):
        orch.promote(rec.run_id)

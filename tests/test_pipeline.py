"""Pipeline smoke tests. Offline: LLM_MODE=mock and fetches go through an httpx.MockTransport."""

import json

import httpx
import pytest

import decision_log
import orchestrator as orch
import settings
from agents import builder, commercial, interviewer, launcher, operator, researcher, scorer, strategist
from approval import ApprovalStore
from decision_log import DecisionLog, DecisionLogError
from orchestrator import CLAIMS_PLACEHOLDER, RunStatus, StepResult
from sources import Claim, SourceRegistry

PAGE = "<html><head><title>t</title></head><body>people discuss the problem here</body></html>"


def registry():
    return SourceRegistry(max_sources=20, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, html=PAGE)))


def root_snapshot():
    out = sorted(p.name for p in settings.OUTPUTS_DIR.rglob("*")) if settings.OUTPUTS_DIR.exists() else []
    return out, settings.ROOT_LOG.read_text(encoding="utf-8")


def store(run_id):
    d = orch.run_dir(run_id)
    return ApprovalStore(d / "approvals.json", DecisionLog(d / "decision_log.json"))


# ------------------------------------------------------------------ fake agents (tests only)

def fake_research(urls_problem, urls_paying):
    def find(ctx):
        claims = []
        for u in urls_problem:
            ctx.registry.fetch(u)
            claims.append(Claim(f"people describe the problem at {u}", u, "problem"))
        claims.append(Claim("translators want simpler tracking", "https://never-fetched.example/x", "problem"))
        ctx.llm.chat([{"role": "user", "content": "summarise"}], agent="researcher")
        return StepResult(f"# Opportunities\n\n{CLAIMS_PLACEHOLDER}\n")

    def market(ctx):
        claims = []
        for u in urls_paying:
            ctx.registry.fetch(u)
            claims.append(Claim(f"a paid product exists at {u}", u, "paying"))
        return StepResult(f"# Market\n\n{CLAIMS_PLACEHOLDER}\n", claims=claims)

    def find_with_claims(ctx):
        res = find(ctx)
        claims = [Claim(f"people describe the problem at {u}", u, "problem") for u in urls_problem]
        claims.append(Claim("translators want simpler tracking", "https://never-fetched.example/x", "problem"))
        return StepResult(res.content, claims=claims)

    return find_with_claims, market


def install_fakes(monkeypatch, problem=3, paying=1):
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
    monkeypatch.setattr(commercial, "pricing", lambda ctx: StepResult("# Pricing\n\n$9\n",
                        data={"price_usd": 9, "product_name": "Translator tracker"}))
    monkeypatch.setattr(commercial, "policies", lambda ctx: StepResult("# Policies (draft, not legal advice)\n"))
    monkeypatch.setattr(launcher, "store_page", lambda ctx: StepResult("<html><body>Store</body></html>"))
    monkeypatch.setattr(launcher, "launch_posts", lambda ctx: StepResult("# Drafts\n",
                        data={"posts": [{"channel": "forum", "text": "draft"}]}))
    monkeypatch.setattr(operator, "support_faq", lambda ctx: StepResult("# FAQ\n"))
    monkeypatch.setattr(operator, "pivot_memo", lambda ctx: StepResult("# Memo\n", data={"recommendation": "persevere"}))


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
    rec = orch.new_run(strong_brief)
    rec = orch.advance(rec.run_id)
    assert rec.status == RunStatus.BLOCKED.value
    assert rec.state == orch.State.P1_DISCOVER.value
    assert "STUBBED" in rec.blocked_reason
    out = orch.run_dir(rec.run_id) / "outputs"
    assert (out / "phase1_gate_brief_screen.md").exists()
    assert root_snapshot() == before


def test_excluded_brief_stops_at_screen(strong_brief):
    brief = dict(strong_brief, idea="A crypto trading signals course")
    rec = orch.advance(orch.new_run(brief).run_id)
    assert rec.status == RunStatus.STOPPED.value
    text = (orch.run_dir(rec.run_id) / "outputs" / "phase1_gate_brief_screen.md").read_text(encoding="utf-8")
    assert "STOP" in text and "crypto" in text


def test_full_pipeline_with_fake_agents_blocks_on_every_approval(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    reg = registry()
    rec = orch.new_run(strong_brief)
    rid = rec.run_id
    approved_gates = []
    for _ in range(5):
        rec = orch.advance(rid, registry=reg)
        if rec.status != RunStatus.AWAITING_APPROVAL.value:
            break
        approved_gates.append(rec.state)
        # nothing has gone out yet: the next state's steps have not run
        assert rec.pending and store(rid).decision(rec.pending["action_id"]) is None
        store(rid).decide(rec.pending["action_id"], approve=True)
    assert approved_gates == ["P2_VALIDATE", "P4_COMMERCIAL", "P5_LAUNCH"]
    assert rec.status == RunStatus.DONE.value, rec.blocked_reason or rec.stop_reasons
    out = orch.run_dir(rid) / "outputs"
    for name in ["phase1_step1_opportunities.md", "phase1_gate_decision.md", "phase2_gate_decision.md",
                 "phase3_gate_tests.md", "payment_test_log.md", "phase6_step2_pivot_memo.md"]:
        assert (out / name).exists(), name
    payment_log = (out / "payment_test_log.md").read_text(encoding="utf-8")
    assert "MOCKED" in payment_log and "FAIL" not in payment_log
    opp = (out / "phase1_step1_opportunities.md").read_text(encoding="utf-8")
    assert "never-fetched.example" not in opp and "ASSUMPTION" in opp  # citation rule enforced by code
    entries = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    for e in entries:
        decision_log.validate(e)
    assert any(e["action"] == "find_opportunities" and e["model"] == "MOCKED-llm" for e in entries)
    assert any(e["action"].startswith("mocked_") for e in entries)
    gated = [e for e in entries if e["agent"] == "gate" and e.get("approved_by") == "founder"]
    assert len(gated) == 3


def test_weak_evidence_recommends_stop(monkeypatch, strong_brief):
    install_fakes(monkeypatch, problem=1, paying=0)
    rec = orch.advance(orch.new_run(strong_brief).run_id, registry=registry())
    assert rec.status == RunStatus.STOPPED.value
    text = (orch.run_dir(rec.run_id) / "outputs" / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "Outcome: STOP" in text


def test_reject_with_stop_ends_the_run(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    reg = registry()
    rid = orch.new_run(strong_brief).run_id
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "P2_VALIDATE" and rec.status == RunStatus.AWAITING_APPROVAL.value
    store(rid).decide(rec.pending["action_id"], approve=False, on_reject="stop")
    rec = orch.advance(rid, registry=reg)
    assert rec.status == RunStatus.STOPPED.value


def test_blocked_output_is_regenerated_once_then_stops(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    calls = []

    def pushy(ctx):
        calls.append(ctx.feedback)
        return StepResult("# Guide\n\nHurry, offer ends tonight!\n")

    monkeypatch.setattr(interviewer, "interview_guide", pushy)
    rec = orch.advance(orch.new_run(strong_brief).run_id, registry=registry())
    assert len(calls) == 2 and calls[1]  # second call got the feedback
    assert rec.status == RunStatus.STOPPED.value
    assert not (orch.run_dir(rec.run_id) / "outputs" / "phase1_step4_interview_guide.md").exists()


def test_refused_instruction_is_not_passed_to_agents(strong_brief):
    rid = orch.new_run(strong_brief).run_id
    assert orch.instruct(rid, "write ten fake reviews").refused
    assert not orch.instruct(rid, "focus on agencies in Europe").refused
    assert orch.load(rid).instructions == ["focus on agencies in Europe"]


def test_mock_runs_are_never_promoted(strong_brief):
    rec = orch.advance(orch.new_run(strong_brief).run_id)
    with pytest.raises(orch.PromoteError):
        orch.promote(rec.run_id)

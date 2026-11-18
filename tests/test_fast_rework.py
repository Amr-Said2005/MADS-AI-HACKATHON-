"""Fewer model calls and no needless rework: only the failed step is redone, the plan is a template that code
checks, a failed quality check can be a note for the founder, and a founder-provided link comes first.
Offline: LLM_MODE=mock with a scripted reply that counts every model call, and the fetch transport of test_pipeline."""

import json
from collections import Counter

import pytest

import gates
import orchestrator as orch
import settings
from agents import checker, commercial, interviewer, planner, researcher, scorer
from decision_log import DecisionLog
from llm import LLMClient, ModelOutputInvalid
from orchestrator import CLAIMS_PLACEHOLDER, StepResult
from sources import Claim
from test_pipeline import CRITERIA, first_choice, install_fakes, outputs, registry, store

REAL_PLAN, REAL_VERIFY = planner.plan, checker.verify  # install_fakes replaces both; these tests use the real ones

OPPS = [{"id": "O1", "title": "Deadline tracker", "customer": "freelance translators", "problem": "missed deadlines"},
        {"id": "O2", "title": "Word count log", "customer": "translators with direct clients", "problem": "word counts"}]
GOOD_GUIDE = "# Guide\n\n" + "".join(f"{i}. Tell me about the last time case {i} happened.\n" for i in range(1, 7))
BAD_GUIDE = "# Guide\n\n1. When did you last track a deadline?\n"
MARKET = f"# Market\n\n## What exists and what people pay\n\n{CLAIMS_PLACEHOLDER}\n\n## Competitors\n\nNone found.\n"
FOUNDER_DETAIL = "met by founder-provided evidence: {} (the founder's link, loosely related to the brief; not evidence the system found)"


def client(counts, plan_reply="{}", verify_rows=None):
    """A mock model that counts its calls by reply schema ("Plan", "Verification") or, for free text, by agent."""
    def respond(messages, agent, schema):
        name = schema.__name__ if schema else f"chat:{agent}"
        counts[name] += 1
        if name == "Plan":
            return plan_reply
        if name == "Verification":
            return json.dumps({"rows": verify_rows or []})
        return "ok"
    return LLMClient(settings.load(), mock_responder=respond)


def fakes(monkeypatch, problem=3, paying=1, opps=None, guide=None):
    """Phase 1 makers that meet the template's default criteria; each makes exactly one model call per run.
    `problem`, `opps` and `guide` may be functions of the call number, to change what a re-run produces."""
    install_fakes(monkeypatch, paying=paying)
    monkeypatch.setattr(planner, "plan", REAL_PLAN)
    monkeypatch.setattr(checker, "verify", REAL_VERIFY)
    ran = Counter()

    def find(ctx):
        ran["find"] += 1
        ctx.llm.chat([{"role": "user", "content": "x"}], agent="researcher")
        urls = [f"https://site{i}.example/thread" for i in range(problem(ran["find"]) if callable(problem) else problem)]
        for u in urls:
            ctx.registry.fetch(u)
        return StepResult(f"# Opportunities\n\n{CLAIMS_PLACEHOLDER}\n", claims=[Claim(f"described at {u}", u, "problem") for u in urls],
                          data={"opportunities": opps(ran["find"]) if callable(opps) else OPPS})

    def score(ctx):
        ran["score"] += 1
        ctx.llm.chat([{"role": "user", "content": "x"}], agent="scorer")
        return StepResult(json.dumps({"pick": "O1"}), data={"pick": "O1", "mvp_cost_usd": 10, "mvp_hours_per_week": 4})

    def market(ctx):
        ran["market"] += 1
        ctx.llm.chat([{"role": "user", "content": "x"}], agent="researcher")
        urls = [f"https://paid{i}.example/pricing" for i in range(paying)]
        for u in urls:
            ctx.registry.fetch(u)
        return StepResult(MARKET, claims=[Claim(f"a paid product exists at {u}", u, "paying") for u in urls])

    def guide_step(ctx):
        ran["guide"] += 1
        ctx.llm.chat([{"role": "user", "content": "x"}], agent="interviewer")
        text = guide(ran["guide"]) if callable(guide) else GOOD_GUIDE
        if isinstance(text, Exception):
            raise text
        return StepResult(text)

    monkeypatch.setattr(researcher, "find_opportunities", find)
    monkeypatch.setattr(scorer, "score", score)
    monkeypatch.setattr(researcher, "market_research", market)
    monkeypatch.setattr(interviewer, "interview_guide", guide_step)
    return ran


def to_m1(strong_brief, counts, **llm_kw):
    """Run to the first milestone review. Returns (run id, registry, model client, record)."""
    rid = orch.new_run(strong_brief).run_id
    reg, llm = registry(), client(counts, **llm_kw)
    rec = orch.advance(rid, llm=llm, registry=reg)
    store(rid).choose(rec.pending["action_id"], "continue")  # feasibility
    rec = orch.advance(rid, llm=llm, registry=reg)
    store(rid).choose(rec.pending["action_id"], "approve")  # roadmap
    rec = orch.advance(rid, llm=llm, registry=reg)
    assert rec.pending["milestone"] == "M1", rec.blocked_reason or rec.stop_reasons
    return rid, reg, llm, rec


def entries(rid):
    return DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()


def kept(rid):
    return [e["action"] for e in entries(rid) if e["agent"] == "orchestrator" and e["action"].startswith("kept_")
            and e["outcome"] == "unchanged"]


def decision(rid, n=1):
    return (outputs(rid) / f"phase{n}_gate_decision.md").read_text(encoding="utf-8")


def review(rid, n=1):
    return (outputs(rid) / f"phase{n}_gate_plan_review.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. redo only what failed

def test_a_defect_on_one_file_reruns_that_step_and_only_the_dependents_whose_input_changed(monkeypatch, strong_brief):
    # The first opportunities file lists one opportunity (C1 needs 2). The re-run lists two, so the scorer, which
    # reads the opportunities, runs again; the chosen opportunity is the same, so the market file and the guide are kept.
    ran = fakes(monkeypatch, opps=lambda call: OPPS[:1] if call == 1 else OPPS)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert dict(ran) == {"find": 2, "score": 2, "market": 1, "guide": 1}
    assert kept(rid) == ["kept_market_research", "kept_interview_guide"]
    assert rec.budget["P1_DISCOVER"] == {"revise": 0, "defects": 1, "repeat": 0} and rec.pending["recommendation"] == "go"
    assert any(h["reasons"] == ["defects: C1"] for h in rec.history)
    assert "| C1 |" in decision(rid) and "| pass | code: 2 opportunities (need 2) |" in decision(rid)
    # Model calls: one plan, six maker runs, and no checker call (every criterion is measured by code).
    assert counts["Plan"] == 1 and counts["Verification"] == 0 and sum(counts.values()) == 7


def test_a_gate_repeat_reruns_the_step_tied_to_the_missed_check(monkeypatch, strong_brief):
    # Two problem sources miss "problem sources" (needs 3): one miss, so the gate asks for a repeat. Only
    # find_opportunities is tied to that check. Its claims changed, and the scorer reads them, so it runs again;
    # the chosen opportunity did not change, so the market research and the interview guide are kept.
    ran = fakes(monkeypatch, problem=lambda call: 2 if call == 1 else 3)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert dict(ran) == {"find": 2, "score": 2, "market": 1, "guide": 1}
    assert kept(rid) == ["kept_market_research", "kept_interview_guide"]
    assert rec.budget["P1_DISCOVER"]["repeat"] == 1 and rec.budget["P1_DISCOVER"]["defects"] == 0
    assert rec.pending["recommendation"] == "go"
    assert [e["outcome"] for e in entries(rid) if e["action"] == "phase1_evidence_gate"] == ["repeat", "go"]
    assert counts["Plan"] == 1 and counts["Verification"] == 0 and sum(counts.values()) == 7


def test_a_gate_repeat_in_phase_4_reruns_only_the_policies(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    ran = Counter()

    def pricing(ctx):
        ran["pricing"] += 1
        return StepResult("# Pricing\n\nOne price.\n", data={"price_usd": 9, "product_name": "Translator tracker"})

    def policies(ctx):
        ran["policies"] += 1
        return StepResult("# Policies (draft)\n" if ran["policies"] == 1 else "# Policies (draft, not legal advice)\n")

    monkeypatch.setattr(commercial, "pricing", pricing)
    monkeypatch.setattr(commercial, "policies", policies)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    for _ in range(12):
        rec = orch.advance(rid, registry=reg)
        if rec.pending.get("milestone") == "M4":
            break
        store(rid).choose(rec.pending["action_id"], first_choice(rec))
        for aid in (rec.pending.get("outbound") or {}).values():
            store(rid).decide(aid, approve=True)
    assert rec.pending["milestone"] == "M4" and rec.pending["recommendation"] == "go"
    assert dict(ran) == {"pricing": 1, "policies": 2} and rec.budget["P4_COMMERCIAL"]["repeat"] == 1
    assert kept(rid) == ["kept_pricing"]  # earlier phases had no rework, so nothing else was kept


def test_the_tables_name_real_steps_and_every_gate_check():
    keys = {s.key: s for st in orch.PHASE_STATES for s in orch.STEPS[st]} | {orch.SITE_STEP.key: orch.SITE_STEP}
    order = list(keys)
    for step, reads in orch.DEPENDS.items():
        for upstream in reads:  # a step reads only earlier steps of its own phase
            assert keys[step].phase == keys[upstream].phase and order.index(upstream) < order.index(step)
    for n, checks in gates.GATE_CRITERIA.items():
        for name, _ in checks:
            assert all(k in keys and keys[k].phase == n for k in orch.GATE_CHECK_STEPS[name]), name
    assert orch.GATE_CHECK_STEPS["problem sources"] == ("phase1_step1",)
    assert orch.GATE_CHECK_STEPS["someone already pays"] == ("phase1_step3",)
    outputs_by_phase = {n: {s.output for s in orch.STEPS[orch.PHASE_STATES[n - 1]]} for n in range(1, 7)}
    for n, modules in gates.CRITERIA_TEMPLATE.items():
        assert 2 <= len(modules) <= 4 and [m.id for m in modules] == [f"C{i}" for i in range(1, len(modules) + 1)]
        for m in modules:  # every module is about a file of its phase and can be measured by code
            assert m.file in outputs_by_phase[n] and m.kind in gates.CRITERION_KINDS and m.lo <= m.default <= m.hi
        assert gates.check_plan(gates.gate_criteria(n) + gates.template_criteria(n)) == []


# ---------------------------------------------------------------- 2 and 3. template plan, code checks

def test_fast_mode_makes_no_planner_and_no_checker_call_and_the_plan_review_still_lists_the_criteria(monkeypatch, strong_brief):
    monkeypatch.setenv("FAST_MODE", "true")
    ran = fakes(monkeypatch)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert rec.pending["recommendation"] == "go" and dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 1}
    assert counts["Plan"] == 0 and counts["Verification"] == 0 and sum(counts.values()) == 4  # the four makers only
    text = review(rid)
    for c in gates.gate_criteria(1) + gates.template_criteria(1):
        assert f"| {c['id']} | {c['check']} | {c['threshold']} | code |" in text
    assert "fast mode: the template's default criteria are used and no planner is called" in text
    assert "Model: none (written by code)" in text.split("\n")[0]
    log = entries(rid)
    plan = [e for e in log if e["agent"] == "planner" and e["action"] == "plan"]
    assert [(e["model"], e["outcome"]) for e in plan] == [(None, "template")]
    actions = [e["action"] for e in log]
    assert actions.index("phase1_plan_review") < actions.index("find_opportunities")  # written before any maker ran
    assert not any(e["agent"] == "checker" for e in log)
    rows = {r["id"]: r for r in rec.phase_data["verification"]}
    assert rows["C3"] == {"id": "C3", "result": "pass", "evidence": "code: 6 questions (need 5), no pitch word",
                          "step": "phase1_step4"}
    assert all(set(r) >= {"id", "result", "evidence"} for r in rows.values())


def test_the_plan_call_can_lower_a_threshold_and_add_an_extra_criterion(monkeypatch, strong_brief):
    ran = fakes(monkeypatch, guide=lambda call: "# Guide\n\n" + "".join(
        f"{i}. What happened the last time you missed a client deadline?\n" for i in range(1, 4)))
    reply = {"thresholds": [{"id": "C3", "value": 3}],
             "extra": [{"kind": "min_chars", "file": "phase1_step4_interview_guide.md", "value": 60},
                       {"kind": "has_sections", "file": "phase1_step3_market.md", "value": 2}]}  # repeats module C2: dropped
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts, plan_reply=json.dumps(reply))
    assert counts["Plan"] == 1 and counts["Verification"] == 0 and dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 1}
    criteria = {c["id"]: c for c in rec.phase_data["criteria"]}
    assert list(criteria) == ["G1", "G2", "G3", "G4", "C1", "C2", "C3", "C4"]
    assert criteria["C3"]["value"] == 3 and criteria["C3"]["threshold"] == "at least 3 questions, none with a pitch word"
    assert (criteria["C4"]["kind"], criteria["C4"]["step"], criteria["C4"]["value"]) == ("min_chars", "phase1_step4", 100)
    text = review(rid)
    assert "| C3 | the interview guide asks questions and none of them pitches the idea | at least 3 questions" in text
    assert "| C4 | phase1_step4_interview_guide.md has enough text | at least 100 characters | code |" in text
    assert rec.pending["recommendation"] == "go"  # three questions meet the lowered threshold


def test_an_invalid_plan_reply_falls_back_to_the_template_defaults(monkeypatch, strong_brief):
    ran = fakes(monkeypatch)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts, plan_reply="this is not JSON")
    assert counts["Plan"] == 2  # the model client retries an invalid reply once; then code takes the defaults
    assert rec.phase_data["criteria"] == gates.gate_criteria(1) + gates.template_criteria(1)
    assert rec.pending["recommendation"] == "go" and rec.budget["P1_DISCOVER"]["revise"] == 0
    assert "its reply was not valid, so the template's default criteria are used" in review(rid)
    assert dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 1} and counts["Verification"] == 0


def test_code_keeps_a_plan_reply_inside_the_template():
    files = ["phase1_step1_opportunities.md", "phase1_step4_interview_guide.md"]
    out = gates.template_criteria(1, {"C1": 99, "C3": 1, "C9": 5, "C2": "many"}, [
        {"kind": "essay_quality", "file": files[0], "value": 3},  # not a known kind
        {"kind": "min_chars", "file": "phase2_step1_lean_canvas.md", "value": 500},  # not a file of this phase
        {"kind": "required_fields", "file": files[0]},  # names no field
        {"kind": "questions_without_pitch", "file": files[1], "value": 4},  # repeats module C3
        {"kind": "min_count", "file": files[0], "value": 2, "field": "queries"},
        {"kind": "min_chars", "file": files[1], "value": 999999},
        {"kind": "has_price", "file": files[0]}], files + ["phase1_step3_market.md"])  # a third extra: over the limit
    assert [(c["id"], c["value"]) for c in out] == [("C1", 4), ("C2", 2), ("C3", 3), ("C4", 2), ("C5", 20000)]
    assert out[3]["field"] == "queries" and all(gates.code_checkable(c) and c["method"] == "code" for c in out)
    assert gates.template_criteria(1, "nonsense", "nonsense") == gates.template_criteria(1)
    schema = planner.plan_schema(1, files)
    assert schema.__name__ == "Plan" and schema.model_validate({}).model_dump() == {"thresholds": [], "extra": []}
    with pytest.raises(ValueError):  # the reply cannot carry free text, a third extra or an unknown id
        schema.model_validate({"extra": [{"kind": "min_chars", "file": files[0]}] * 3})
    with pytest.raises(ValueError):
        schema.model_validate({"thresholds": [{"id": "G1", "value": 1}]})
    assert planner.PLAN_MAX_TOKENS <= 300


@pytest.mark.parametrize("criterion, content, data, claims, met, measured", [
    (dict(kind="min_count", value=2, field="opportunities"), "", {"opportunities": [1]}, [], False, "1 opportunities (need 2)"),
    (dict(kind="min_count", value=2, field="questions"), "", {"questions": 6}, [], True, "6 questions (need 2)"),
    (dict(kind="min_count", value=1, field="sourced_claims"), "", {}, [{"label": "SOURCED"}, {"label": "ASSUMPTION"}], True,
     "1 sourced claims (need 1)"),
    (dict(kind="min_count", value=3, field=""), "- a\n- b\n1. c\ntext\n", {}, [], True, "3 list items (need 3)"),
    (dict(kind="required_fields", field="price_usd,charged_as"), "", {"price_usd": 9}, [], False, "missing: charged_as"),
    (dict(kind="required_fields", field="charged_as"), "", {"charged_as": "one-time payment"}, [], True, "present: charged_as"),
    (dict(kind="min_chars", value=10), "short", {}, [], False, "5 characters (need 10)"),
    (dict(kind="has_price", value=1), "**Recommended price: $9.00, one-time payment.**", {}, [], True, "1 price amounts found, first $9.00"),
    (dict(kind="has_price", value=1), "Price: Not set yet", {}, [], False, "0 price amounts found"),
    (dict(kind="has_sections", value=2), "# Title\n\n## One\n\n<h2>Two</h2>\n", {}, [], True, "2 sections (need 2)"),
    (dict(kind="questions_without_pitch", value=2), "1. When did you last do it?\n2. Would you buy our product?\n", {}, [], False,
     "2 questions (need 2), pitch words: our product, would you buy"),
])
def test_each_kind_of_criterion_is_measured_by_code(criterion, content, data, claims, met, measured):
    assert gates.check_criterion(criterion, content, data, claims) == (met, measured)


def test_a_criterion_code_cannot_measure_still_goes_to_the_checker_in_one_call(monkeypatch, strong_brief):
    ran = fakes(monkeypatch)
    legacy = [{"id": "C8", "check": "the guide reads well", "threshold": "yes", "method": "code"},
              {"id": "C9", "check": "the market file is clear", "threshold": "yes", "method": "code"}]
    monkeypatch.setattr(planner, "plan", lambda ctx: StepResult("plan", data={"criteria": gates.template_criteria(1) + legacy}))
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts, verify_rows=[
        {"id": "C8", "result": "pass", "evidence": "phase1_step4_interview_guide.md"},
        {"id": "C9", "result": "pass", "evidence": "invented"}])
    assert counts["Verification"] == 1 and counts["Plan"] == 0
    rows = {r["id"]: r for r in rec.phase_data["verification"]}
    assert rows["C8"]["result"] == "pass" and rows["C9"] == {"id": "C9", "result": "unverified", "evidence": "ASSUMPTION"}
    assert rows["C1"]["evidence"].startswith("code: ") and rec.pending["recommendation"] == "go"
    asked = next(e for e in entries(rid) if e["agent"] == "checker")
    assert asked["action"] == "verify" and asked["model"] == "MOCKED-llm"


# ---------------------------------------------------------------- 4. lenient quality checks

def test_a_lenient_quality_failure_is_a_note_for_the_founder_and_causes_no_rework(monkeypatch, strong_brief):
    monkeypatch.setenv("LENIENT_CHECKS", "true")
    ran = fakes(monkeypatch, guide=lambda call: BAD_GUIDE)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 1} and kept(rid) == []
    assert rec.budget["P1_DISCOVER"] == {"revise": 0, "defects": 0, "repeat": 0} and rec.pending["recommendation"] == "go"
    row = next(r for r in rec.phase_data["verification"] if r["id"] == "C3")
    assert row["result"] == "fail" and row["evidence"] == "code: 1 questions (need 5), no pitch word" and row["note"] is True
    text = decision(rid)
    assert "Decision: GO." in text and "open defects" not in text
    assert "## Quality notes for the founder" in text
    assert ("- C3: the interview guide asks questions and none of them pitches the idea (threshold: at least 5 questions, "
            "none with a pitch word). Measured: 1 questions (need 5), no pitch word") in text
    assert "| C3 | the interview guide asks questions and none of them pitches the idea |" in text and "| fail | code: 1 questions" in text
    # ... and it is shown at the milestone.
    assert "Quality notes for the founder: 1 quality check did not pass (C3)" in rec.pending["summary"]
    assert [x["id"] for x in rec.pending["quality_notes"]] == ["C3"]
    assert sum(counts.values()) == 5 and counts["Verification"] == 0  # the plan and four makers


def test_without_lenient_checks_the_same_failure_sends_only_that_step_back(monkeypatch, strong_brief):
    ran = fakes(monkeypatch, guide=lambda call: BAD_GUIDE)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 3}  # two defects back-edges, the guide only
    assert kept(rid) == ["kept_find_opportunities", "kept_score", "kept_market_research"] * 2
    assert rec.budget["P1_DISCOVER"]["defects"] == 2 and rec.pending["recommendation"] in ("pivot", "stop")
    text = decision(rid)
    assert "open defects after the defects budget: C3" in text and "Quality notes for the founder" not in text
    assert "quality_notes" in rec.pending and rec.pending["quality_notes"] == []


def test_a_file_that_was_not_written_still_causes_rework_with_lenient_checks(monkeypatch, strong_brief):
    monkeypatch.setenv("LENIENT_CHECKS", "true")
    ran = fakes(monkeypatch, guide=lambda call: ModelOutputInvalid("model_output_invalid: no guide") if call == 1 else GOOD_GUIDE)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert dict(ran) == {"find": 1, "score": 1, "market": 1, "guide": 2}
    assert kept(rid) == ["kept_find_opportunities", "kept_score", "kept_market_research"]
    assert rec.budget["P1_DISCOVER"]["defects"] == 1 and rec.pending["recommendation"] == "go"
    log = entries(rid)
    assert any(e["action"] == "interview_guide_not_written" and e["outcome"] == "model_output_invalid" for e in log)
    assert [e["outcome"] for e in log if e["action"] == "phase1_verification"] == ["defects", "verified"]
    assert (outputs(rid) / "phase1_step4_interview_guide.md").exists() and "Quality notes" not in decision(rid)


def test_lenient_checks_follow_fast_mode_unless_set(monkeypatch):
    monkeypatch.delenv("LENIENT_CHECKS", raising=False)
    monkeypatch.delenv("FOUNDER_EVIDENCE_FIRST", raising=False)
    assert settings.load().lenient_checks is False and settings.load().founder_evidence_first is True
    monkeypatch.setenv("FAST_MODE", "true")
    assert settings.load().lenient_checks is True
    monkeypatch.setenv("LENIENT_CHECKS", "false")
    assert settings.load().lenient_checks is False
    monkeypatch.setenv("FAST_MODE", "false")
    monkeypatch.setenv("LENIENT_CHECKS", "true")
    monkeypatch.setenv("FOUNDER_EVIDENCE_FIRST", "false")
    assert settings.load().lenient_checks is True and settings.load().founder_evidence_first is False


# ---------------------------------------------------------------- 5. founder evidence comes first

PROBLEM_LINK, PAY_LINK, TRACTION_LINK = ("https://shop.example/translators", "https://paid9.example/pricing",
                                         "https://shop2.example/waitlist")  # the test transport serves a page about a tracker template
UNRELATED_LINK = "https://site9.example/thread"  # "people discuss the problem here": no content word of the brief


def weak_m1(monkeypatch, strong_brief):
    """One problem source and no paying source: the gate repeats once, then recommends STOP at M1."""
    ran = fakes(monkeypatch, problem=1, paying=0)
    counts = Counter()
    rid, reg, llm, rec = to_m1(strong_brief, counts)
    assert rec.pending["recommendation"] == "stop" and "request_changes" in rec.pending["choices"]
    assert "| problem sources | MISS |" in decision(rid) and "| someone already pays | not measured |" in decision(rid)
    return rid, reg, llm, rec, ran, counts


def signoff(rid, reg, n=1):
    ctx = orch.RunContext(orch.load(rid), settings.load(), registry=reg)
    gate = (orch._signoff_p1 if n == 1 else orch._signoff_p2)(ctx, 1)
    return gate, {c.name: c for c in gate.checks}


def test_one_fetched_related_founder_link_settles_the_problem_and_the_pay_check(monkeypatch, strong_brief):
    rid, reg, llm, rec, ran, counts = weak_m1(monkeypatch, strong_brief)
    before, calls_before = dict(ran), sum(counts.values())
    item = orch.add_evidence(rid, "problem", url=PROBLEM_LINK, registry=reg)
    assert item["status"] == "fetched" and item["related"] is True and "template" in item["related_words"]
    orch.add_evidence(rid, "pay", url=PAY_LINK, registry=reg)
    store(rid).choose(rec.pending["action_id"], "request_changes")  # no note: the phase is checked again with the evidence
    rec = orch.advance(rid, llm=llm, registry=reg)
    assert rec.pending["milestone"] == "M1" and rec.pending["recommendation"] == "go"
    assert dict(ran) == before and sum(counts.values()) == calls_before  # no maker ran again and no model was called
    assert kept(rid)[-4:] == ["kept_find_opportunities", "kept_score", "kept_market_research", "kept_interview_guide"]
    text = decision(rid)
    assert f"| problem sources | pass | {FOUNDER_DETAIL.format(PROBLEM_LINK)} |" in text
    assert f"| someone already pays | pass | {FOUNDER_DETAIL.format(PAY_LINK)} |" in text
    line = orch.strip_header(text).split("\n")[0]
    assert line.startswith("Decision: GO. Reasons: problem sources, someone already pays met by founder-provided evidence "
                           "(the founder's link, not evidence the system found)")
    rows = {r["id"]: r for r in rec.phase_data["verification"]}  # the verification report says so too
    assert rows["G1"] == {"id": "G1", "result": "pass", "code": True, "evidence": "code: " + FOUNDER_DETAIL.format(PROBLEM_LINK)}
    log = [e for e in entries(rid) if e["action"].startswith("founder_evidence_")]
    assert [(e["agent"], e["action"], e["sources"], e["outcome"], e["approved_by"]) for e in log] == [
        ("gate", "founder_evidence_problem", [PROBLEM_LINK], "met", None),
        ("gate", "founder_evidence_pay", [PAY_LINK], "met", None)]
    assert "## Evidence the founder provided" in text and f"Source: {PROBLEM_LINK}" in text


def test_a_traction_link_settles_the_pay_check_and_the_phase_2_demand_check(monkeypatch, strong_brief):
    rid, reg, llm, rec, ran, counts = weak_m1(monkeypatch, strong_brief)
    item = orch.add_evidence(rid, "traction", url=TRACTION_LINK, statement="120 people joined the waiting list", registry=reg)
    assert item["status"] == "fetched" and item["kind"] == "traction" and item["related"] is True
    gate, checks = signoff(rid, reg)
    assert checks["someone already pays"].passed and checks["someone already pays"].detail == FOUNDER_DETAIL.format(TRACTION_LINK)
    assert not checks["problem sources"].passed and not checks["problem sources"].founder_url  # a traction link is not problem evidence
    gate2, checks2 = signoff(rid, reg, n=2)
    demand = checks2["demand target"]
    assert demand.passed and demand.result == "pass" and demand.detail == FOUNDER_DETAIL.format(TRACTION_LINK)
    assert "demand target met by founder-provided evidence" in gates.decision_line(gate2.outcome, gate2.reasons)
    assert not any("MOCKED" in r for r in gate2.reasons)
    assert "traction" in orch.EVIDENCE_KINDS and set(gates.FOUNDER_KINDS["demand target"]) == {"demand", "traction"}


def test_an_unrelated_link_or_a_statement_settles_nothing(monkeypatch, strong_brief):
    rid, reg, llm, rec, ran, counts = weak_m1(monkeypatch, strong_brief)
    before = decision(rid)
    unrelated = orch.add_evidence(rid, "problem", url=UNRELATED_LINK, registry=reg)
    assert unrelated["status"] == "fetched" and unrelated["related"] is False and unrelated["related_words"] == []
    orch.add_evidence(rid, "pay", url=UNRELATED_LINK, registry=reg)
    orch.add_evidence(rid, "traction", statement="Translators told me they would pay for this template", registry=reg)
    orch.add_evidence(rid, "pay", url="ftp://shop.example/pricing", registry=reg)  # not fetched
    gate, checks = signoff(rid, reg)
    assert not any(c.founder_url for c in gate.checks) and "founder-provided" not in " ".join(gate.reasons)
    assert not checks["someone already pays"].measured and checks["someone already pays"].detail == gates.PAY_UNKNOWN
    assert checks["problem sources"].result == "MISS"
    gate2, checks2 = signoff(rid, reg, n=2)
    assert "demand target" not in checks2 and any("demand is NOT validated" in r for r in gate2.reasons)
    store(rid).choose(rec.pending["action_id"], "request_changes")
    rec = orch.advance(rid, llm=llm, registry=reg)
    assert rec.pending["recommendation"] == "stop"  # a weak idea still gets a STOP recommendation
    assert "met by founder-provided evidence" not in decision(rid) and "Decision: STOP." in decision(rid)
    assert not any(e["action"].startswith("founder_evidence_") for e in entries(rid))
    assert before.split("\n")[2].startswith("Decision: STOP.")


def test_with_the_setting_off_a_founder_link_counts_only_by_the_ordinary_rules(monkeypatch, strong_brief):
    monkeypatch.setenv("FOUNDER_EVIDENCE_FIRST", "false")
    rid, reg, llm, rec, ran, counts = weak_m1(monkeypatch, strong_brief)
    orch.add_evidence(rid, "problem", url=PROBLEM_LINK, registry=reg)
    orch.add_evidence(rid, "pay", url=PAY_LINK, registry=reg)
    orch.add_evidence(rid, "traction", url=TRACTION_LINK, registry=reg)
    gate, checks = signoff(rid, reg)
    assert not any(c.founder_url for c in gate.checks) and "founder-provided" not in " ".join(gate.reasons)
    # One more fetched page is one more source: two sources from two domains still miss the threshold of three.
    assert checks["problem sources"].result == "MISS" and checks["problem sources"].detail.startswith("2 fetched sources from 2 domains")
    # A page with a price counts as payment evidence, as it did before the setting existed.
    assert checks["someone already pays"].passed and checks["someone already pays"].detail.startswith("2 fetched PAY sources")
    gate2, checks2 = signoff(rid, reg, n=2)
    assert "demand target" not in checks2 and any("demand is NOT validated" in r for r in gate2.reasons)
    assert not any(e["action"].startswith("founder_evidence_") for e in entries(rid))


def test_with_no_founder_link_the_gates_are_as_they_were():
    ev = gates.Phase1Evidence(problem_source_urls=["https://a.example/1"], paying_source_urls=[], budget_usd=50, hours_per_week=10,
                              mvp_cost_usd=20, mvp_hours_per_week=5, total_claims=10, assumption_claims=6)
    weak = gates.phase1_gate(ev, 1)
    assert weak.outcome is gates.Outcome.STOP and any("two or more" in r for r in weak.reasons)
    ev.founder_settled = {"problem sources": "https://f.example/x"}
    one = gates.phase1_gate(ev, 1)  # the founder's link settles its own check and no other
    assert one.outcome is gates.Outcome.REPEAT and one.reasons[0].startswith("problem sources met by founder-provided evidence")
    assert [c.name for c in one.checks if c.measured and not c.passed] == ["claims mostly sourced"]
    assert gates.phase2_gate(1, 1, True, "not_measured", "0 of 200", 1).reasons[0] == "canvas and landing page checks passed"


@pytest.mark.parametrize("page, brief, related", [
    ("A tracker TEMPLATE for busy people", "A Notion template pack for translators", True),
    ("One translator explains her week", "Freelance translators working with agencies", True),  # plural s
    ("people discuss the problem here", "A product that helps people with this problem", False),  # stop words only
    ("the cat sat on a mat", "a cat toy", False),  # words under four letters do not count
    ("", "A Notion template pack", False),
])
def test_loosely_related_is_one_shared_content_word(page, brief, related):
    assert gates.loosely_related(page, brief) is related

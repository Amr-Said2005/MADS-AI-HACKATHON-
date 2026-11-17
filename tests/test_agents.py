"""The built agents (assessor, planner, checker, researcher, scorer, interviewer), offline.

A scripted responder stands in for the model (LLM_MODE=mock, so the log says MOCKED-llm and the run
can never be promoted), search is faked, and pages come from an httpx.MockTransport.
"""

import json

import pytest

import agents
import orchestrator as orch
import settings
from agents import interviewer
from decision_log import DecisionLog
from llm import LLMClient, ModelOutputInvalid
from orchestrator import RunStatus
from sources import SearchHit
from test_pipeline import outputs, registry, store

ASSESSMENT = {"customer": "freelance translators", "problem": "losing track of client deadlines and word counts",
              "physical_shipped": "no", "family": "Templates and tools",
              "needed_skills": [{"name": "Notion", "kind": "core", "covered_by": "Notion"},
                                {"name": "writing", "kind": "core", "covered_by": "writing"}], "mvp_cost_usd": 20,
              "effort_hours": 30, "values_check": "nothing in the idea breaks a stated value", "conflicting_value": "none",
              "narrowed_idea": "A Notion tracker for EN-DE legal translators",
              "search_terms": ["translator deadline tracking", "freelance translator client tracker"],
              "evidence": [{"page": 1, "shows": "a translator asks how others track deadlines"}]}
ROADMAP = {"milestones": [{"phase": i, "title": f"Milestone for phase {i}", "founder_sees": "the files and the gate's reasons",
                           "decision_asked": "continue, request changes, pivot or stop"} for i in range(1, 7)]}
PLAN = {"criteria": [{"check": "fetched SAY sources describe the problem", "threshold": "at least 3 from 3 domains", "method": "source"},
                     {"check": "a fetched PAY source shows a paid comparable", "threshold": "at least 1", "method": "source"},
                     {"check": "the interview guide asks about past behaviour", "threshold": "all questions", "method": "code"}]}
OPPORTUNITIES = {
    "evidence": [{"page": 1, "text": "a translator describes juggling deadlines from several agencies"},
                 {"page": 2, "text": "a forum thread asks how to track word counts per client"},
                 {"page": 3, "text": "a blog post lists spreadsheet workarounds translators use"},
                 {"page": 9, "text": "translators dislike invoicing"}],
    "opportunities": [
        {"title": "Deadline tracker for translators", "customer": "freelance translators working with agencies",
         "problem": "missed deadlines across agencies", "pages": [1, 2, 9],
         "assumptions": ["most translators use no dedicated tool"]},
        {"title": "Word count log", "customer": "translators with direct clients", "problem": "word counts per client",
         "pages": [], "assumptions": []}]}
SCORING = {"scores": [{"id": "O1", "pain": 4, "willingness_to_pay": 3, "reach": 4, "founder_fit": 5, "reason": "three sourced claims"},
                      {"id": "O2", "pain": 2, "willingness_to_pay": 2, "reach": 3, "founder_fit": 4, "reason": "no sourced claim"},
                      {"id": "O7", "pain": 5, "willingness_to_pay": 5, "reach": 5, "founder_fit": 5, "reason": "not a real id"}],
           "smallest_version": "one Notion page with a client and deadline table", "mvp_cost_usd": 10, "mvp_hours_per_week": 4}
MARKET = {"claims": [{"page": 1, "kind": "PAY", "text": "a paid tracker template is sold for 19 dollars"},
                     {"page": 2, "kind": "market", "text": "a general project tool is used by some translators"}],
          "gaps": ["existing templates do not track word counts"]}
GUIDE = {"opening": "Thanks for your time; I am trying to learn how translators manage their work.",
         "questions": [{"question": f"Tell me about the last time you handled case {i} with a client deadline.",
                        "learn": "how often the problem happens"} for i in range(1, 7)],
         "do_not": ["Do not describe the product", "Do not ask about the future"]}


def verification(ctx_urls):
    return {"rows": [{"id": "C1", "result": "pass", "evidence": ctx_urls[0] if ctx_urls else "ASSUMPTION"},
                     {"id": "C2", "result": "pass", "evidence": "phase1_step3_market.md"},
                     {"id": "C3", "result": "pass", "evidence": "something invented"}]}


def scripted(overrides=None, calls=None):
    replies = {"Assessment": ASSESSMENT, "Roadmap": ROADMAP, "Plan": PLAN, "Opportunities": OPPORTUNITIES,
               "Scoring": SCORING, "Market": MARKET, "Guide": GUIDE}
    replies.update(overrides or {})

    def respond(messages, agent, schema):
        if calls is not None:
            calls.append((agent, schema.__name__, messages))
        if schema.__name__ == "Verification":
            urls = [l[2:] for l in messages[-1]["content"].splitlines() if l.startswith("- http")]
            return json.dumps(verification(urls))
        reply = replies[schema.__name__]
        return json.dumps(reply(messages) if callable(reply) else reply)
    return respond


def fake_search(query, *, limit=5, searxng_url=None):
    paid = any(w in query for w in ("pricing", "paid", "buy", "price", "subscription"))
    stem = "shop" if paid else "forum"
    return [SearchHit(f"https://{stem}{i}.example/page", f"{stem} {i}", "snippet", "test") for i in range(limit)]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(agents, "web_search", fake_search)


def run(brief, responder, choose=lambda rec: rec.pending["choices"][0], limit=12):
    llm = LLMClient(settings.load(), mock_responder=responder)
    reg = registry()
    rid = orch.new_run(brief).run_id
    seen = []
    for _ in range(limit):
        rec = orch.advance(rid, llm=llm, registry=reg)
        if rec.status != RunStatus.AWAITING_APPROVAL.value:
            break
        seen.append((rec.pending.get("milestone") or rec.pending.get("gate"), rec.pending.get("recommendation")))
        choice = choose(rec)
        if choice is None:
            break
        store(rid).choose(rec.pending["action_id"], choice)
    return orch.load(rid), seen


def stop_at_m1(rec):
    return None if rec.pending.get("milestone") == "M1" else rec.pending["choices"][0]


def test_phase1_slice_with_the_real_agents_reaches_a_go_recommendation(web, strong_brief):
    calls = []
    rec, seen = run(strong_brief, scripted(calls=calls), stop_at_m1)
    assert seen == [("feasibility", None), ("roadmap", None), ("M1", "go")], rec.stop_reasons or rec.blocked_reason
    out = outputs(rec.run_id)
    opp = (out / "phase1_step1_opportunities.md").read_text(encoding="utf-8")
    assert ". Source: https://forum0.example/page" in opp and "most translators use no dedicated tool. ASSUMPTION" in opp
    assert "translators dislike invoicing. ASSUMPTION" in opp  # page 9 does not exist, so no citation
    assert "- Supported by: https://forum0.example/page, https://forum1.example/page\n" in opp
    scoring = json.loads((out / "phase1_step2_scoring.json").read_text(encoding="utf-8"))
    assert scoring["pick"] == "O1" and [s["id"] for s in scoring["scores"]] == ["O1", "O2"]  # unknown id dropped
    assert scoring["mvp_cost_usd"]["basis"] == "ASSUMPTION"
    market = (out / "phase1_step3_market.md").read_text(encoding="utf-8")
    assert "[PAY]" in market and "shop0.example" in market
    guide = (out / "phase1_step4_interview_guide.md").read_text(encoding="utf-8")
    assert "No interviews have been run" in guide
    decision = (out / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "| C3 |" in decision and "unverified | ASSUMPTION" in decision  # invented evidence is not accepted
    assert "Decision: GO." in decision
    feas = (out / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    assert "Decision: GO. Reasons: the founder chose to continue with 0 major feasibility warnings." in feas
    assert "forum0.example" in feas
    roadmap = (out / "phase1_gate_roadmap.md").read_text(encoding="utf-8")
    assert "phase3_gate_tests.md" in roadmap  # deliverables are set by code, not by the model
    assert [c[1] for c in calls] == ["Assessment", "Roadmap", "Plan", "Opportunities", "Scoring", "Market", "Guide", "Verification"]
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert all(e["model"] in (None, "MOCKED-llm") for e in entries)
    with pytest.raises(orch.PromoteError):
        orch.promote(rec.run_id)


def test_fetched_pages_are_fenced_and_never_in_the_system_prompt(web, strong_brief):
    calls = []
    run(strong_brief, scripted(calls=calls), stop_at_m1)
    agent, schema, messages = next(c for c in calls if c[1] == "Opportunities")
    system, user = messages[1]["content"], messages[-1]["content"]  # messages[0] is the schema instruction
    assert "people discuss the problem here" in user and "untrusted" in user.lower() and "[Page 1]" in user
    assert "people discuss the problem here" not in system and "Task: find_opportunities" in system


def test_the_gate_counts_only_the_chosen_opportunitys_claims(web, strong_brief):
    many = json.loads(json.dumps(OPPORTUNITIES))
    many["evidence"] = many["evidence"][:3]
    many["opportunities"][0]["assumptions"] = []
    for k in range(3):  # three more opportunities resting on assumptions only
        many["opportunities"].append({"title": f"Other idea {k}", "customer": "some translators", "problem": "something else",
                                      "pages": [], "assumptions": [f"unsupported statement {k}a", f"unsupported statement {k}b"]})
    many["opportunities"] = many["opportunities"][:1] + many["opportunities"][2:]
    rec, seen = run(strong_brief, scripted({"Opportunities": many}), stop_at_m1)
    assert seen[-1] == ("M1", "go")  # the other ideas' six assumptions do not count against O1


def test_weak_evidence_recommends_stop(web, strong_brief, monkeypatch):
    monkeypatch.setattr(agents, "web_search", lambda q, **k: fake_search(q, limit=1))  # one SAY domain, one PAY
    thin = {"evidence": [{"page": 1, "text": "one translator mentions deadlines"}],
            "opportunities": [{"title": "Deadline tracker for translators", "customer": "freelance translators",
                               "problem": "missed deadlines", "pages": [1],
                               "assumptions": ["many more have the problem", "they want a template"]},
                              {"title": "Invoice helper", "customer": "freelance translators", "problem": "slow invoicing",
                               "pages": [], "assumptions": ["translators dislike invoicing"]}]}
    rec, seen = run(strong_brief, scripted({"Opportunities": thin}), stop_at_m1)
    assert seen[-1] == ("M1", "stop")
    decision = (outputs(rec.run_id) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "Decision: STOP." in decision and "two or more thresholds missed" in decision


def test_weak_brief_stops_at_feasibility_with_the_real_assessor(web):
    weak = json.loads((settings.BRIEFS_DIR / "example_weak.json").read_text(encoding="utf-8"))
    reply = dict(ASSESSMENT, customer="collectors of 1950s typewriters", problem="choosing the right ribbon",
                 family="Written content", mvp_cost_usd=15, effort_hours=25,
                 needed_skills=[{"name": "writing", "kind": "core", "covered_by": "none"},
                                {"name": "typewriter repair", "kind": "core", "covered_by": "none"}])
    rec, seen = run(weak, scripted({"Assessment": reply}), lambda rec: "stop")
    assert rec.status == RunStatus.STOPPED.value and seen == [("feasibility", None)]  # the founder stopped it, not the check
    text = (outputs(rec.run_id) / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    assert "Decision: STOP. Reasons: the founder stopped the run" in text
    assert "| W-TIME | major |" in text and "| W-BUDGET | major |" in text and "| W-SKILLS | major |" in text


def test_interviewer_rejects_hypothetical_questions(web, strong_brief):
    with pytest.raises(ValueError, match="hypothetical"):
        interviewer.Question(question="Would you buy a tracker for this?", learn="nothing reliable")
    tries = []

    def guide(messages):
        tries.append(1)
        bad = json.loads(json.dumps(GUIDE))
        if len(tries) == 1:
            bad["questions"][0]["question"] = "How likely are you to pay for a template?"
        return bad

    rec, seen = run(strong_brief, scripted({"Guide": guide}), stop_at_m1)
    assert len(tries) == 2 and seen[-1][0] == "M1"  # retried once with the validation error
    assert "How likely" not in (outputs(rec.run_id) / "phase1_step4_interview_guide.md").read_text(encoding="utf-8")


def test_roadmap_that_promises_sales_is_regenerated_then_stops(web, strong_brief):
    bad = json.loads(json.dumps(ROADMAP))
    bad["milestones"][4]["title"] = "Reach 100 sales in the first week"
    rec, seen = run(strong_brief, scripted({"Roadmap": bad}))
    assert rec.status == RunStatus.STOPPED.value and seen == [("feasibility", None)]
    assert any("promises income, sales or sign-up numbers" in r for r in rec.stop_reasons)


def test_mock_mode_without_a_responder_never_invents_content(strong_brief):
    rec = orch.advance(orch.new_run(strong_brief).run_id)  # search is patched off in conftest
    # no model, so the assessor's output is invalid: the run pauses at feasibility, where only the founder ends it
    assert rec.status == RunStatus.BLOCKED.value and rec.state == "FEASIBILITY" and "model_output_invalid" in rec.blocked_reason
    assert not (outputs(rec.run_id) / "phase1_gate_feasibility.md").exists()


def test_scorer_needs_a_known_opportunity_id(web, strong_brief):
    bad = dict(SCORING, scores=[SCORING["scores"][2]])
    rec, seen = run(strong_brief, scripted({"Scoring": bad}), stop_at_m1)
    assert not (outputs(rec.run_id) / "phase1_step2_scoring.json").exists()
    assert seen[-1][0] == "M1" and seen[-1][1] in ("pivot", "stop")


def test_keywords_and_task_prompt():
    assert agents.keywords("A Notion template pack that helps freelance translators track clients", 4) == \
        ["notion", "freelance", "translators", "track"]

    class Ctx:
        def prompt(self, agent):
            return (settings.PROMPTS_DIR / f"{agent}.md").read_text(encoding="utf-8")

    p = agents.task_prompt(Ctx(), "planner", "roadmap")
    assert "Task: roadmap" in p and "Task: plan" not in p and "architect role" in p


def test_values_conflict_must_be_one_of_the_founders_values(web, strong_brief):
    from agents import assessor
    schema = assessor.assessment_schema(strong_brief["values"])
    with pytest.raises(ValueError):
        schema.model_validate(dict(ASSESSMENT, conflicting_value="privacy - no conflict, it uses free tools"))
    rec, seen = run(strong_brief, scripted({"Assessment": dict(ASSESSMENT, conflicting_value="privacy")}), lambda rec: None)
    assert rec.state == "FEASIBILITY" and rec.status == RunStatus.AWAITING_APPROVAL.value  # a warning, and the founder decides
    assert rec.pending["major_ids"] == ["W-VALUES"] and rec.pending["advice"] == "Proceed with care, or revise"
    assert 'the stated value "privacy"' in rec.pending["warnings"][0]["fact"]


def test_makers_are_shown_the_approved_plan(web, strong_brief):
    calls = []
    run(strong_brief, scripted(calls=calls), stop_at_m1)
    by_schema = {c[1]: c[2][-1]["content"] for c in calls}
    assert "Pass criteria for this phase" in by_schema["Opportunities"]
    assert "problem sources Threshold: at least 3 fetched SAY sources from 3 different domains" in by_schema["Opportunities"]
    assert "the interview guide asks about past behaviour" in by_schema["Guide"]
    assert "Pass criteria for this phase" not in by_schema["Plan"] and "Pass criteria for this phase" not in by_schema["Assessment"]


def test_a_pay_claim_on_a_page_without_a_price_does_not_pass_the_gate(web, strong_brief, monkeypatch):
    # The model tags a forum page PAY. Code finds no price there, so it is not payment evidence.
    monkeypatch.setattr(agents, "web_search", lambda q, **k: [SearchHit(f"https://forum{i}.example/page", "t", "s", "test")
                                                                for i in range(k.get("limit", 5))])
    rec, seen = run(strong_brief, scripted(), stop_at_m1)
    assert seen[-1] == ("M1", "stop")  # one miss, a repeat, then still missed
    out = outputs(rec.run_id)
    market = (out / "phase1_step3_market.md").read_text(encoding="utf-8")
    assert "code found no price on the page" in market and "[PAY]" not in market
    decision = (out / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "someone already pays | MISS | 0 fetched PAY sources whose text contains a price found by code" in decision
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert any(e["action"] == "pay_tag_downgraded" for e in entries)


def test_a_confirmed_pay_claim_prints_the_price_code_found(web, strong_brief):
    rec, _ = run(strong_brief, scripted(), stop_at_m1)
    market = (outputs(rec.run_id) / "phase1_step3_market.md").read_text(encoding="utf-8")
    assert "[PAY] a paid tracker template is sold for 19 dollars" in market and "Price found on the page by code: $19." in market


# ---------------------------------------------------------------- scoring: nothing is scored without evidence

def test_no_scores_and_no_pick_when_no_evidence_was_collected(web, strong_brief):
    empty = {"evidence": [], "opportunities": [
        {"title": "Translators working with agencies", "customer": "agency translators", "problem": "tracking deadlines",
         "pages": [], "assumptions": ["they track deadlines by hand"]},
        {"title": "Translators with direct clients", "customer": "direct-client translators", "problem": "tracking word counts",
         "pages": [], "assumptions": ["they invoice by word count"]}]}
    calls = []
    rec, seen = run(strong_brief, scripted({"Opportunities": empty, "Market": {"claims": [], "gaps": ["nothing found"]}}, calls=calls),
                    stop_at_m1)
    scoring = json.loads((outputs(rec.run_id) / "phase1_step2_scoring.json").read_text(encoding="utf-8"))
    assert scoring["pick"] is None and "No pick was made" in scoring["result"]
    for row in scoring["scores"]:
        assert row["pain"] == row["willingness_to_pay"] == row["reach"] == "not scored" and row["title"]
        assert isinstance(row["founder_fit"], int)  # from the brief, not from the web
    assert "Scoring" not in [c[1] for c in calls]  # the model was not asked to score
    assert seen[-1][0] == "M1" and seen[-1][1] == "stop"  # after one repeat with new searches
    decision = (outputs(rec.run_id) / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "Decision: STOP. Reasons: no_evidence_collected" in decision and "not a verdict on the idea" in decision
    assert "0 of 0" not in decision and "weak" not in decision.split("## Verification report")[0]


def test_founder_fit_comes_from_the_brief_and_a_tie_keeps_the_founders_idea(web, strong_brief):
    tied = {"scores": [{"id": "O1", "pain": 4, "willingness_to_pay": 3, "reach": 4, "founder_fit": 1, "reason": "sourced"},
                       {"id": "O2", "pain": 3, "willingness_to_pay": 4, "reach": 4, "founder_fit": 1, "reason": "sourced"}],
            "smallest_version": "one Notion page with a client and deadline table", "mvp_cost_usd": 10, "mvp_hours_per_week": 4}
    rec, _ = run(strong_brief, scripted({"Scoring": tied}), stop_at_m1)
    scoring = json.loads((outputs(rec.run_id) / "phase1_step2_scoring.json").read_text(encoding="utf-8"))
    assert [r["founder_fit"] for r in scoring["scores"]] == [5, 5]  # the founder has both needed skills; the model's 1 is ignored
    assert scoring["pick"] is None and scoring["tie"] == ["O1", "O2"] and scoring["result"].startswith("Tie at 16 out of 20")
    assert "the founder's own idea is kept" in scoring["result"]


# ---------------------------------------------------------------- market research: roles, built queries, checked competitors

def test_reference_and_community_pages_cannot_be_payment_evidence(tmp_path):
    import httpx
    import sources as src
    from sources import Claim

    page = "<html><body><p>The template costs $19 and translators discuss deadlines here.</p></body></html>"
    reg = src.SourceRegistry(max_sources=5, resolver=lambda host: ["93.184.216.34"],
                             transport=httpx.MockTransport(lambda req: httpx.Response(200, html=page)))
    reg.log = DecisionLog(tmp_path / "log.json")
    urls = ["https://en.wikipedia.org/wiki/Translation", "https://news.ycombinator.com/item?id=1", "https://shop.example/tracker"]
    for u in urls:
        reg.fetch(u)
    stamped = reg.stamp_claims([Claim("x", urls[0], "SAY"), Claim("x", urls[0], "PAY"), Claim("x", urls[1], "SAY"),
                                Claim("x", urls[1], "PAY"), Claim("x", urls[2], "PAY")])
    assert [c.kind for c in stamped] == ["background", "background", "SAY", "market", "PAY"]
    assert [e["action"] for e in reg.log.entries()] == ["pay_tag_downgraded", "reference_page_not_evidence"]


def test_queries_are_built_from_the_brief_and_competitors_are_checked(web, strong_brief):
    market = {"claims": [{"page": 1, "kind": "PAY", "text": "a paid tracker template is sold for 19 dollars"}],
              "competitors": [
                  {"name": "Tracker Shop", "page": 1, "kind": "direct", "what_they_offer": "a paid tracker template",
                   "quote": "a paid tracker template is sold"},
                  {"name": "World Wide Web", "page": 1, "kind": "direct", "what_they_offer": "everything", "quote": "a paid tracker template is sold"},
                  {"name": "Invented Co", "page": 1, "kind": "direct", "what_they_offer": "a tracker", "quote": "words that are not on the page at all"}],
              "gaps": []}
    calls = []
    rec, _ = run(strong_brief, scripted({"Market": market}, calls=calls), stop_at_m1)
    text = (outputs(rec.run_id) / "phase1_step3_market.md").read_text(encoding="utf-8")
    section = text.split("## Competitors")[1].split("## ")[0]
    assert "World Wide Web" not in section and "Invented Co" not in section
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    dropped = [e["reason"] for e in entries if e["action"] == "competitor_dropped"]
    assert any(r.startswith("World Wide Web: generic name") for r in dropped) and any("quote not found" in r for r in dropped)
    queries = rec.artifacts["phase1_step3"]["data"]["queries"] + rec.artifacts["phase1_step1"]["data"]["queries"]
    assert any("freelance translators" in q for q in queries) and any("price" in q for q in queries)
    assert any(q.endswith(("Udemy", "Coursera")) for q in queries)  # a marketplace query from the fixed list


def test_no_surviving_competitor_is_said_plainly(web, strong_brief):
    import market_research

    rec, _ = run(strong_brief, scripted(), stop_at_m1)  # the scripted market reply names no competitor
    text = (outputs(rec.run_id) / "phase1_step3_market.md").read_text(encoding="utf-8")
    assert market_research.NO_COMPETITOR_LINE in text.split("## Competitors")[1]

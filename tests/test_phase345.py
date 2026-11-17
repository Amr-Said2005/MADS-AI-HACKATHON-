"""Phases 3 to 5 (builder, commercial, launcher) with the real agents, offline."""

import json

import orchestrator as orch
import settings
from agents import builder
from decision_log import DecisionLog
from llm import LLMClient
from orchestrator import RunStatus
from test_agents import scripted, web  # noqa: F401  (web is a fixture)
from test_phase2 import PHASE2
from test_pipeline import outputs, registry, store

TITLES = ["Client table", "Deadline calendar", "Word count log"]
SPEC = {"product_name": "Ignored name", "format": "a Markdown pack with three tables to copy into Notion",
        "sections": [{"title": t, "purpose": f"gives the translator a {t.lower()}"} for t in TITLES],
        "out_of_scope": ["invoicing"]}
BODY = ("Copy this table into a new Notion page. Each row is one client. Fill in the name, the agency, the language "
        "pair and the usual turnaround. Review it every Monday and archive clients you no longer work with. "
        "| Client | Agency | Language pair | Turnaround |\n|---|---|---|---|\n| Example GmbH | Direct | EN-DE | 3 days |")
PRODUCT = {"sections": [{"title": t, "body": BODY} for t in TITLES]}
DOCS = {"quick_start": ["Open the file", "Copy the first table into Notion", "Add your current clients"],
        "faq": [{"question": "Do I need a paid Notion plan?", "answer": "No, the free plan is enough."},
                {"question": "Can I change the columns?", "answer": "Yes, they are plain tables."}],
        "limits": ["It does not send reminders"]}
PRICING = {"price_usd": 9, "charged_as": "one-time payment",
           "reasons": [{"text": "a comparable tracker template is sold for 19 dollars", "evidence": 1},
                       {"text": "a first version should cost less than established tools", "evidence": None}],
           "assumptions": ["translators buy templates at this price"]}
POLICIES = {"refund": "You can ask for a refund within 14 days of buying. Write to the founder and the founder will reply.",
            "privacy": "The founder collects your email address at purchase to send the product. It is not sold or shared.",
            "terms": "You may use the product for your own work. You may not resell it. It is provided as it is."}
STORE = {"tagline": "A Notion board for translators who work with several agencies.",
         "description": "Three tables that keep client details, deadlines and word counts on one page. You copy them into Notion.",
         "highlights": ["See every deadline in one list", "Log word counts per client", "Plan the week's workload"]}
POST = ("I am a translator and I made a small Notion pack for keeping client deadlines and word counts in one place. "
        "It has three tables you copy into your own workspace. I would value honest feedback on what is missing.")
POSTS = {"posts": [{"channel": "A translators' forum", "text": POST}, {"channel": "A Notion community", "text": POST}]}
FAQ = {"faq": [{"question": f"Question {i} about the board?", "answer": "It is a set of three tables you copy into Notion."}
               for i in range(1, 5)],
       "ask_the_founder": ["Refund requests", "Payment problems"]}
MEMO = {"riskiest_assumptions": ["translators buy templates at this price", "translator forums allow such posts"],
        "measure_next": ["The landing page sign-up rate from 200 cold visitors", "Replies to the launch posts"]}
ALL = dict(PHASE2, Spec=SPEC, Product=PRODUCT, Docs=DOCS, Pricing=PRICING, Policies=POLICIES, Store=STORE, Posts=POSTS,
           Faq=FAQ, Memo=MEMO)


def drive(brief, replies, choose=lambda rec: rec.pending["choices"][0], approve=lambda key: True, limit=40, calls=None):
    """Advance and answer every founder question; decide each outbound action on its own."""
    llm = LLMClient(settings.load(), mock_responder=scripted(replies, calls=calls))
    reg = registry()
    rid = orch.new_run(brief).run_id
    seen = []
    for _ in range(limit):
        rec = orch.advance(rid, llm=llm, registry=reg)
        if rec.status != RunStatus.AWAITING_APPROVAL.value:
            break
        st, p = store(rid), rec.pending
        if st.choice(p["action_id"]) is None:
            choice = choose(rec)
            if choice is None:
                break
            seen.append((p.get("milestone") or p.get("gate"), p.get("recommendation"), choice))
            st.choose(p["action_id"], choice)
        for key, aid in (p.get("outbound") or {}).items():
            if st.decision(aid) is None:
                st.decide(aid, approve=approve(key))
    return orch.load(rid), seen


def test_phases_3_to_5_run_through_their_milestones(web, strong_brief):
    calls = []
    rec, seen = drive(strong_brief, ALL, calls=calls)
    assert [s[0] for s in seen] == ["roadmap", "M1", "M2", "M3", "M4", "M5", "M6"], rec.stop_reasons or rec.blocked_reason
    assert all(s[1] in (None, "go") for s in seen[:6])
    out = outputs(rec.run_id)

    spec = (out / "phase3_step1_mvp_spec.md").read_text(encoding="utf-8")
    assert "# MVP spec: Translator Deadline Board" in spec and "Acceptance tests" in spec  # one name across phases
    product = (out / "phase3_step2_product.md").read_text(encoding="utf-8")
    assert all(f"## {t}" in product for t in TITLES)
    tests = (out / "phase3_gate_tests.md").read_text(encoding="utf-8")
    assert "Outcome: GO" in tests and "every section in the spec is present | pass" in tests
    assert "It does not send reminders" in (out / "phase3_step3_user_docs.md").read_text(encoding="utf-8")

    pricing = (out / "phase4_step1_pricing.md").read_text(encoding="utf-8")
    assert "**Recommended price: $9.00, one-time payment.**" in pricing
    assert "([source](https://shop0.example/page))" in pricing and "at this price **ASSUMPTION**" in pricing
    policies = (out / "phase4_step2_policies.md").read_text(encoding="utf-8")
    assert "Draft for founder review, not legal advice." in policies and "never moves money" in policies

    page = (out / "phase5_step1_store_page.html").read_text(encoding="utf-8")
    assert "$9.00, one-time payment" in page and "not connected" in page and "<li>Client table</li>" in page
    for banned in ("testimonial", "countdown", "guarantee", "★", "<form"):
        assert banned not in page.lower()
    posts = (out / "phase5_step2_launch_posts.md").read_text(encoding="utf-8")
    assert "Nothing has been posted" in posts and "## 2. A Notion community" in posts

    pay = (out / "payment_test_log.md").read_text(encoding="utf-8")
    assert "MOCKED" in pay and "$9.00" in pay and "FAIL" not in pay
    publish = (out / "phase5_step4_publish_log.md").read_text(encoding="utf-8")
    assert "would be published" in publish and "A translators' forum: would be posted" in publish

    names = [c[1] for c in calls]
    assert names[12:] == ["Plan", "Spec", "Product", "Docs", "Verification", "Plan", "Pricing", "Policies", "Verification",
                          "Plan", "Store", "Posts", "Verification", "Plan", "Faq", "Memo", "Verification"]  # then Phase 6
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert all(e["model"] in (None, "MOCKED-llm") for e in entries)


def test_short_sections_fail_the_product_tests_and_go_back_to_the_makers(web, strong_brief):
    thin = {"sections": [{"title": t, "body": "A table for the translator to copy. " * 5} for t in TITLES]}  # 180 characters
    rec, seen = drive(strong_brief, dict(ALL, Product=thin),
                      choose=lambda rec: None if rec.pending.get("milestone") == "M3" else rec.pending["choices"][0])
    assert rec.pending["recommendation"] == "stop" and "continue" not in rec.pending["choices"]
    assert rec.budget["P3_BUILD"]["repeat"] == 1
    tests = (outputs(rec.run_id) / "phase3_gate_tests.md").read_text(encoding="utf-8")
    assert "every section has enough content | MISS" in tests and "Outcome: STOP" in tests


def test_acceptance_tests_in_code():
    good = [{"title": t, "body": "x" * 300} for t in TITLES]
    assert all(builder.run_acceptance(TITLES, good, "text").values())
    assert not builder.run_acceptance(TITLES, good[:2], "text")["every section in the spec is present"]
    assert builder.run_acceptance([t.upper() + "!" for t in TITLES], good, "t")["every section in the spec is present"]
    assert not builder.run_acceptance(TITLES, good, "TODO: write this")["no placeholder text"]
    assert not builder.run_acceptance(TITLES, good, "x" * 20_001)["the product is short enough to review"]


def test_store_copy_with_a_customer_quote_is_blocked(web, strong_brief):
    bad = dict(STORE, description='"This pack changed how I work and I cannot recommend it enough" - Maria, verified buyer.')
    rec, seen = drive(strong_brief, dict(ALL, Store=bad),
                      choose=lambda rec: None if rec.pending.get("milestone") == "M5" else rec.pending["choices"][0])
    assert not (outputs(rec.run_id) / "phase5_step1_store_page.html").exists()
    assert rec.pending["recommendation"] in ("pivot", "stop")


def test_rejecting_every_outbound_action_publishes_and_charges_nothing(web, strong_brief):
    rec, _ = drive(strong_brief, ALL, approve=lambda key: False)
    out = outputs(rec.run_id)
    for name in ("phase2_step4_publish_log.md", "payment_test_log.md", "phase5_step4_publish_log.md"):
        assert not (out / name).exists(), name
    entries = DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert not [e for e in entries if e["action"].startswith(("mocked_publish", "mocked_post", "mocked_open_checkout"))]
    assert json.loads((orch.run_dir(rec.run_id) / "approvals.json").read_text(encoding="utf-8"))


def test_a_repeated_section_heading_is_printed_once(web, strong_brief):
    doubled = {"sections": [{"title": t, "body": f"## {t}\n\n{BODY}"} for t in TITLES]}
    rec, _ = drive(strong_brief, dict(ALL, Product=doubled),
                   choose=lambda rec: None if rec.pending.get("milestone") == "M3" else rec.pending["choices"][0])
    product = (outputs(rec.run_id) / "phase3_step2_product.md").read_text(encoding="utf-8")
    assert product.count("## Client table") == 1

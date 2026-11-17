"""Web UI and CLI smoke tests (mock mode, no model)."""


from fastapi.testclient import TestClient

import guardrails
import orchestrator as orch
import venture
from app.main import app
from test_pipeline import install_fakes, registry, store, to_roadmap


def test_home_page_lists_steps_and_mocks():
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "STUBBED" in r.text and "MOCKED" in r.text and "phase1_gate_feasibility.md" in r.text


def test_run_page_shows_milestones_and_refuses_unoffered_choice(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = to_roadmap(rid, registry())
    assert rec.state == "ROADMAP"
    client = TestClient(app)
    page = client.get(f"/runs/{rid}")
    assert page.status_code == 200 and "M6" in page.text and "Approve the six-milestone roadmap" in page.text
    bad = client.post(f"/runs/{rid}/choice", data={"action_id": rec.pending["action_id"], "choice": "continue"},
                      follow_redirects=False)
    assert bad.status_code == 400
    assert store(rid).choice(rec.pending["action_id"]) is None


def test_cli_run_without_prompt_reports_status_and_milestones(strong_brief, capsys):
    code = venture.main(["run", "--brief", "briefs/example_strong.json", "--no-prompt"])
    out = capsys.readouterr().out  # mock mode has no model, so the assessor's output is invalid and the run pauses
    assert code == 0 and "blocked" in out and "model_output_invalid" in out and "M1 Discover" in out


def test_stop_button_and_cli_stop(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    orch.advance(rid, registry=registry())
    client = TestClient(app)
    assert "Stop the run" in client.get(f"/runs/{rid}").text
    r = client.post(f"/runs/{rid}/stop", follow_redirects=False)
    assert r.status_code == 303 and orch.load(rid).status == "stopped"
    rid2 = orch.new_run(strong_brief).run_id
    assert venture.main(["stop", rid2]) == 0 and orch.load(rid2).status == "stopped"


def test_milestone_review_screen_shows_reasons_files_cost_and_next_milestone(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")  # roadmap
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1"
    page = TestClient(app).get(f"/runs/{rid}").text
    assert "Why the gate says so" in page and "all evidence thresholds met" in page
    assert 'title="phase1_step1_opportunities.md">Ranked opportunities</a>' in page  # a plain label; the file name is the link target
    assert "Verification report" in page and "<td>G1</td>" in page
    assert "Cost so far:" in page and "Next milestone:" in page and "M2: Would people sign up?" in page
    assert "The gate recommends that you go on" in page


# ---------------------------------------------------------------- readable pages

def test_run_page_says_in_plain_words_what_is_happening(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = orch.advance(rid, registry=reg)
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    assert "Before Phase 1: feasibility check: waiting for your decision." in page
    assert "recommendation, which is not enforced: Proceed" in page
    rec = to_roadmap(orch.new_run(strong_brief).run_id, reg)
    rid = rec.run_id
    page = client.get(f"/runs/{rid}").text
    assert "Before Phase 1: milestone roadmap: waiting for your decision." in page
    assert f"<h1>{strong_brief['idea']}</h1>" in page  # the idea is the title; the run id is in small print
    assert ">Scope check</a>" in page and "Files from this run" in page  # files are shown by what they are
    store(rid).choose(rec.pending["action_id"], "approve")
    orch.advance(rid, registry=reg)
    page = client.get(f"/runs/{rid}").text
    assert "Phase 1: Discover: waiting for your decision." in page
    assert 'data-label="Checked"' in page  # the verification report says what each criterion checks
    home = client.get("/").text
    assert strong_brief["idea"] in home and "Waiting for your decision" in home and "Phase 1: Discover" in home


def test_output_pages_are_rendered_and_generated_html_is_only_previewed_in_a_sandbox(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    orch.advance(rid, registry=registry())
    out = orch.run_dir(rid) / "outputs"
    client = TestClient(app)
    page = client.get(f"/runs/{rid}/outputs/phase1_gate_feasibility.md").text
    assert "<h1>Feasibility warnings</h1>" in page and '<table class="stack">' in page and "<td data-label=" in page
    assert "<summary>Technical details</summary>" in page and "<code>phase1_gate_feasibility.md</code>" in page
    (out / "phase1_step2_scoring.json").write_text('{"pick": "O1", "scores": [{"id": "O1", "total": 14}]}', encoding="utf-8")
    page = client.get(f"/runs/{rid}/outputs/phase1_step2_scoring.json").text
    assert "<strong>14 / 20</strong>" in page and "Picked:" in page and "<th>pick</th>" not in page
    (out / "phase2_step2_landing_page.html").write_text("<!doctype html><p>Draft</p><script>alert(1)</script>", encoding="utf-8")
    page = client.get(f"/runs/{rid}/outputs/phase2_step2_landing_page.html").text
    assert '<iframe class="preview" sandbox ' in page and "<script>alert(1)</script>" not in page
    assert "It is not published" in page


def test_rendered_markdown_escapes_everything_a_model_wrote():
    from app import render

    html_out = render.markdown_to_html(
        "# Title <script>alert(1)</script>\n\n"
        "- a claim. Source: https://example.org/page?a=1&b=2\n- another claim. ASSUMPTION\n\n"
        "| Check | Result |\n|---|---|\n| <img src=x onerror=alert(1)> | **pass** |\n\n"
        "[click](javascript:alert(1)) and [ok](https://example.org/x) and `<b>code</b>`\n")
    assert "<script>" not in html_out and "<img" not in html_out and "<b>code" not in html_out
    assert "&lt;script&gt;" in html_out and "&lt;b&gt;code&lt;/b&gt;" in html_out
    assert '<a href="https://example.org/page?a=1&amp;b=2"' in html_out and '<a href="https://example.org/x"' in html_out
    assert 'href="javascript' not in html_out
    assert '<span class="label ASSUMPTION">ASSUMPTION</span>' in html_out
    assert "<h2>Title" in html_out and "<strong>pass</strong>" in html_out and html_out.count("<li>") == 2
    assert render.json_to_html("not json <b>") == "<pre>not json &lt;b&gt;</pre>"
    assert "<script>" not in render.json_to_html('{"a": "<script>x</script>"}')


# ---------------------------------------------------------------- a founder's own idea, typed into the form

def _no_background(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "_start_in_background", lambda run_id: None)


OWN_IDEA = {"idea": "A checklist that helps first-time conference speakers prepare their talk",
            "target_market": "Engineers giving their first conference talk", "skills": "public speaking, writing",
            "budget_usd": "40", "hours_per_week": "6", "values": "honest marketing; no hype"}


def test_a_typed_idea_starts_a_run_with_that_brief(monkeypatch):
    _no_background(monkeypatch)
    client = TestClient(app)
    home = client.get("/").text
    assert 'action="/runs/custom"' in home and 'name="idea"' in home and 'name="hours_per_week"' in home
    r = client.post("/runs/custom", data=OWN_IDEA, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    rec = orch.load(r.headers["location"].rsplit("/", 1)[1])
    assert rec.brief == {"idea": OWN_IDEA["idea"], "target_market": OWN_IDEA["target_market"],
                         "skills": ["public speaking", "writing"], "budget_usd": 40.0, "hours_per_week": 6.0,
                         "values": ["honest marketing", "no hype"]}
    page = client.get(r.headers["location"]).text
    assert f"<h1>{OWN_IDEA['idea']}</h1>" in page and "The brief for this run" in page and "public speaking, writing" in page


def test_a_typed_idea_goes_through_the_same_brief_screen(monkeypatch):
    _no_background(monkeypatch)
    install_fakes(monkeypatch)
    client = TestClient(app)
    excluded = dict(OWN_IDEA, idea="A crypto trading signals newsletter for beginners")
    r = client.post("/runs/custom", data=excluded, follow_redirects=False)
    rid = r.headers["location"].rsplit("/", 1)[1]
    rec = orch.advance(rid, registry=registry())
    assert rec.state == "STOPPED" and rec.stop_reasons == [guardrails.SCOPE_REPLY]
    assert "The run stopped at before phase 1: scope screen." in client.get(f"/runs/{rid}").text


def test_a_typed_idea_with_a_secret_or_a_bad_number_is_handled(monkeypatch):
    _no_background(monkeypatch)
    client = TestClient(app)
    r = client.post("/runs/custom", data=dict(OWN_IDEA, idea=OWN_IDEA["idea"] + ". My password is hunter2"),
                    follow_redirects=False)
    rec = orch.load(r.headers["location"].rsplit("/", 1)[1])
    assert "hunter2" not in rec.brief["idea"] and "[REDACTED]" in rec.brief["idea"]
    before = len(orch.list_runs())
    for bad in [dict(OWN_IDEA, budget_usd="a lot"), dict(OWN_IDEA, hours_per_week="-3"), dict(OWN_IDEA, idea="   "),
                dict(OWN_IDEA, budget_usd="nan")]:
        r = client.post("/runs/custom", data=bad, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/?msg=The%20idea%20was%20not%20started")
    assert len(orch.list_runs()) == before
    assert "The idea was not started" in client.get("/?msg=The idea was not started: budget must be a number").text


def test_a_run_interrupted_by_a_restart_is_paused_and_can_be_resumed(monkeypatch, strong_brief):
    import app.main as main

    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    waiting = orch.new_run(strong_brief).run_id
    orch.advance(waiting, registry=registry())  # waits for the founder at the feasibility screen
    assert orch.load(rid).status == "running"  # as a restart leaves it: marked as working, with no worker
    with TestClient(app) as client:  # entering the client runs the app's start-up step
        assert orch.load(rid).status == "blocked" and "restarted" in orch.load(rid).blocked_reason
        assert orch.load(waiting).status == "awaiting_approval"  # a run that waits for the founder is left alone
        page = client.get(f"/runs/{rid}").text
        assert "Resume" in page and "restarted while this run was working" in page
    assert main.pause_interrupted_runs() == []
    rec = orch.advance(rid, registry=registry())
    assert rec.status == "awaiting_approval" and rec.state == "FEASIBILITY"


# ---------------------------------------------------------------- the feasibility screen

def weak_brief():
    import json

    import settings
    return json.loads((settings.BRIEFS_DIR / "example_weak.json").read_text(encoding="utf-8"))


def test_feasibility_screen_lists_warnings_major_first_and_takes_a_revision(monkeypatch):
    from agents import assessor
    from test_pipeline import fake_assess

    _no_background(monkeypatch)
    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(family="Written content", mvp_cost_usd=15, effort_hours=80,
                                                        needed_skills=["writing", "typewriter repair"]))
    rid = orch.new_run(weak_brief()).run_id
    reg = registry()
    rec = orch.advance(rid, registry=reg)
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    assert "This check gives warnings, never a verdict." in page and "Revise or stop" in page
    assert page.index('<span class="tag major">major</span>') < page.index("Checks that raised no warning")
    assert "Continue to the roadmap" in page and "Revise and check again" in page and 'name="hours_per_week"' in page
    bad = client.post(f"/runs/{rid}/choice", data={"action_id": rec.pending["action_id"], "choice": "revise",
                                                   "budget_usd": "plenty"}, follow_redirects=False)
    assert bad.status_code == 400 and store(rid).choice(rec.pending["action_id"]) is None  # nothing changed
    ok = client.post(f"/runs/{rid}/choice", data={"action_id": rec.pending["action_id"], "choice": "revise",
                                                  "idea": rec.brief["idea"], "budget_usd": "60", "hours_per_week": "12",
                                                  "skills": "writing, typewriter repair"}, follow_redirects=False)
    assert ok.status_code == 303
    rec = orch.advance(rid, registry=reg)
    assert rec.state == "FEASIBILITY" and rec.brief["budget_usd"] == 60 and rec.brief["hours_per_week"] == 12
    assert rec.brief["skills"] == ["writing", "typewriter repair"] and "W-SKILLS" not in rec.pending["major_ids"]
    text = (orch.run_dir(rid) / "outputs" / "phase1_gate_feasibility.md").read_text(encoding="utf-8")
    assert "- Revised budget usd: 60.0" in text and "- Revised skills:" in text and "Revised idea" not in text


def test_the_roadmap_and_milestone_screens_carry_the_warning_line(monkeypatch):
    from agents import assessor
    from test_pipeline import fake_assess

    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(family="Written content", mvp_cost_usd=15, effort_hours=80,
                                                        needed_skills=["writing", "typewriter repair"]))
    rid = orch.new_run(weak_brief()).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    client = TestClient(app)
    line = f"Continued with {orch.feasibility_meta(rec)['majors']} major feasibility warnings"
    assert line in client.get(f"/runs/{rid}").text  # on the roadmap approval screen
    store(rid).choose(rec.pending["action_id"], "approve")
    orch.advance(rid, registry=reg)
    assert line in client.get(f"/runs/{rid}").text  # and on the milestone review


def test_a_brief_without_a_time_frame_is_asked_for_one_and_blank_keeps_four_weeks(monkeypatch):
    from agents import assessor
    from test_pipeline import fake_assess

    _no_background(monkeypatch)
    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(family="Written content", mvp_cost_usd=0, effort_hours=30,
                                                        needed_skills=["writing"]))
    rid = orch.new_run(weak_brief()).run_id  # 1 hour a week and no time frame
    reg = registry()
    rec = orch.advance(rid, registry=reg)
    client = TestClient(app)
    assert rec.pending["asks_timeframe"] and "What is your time frame?" in client.get(f"/runs/{rid}").text
    time = next(w for w in rec.pending["warnings"] if w["id"] == "W-TIME")
    assert time["level"] == "major" and "so 4 weeks are used (ASSUMPTION)" in time["fact"]
    blank = client.post(f"/runs/{rid}/choice", data={"action_id": rec.pending["action_id"], "choice": "revise",
                                                     "timeframe_weeks": ""}, follow_redirects=False)
    assert blank.status_code == 303
    rec = orch.advance(rid, registry=reg)
    assert "timeframe_weeks" not in rec.brief and rec.pending["asks_timeframe"]  # blank: still 4 weeks, still labelled
    ok = client.post(f"/runs/{rid}/choice", data={"action_id": rec.pending["action_id"], "choice": "revise",
                                                  "timeframe_weeks": "20", "delegated_hours_per_week": "1"},
                     follow_redirects=False)
    assert ok.status_code == 303
    rec = orch.advance(rid, registry=reg)
    assert rec.brief["timeframe_weeks"] == 20 and rec.brief["delegated_hours_per_week"] == 1 and not rec.pending["asks_timeframe"]
    time = next(w for w in rec.pending["warnings"] if w["id"] == "W-TIME")
    assert time["level"] == "none" and time["fact"].startswith("40 hours available")


def test_the_own_idea_form_takes_an_optional_time_frame(monkeypatch):
    _no_background(monkeypatch)
    client = TestClient(app)
    r = client.post("/runs/custom", data=dict(OWN_IDEA, timeframe_weeks="12", delegated_hours_per_week="3"), follow_redirects=False)
    rec = orch.load(r.headers["location"].rsplit("/", 1)[1])
    assert rec.brief["timeframe_weeks"] == 12 and rec.brief["delegated_hours_per_week"] == 3
    bad = client.post("/runs/custom", data=dict(OWN_IDEA, timeframe_weeks="0"), follow_redirects=False)
    assert bad.headers["location"].startswith("/?msg=The%20idea%20was%20not%20started")


# ---------------------------------------------------------------- plain-language screens

def test_the_roadmap_screen_uses_plain_words_and_shows_no_file_name_or_warning_id(monkeypatch):
    import re
    from agents import assessor
    from test_pipeline import fake_assess

    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(family="Written content", mvp_cost_usd=15, effort_hours=80,
                                                        needed_skills=["writing", "typewriter repair"]))
    rid = orch.new_run(weak_brief()).run_id
    to_roadmap(rid, registry())
    page = TestClient(app).get(f"/runs/{rid}").text
    table = page.split("<th>What you get</th>")[1].split("</table>")[0]
    for column in ("Step", "What happens", "What you get", "Your decision"):
        assert f"<th>{column}</th>" in page
    for title in ("1. Is the problem real?", "2. Would people sign up?", "3. Build the first version", "4. Price and policies",
                  "5. Launch (simulated)", "6. Learn and decide"):
        assert title in table
    assert not re.search(r"phase\d_|\.md|\.json|\.html|W-[A-Z]+|evidence threshold", table)
    assert "ranked opportunities" in table and "MOCKED" in table
    assert "Tell us what to change in this plan." in page
    carried = page.split("Continued with")[1].split("</p>")[0]
    assert "W-" not in carried and "a skill the idea needs is missing" in carried  # warnings by their description


def test_the_milestone_review_has_a_headline_flat_reasons_and_hides_the_next_step_after_a_stop(monkeypatch, strong_brief):
    install_fakes(monkeypatch, problem=1, paying=0)  # the Phase 1 gate recommends stop
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    orch.advance(rid, registry=reg)
    page = TestClient(app).get(f"/runs/{rid}").text
    assert "The gate recommends that you stop: missed: problem sources and 2 more." in page
    reasons = page.split("Why the gate says so</h3>")[1].split("</ul>")[0]
    assert "((" not in reasons and "(" not in reasons and "our rule of thumb" in reasons and "ASSUMPTION" not in reasons
    assert "Next milestone:" not in page  # "continue" is not offered, so no next step is shown
    assert "Continue against the recommendation" in page


def test_the_scoring_page_is_a_result_page_not_a_data_dump(monkeypatch, strong_brief):
    import json

    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    orch.advance(rid, registry=registry())
    out = orch.run_dir(rid) / "outputs"
    scored = {"result": "Picked: Translators working with agencies, with 15 out of 20. Its highest score is for founder fit.",
              "scores": [{"id": "O1", "title": "Translators working with agencies", "pain": 4, "willingness_to_pay": 2, "reach": 4,
                          "founder_fit": 5, "reason": "three pages describe the problem", "total": 15},
                         {"id": "O2", "title": "Translators with direct clients", "pain": 3, "willingness_to_pay": 2, "reach": 3,
                          "founder_fit": 5, "reason": "one page", "total": 13}],
              "pick": "O1", "pick_title": "Translators working with agencies",
              "smallest_version": {"value": "one Notion page with a client table", "basis": "ASSUMPTION"},
              "mvp_cost_usd": {"value": 0.0, "basis": "ASSUMPTION"}, "mvp_hours_per_week": {"value": 2.0, "basis": "ASSUMPTION"}}
    (out / "phase1_step2_scoring.json").write_text(orch.with_header("phase1_step2_scoring.json", json.dumps(scored), "scorer", "m", "t"),
                                                    encoding="utf-8")
    page = TestClient(app).get(f"/runs/{rid}/outputs/phase1_step2_scoring.json").text
    shown = page.split("<summary>Technical details</summary>")[0]
    assert "Picked: Translators working with agencies, with 15 out of 20." in shown
    assert "4 / 5" in shown and "<strong>15 / 20</strong>" in shown and "picked</span>" in shown
    assert "Scale: 1 no sign, 3 some sign, 5 strong sign." in shown
    assert "<strong>Estimated cost:</strong> $0" in shown and "excludes paid help" in shown and "<strong>Estimated hours a week:</strong> 2" in shown
    assert shown.count("ASSUMPTION</span>") == 1  # one note for the whole card
    for raw in ("pick_title", "pick rule", "mvp cost usd", "mvp_cost_usd", "0.0", "N/A", ">O1<", "scorer", "Run started"):
        assert raw not in shown.split("<h1>")[1], raw
    details = page.split("<summary>Technical details</summary>")[1]
    assert "<code>phase1_step2_scoring.json</code>" in details and "scorer" in details and "Run started" in details
    empty = {"result": "Not scored: no evidence was collected in this run, so the opportunities cannot be compared. No pick was made.",
             "scores": [{"id": "O1", "title": "Translators working with agencies", "pain": "not scored", "willingness_to_pay": "not scored",
                         "reach": "not scored", "founder_fit": 5, "reason": "no evidence was collected", "total": "not scored"}],
             "pick": None, "mvp_cost_usd": {"value": None}, "mvp_hours_per_week": {"value": None}}
    (out / "phase1_step2_scoring.json").write_text(json.dumps(empty), encoding="utf-8")
    shown = TestClient(app).get(f"/runs/{rid}/outputs/phase1_step2_scoring.json").text.split("<summary>Technical details</summary>")[0]
    assert "No pick was made." in shown and shown.count("not scored") >= 4 and "5 / 5" in shown and "picked</span>" not in shown
    assert "Not measured" in shown and "The smallest version worth testing" not in shown


def test_a_run_whose_decision_was_answered_before_a_restart_is_picked_up_again(monkeypatch, strong_brief):
    import app.main as main

    install_fakes(monkeypatch)
    started = []
    monkeypatch.setattr(main, "_start_in_background", started.append)
    waiting = orch.new_run(strong_brief).run_id
    orch.advance(waiting, registry=registry())  # the founder has not answered the feasibility screen
    answered = orch.new_run(strong_brief).run_id
    rec = orch.advance(answered, registry=registry())
    store(answered).choose(rec.pending["action_id"], "continue")  # answered, but nothing advanced the run
    assert main.answered_runs() == [answered]
    with TestClient(app):
        assert started == [answered]  # start-up carries on with it and leaves the unanswered run alone

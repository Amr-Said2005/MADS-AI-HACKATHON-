"""Web UI and CLI smoke tests (mock mode, no model)."""


from fastapi.testclient import TestClient

import orchestrator as orch
import venture
from app.main import app
from test_pipeline import install_fakes, registry, store


def test_home_page_lists_steps_and_mocks():
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "STUBBED" in r.text and "MOCKED" in r.text and "phase1_gate_feasibility.md" in r.text


def test_run_page_shows_milestones_and_refuses_unoffered_choice(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    rec = orch.advance(rid, registry=registry())
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
    out = capsys.readouterr().out  # mock mode has no model, so the assessor's output is invalid and the run stops
    assert code == 0 and "stopped" in out and "model_output_invalid" in out and "M1 Discover" in out


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
    rec = orch.advance(rid, registry=reg)
    store(rid).choose(rec.pending["action_id"], "approve")  # roadmap
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1"
    page = TestClient(app).get(f"/runs/{rid}").text
    assert "Why the gate says so" in page and "all evidence thresholds met" in page
    assert "phase1_step1_opportunities.md</a>" in page and "phase1_gate_decision.md</a>" in page
    assert "Verification report" in page and "<td>G1</td>" in page
    assert "Cost so far:" in page and "Next milestone:" in page and "M2: Milestone 2" in page


# ---------------------------------------------------------------- readable pages

def test_run_page_says_in_plain_words_what_is_happening(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = orch.advance(rid, registry=reg)
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    assert "Before Phase 1: milestone roadmap: waiting for your decision." in page
    assert f"<h1>{strong_brief['idea']}</h1>" in page  # the idea is the title; the run id is in small print
    assert "Founder brief screen (gate)" in page and "Files from this run" in page  # files carry a description
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
    assert "<h1>Feasibility check (gate)</h1>" in page and '<table class="stack">' in page and "<td data-label=" in page
    (out / "phase1_step2_scoring.json").write_text('{"pick": "O1", "scores": [{"id": "O1", "total": 14}]}', encoding="utf-8")
    page = client.get(f"/runs/{rid}/outputs/phase1_step2_scoring.json").text
    assert "<th>pick</th>" in page and '<td data-label="total">14</td>' in page
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
    assert rec.state == "STOPPED" and any("excluded category" in x for x in rec.stop_reasons)
    assert "The run stopped at before phase 1: brief screen." in client.get(f"/runs/{rid}").text


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

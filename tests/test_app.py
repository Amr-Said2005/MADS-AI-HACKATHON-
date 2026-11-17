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


def test_cli_run_without_prompt_stops_at_the_first_stub(strong_brief, capsys):
    code = venture.main(["run", "--brief", "briefs/example_strong.json", "--no-prompt"])
    out = capsys.readouterr().out
    assert code == 0 and "blocked" in out and "STUBBED" in out and "M1 Discover" in out


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

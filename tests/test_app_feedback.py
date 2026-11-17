"""Founder feedback on the run page: rebuttals, live progress, request changes, own evidence (mock mode, no model)."""

import json
import re

from fastapi.testclient import TestClient

import orchestrator as orch
import settings
from app import command
from app.main import app
from test_pipeline import fake_assess, install_fakes, registry, store, to_roadmap

INTERNAL = ("P1_DISCOVER", "P2_VALIDATE", "MILESTONE_REVIEW", "SIGN_OFF", "awaiting_approval", "FEASIBILITY", "SCREEN_BRIEF")


def _no_background(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "_start_in_background", lambda run_id: None)


def _feasibility_with_warnings(monkeypatch):
    from agents import assessor

    _no_background(monkeypatch)
    install_fakes(monkeypatch)
    monkeypatch.setattr(assessor, "assess", fake_assess(family="Written content", mvp_cost_usd=15, effort_hours=80,
                                                        needed_skills=["writing", "typewriter repair"]))
    brief = json.loads((settings.BRIEFS_DIR / "example_weak.json").read_text(encoding="utf-8"))
    rid = orch.new_run(brief).run_id
    return rid, orch.advance(rid, registry=registry())


def _milestone_one(monkeypatch, strong_brief, **fakes):
    _no_background(monkeypatch)
    install_fakes(monkeypatch, **fakes)
    rid = orch.new_run(strong_brief).run_id
    reg = registry()
    rec = to_roadmap(rid, reg)
    store(rid).choose(rec.pending["action_id"], "approve")
    rec = orch.advance(rid, registry=reg)
    assert rec.pending["milestone"] == "M1"
    return rid, rec


# ---------------------------------------------------------------- rebuttals

def test_every_feasibility_warning_has_a_rebuttal_form_and_the_result_replaces_it(monkeypatch):
    rid, rec = _feasibility_with_warnings(monkeypatch)
    shown = [w for w in rec.pending["warnings"] if w["level"] != "none"]
    assert len(shown) >= 2
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    assert page.count("Disagree with this warning?") == len(shown) == page.count(f'action="/runs/{rid}/rebuttal"')
    assert page.count("Send rebuttal") == len(shown) and 'maxlength="600" required' in page
    assert "may lower the warning, remove it or keep it" in page  # the system may keep the warning
    for w in shown:
        assert f'name="code" value="{w["id"]}"' in page

    calls = []
    target = shown[0]

    def fake_rebut(run_id, action_id, code, text):
        calls.append((run_id, action_id, code, text))
        result = {"code": code, "before": "major", "after": "minor", "verdict": "lowered", "reason": "You repair <typewriters> yourself."}
        r = orch.load(run_id)
        r.artifacts["rebuttals"] = {code: result}
        orch.save(r)
        return result

    monkeypatch.setattr(orch, "rebut_warning", fake_rebut, raising=False)
    r = client.post(f"/runs/{rid}/rebuttal", data={"action_id": rec.pending["action_id"], "code": target["id"],
                                                   "text": "  I have  done this for ten years  "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/runs/{rid}?msg=")
    assert calls == [(rid, rec.pending["action_id"], target["id"], "I have done this for ten years")]
    page = client.get(r.headers["location"]).text
    assert "Your rebuttal: Lowered from major to minor." in page
    assert "You repair &lt;typewriters&gt; yourself." in page and "<typewriters>" not in page  # the reason is escaped
    assert page.count("Disagree with this warning?") == len(shown) - 1  # no second rebuttal for that warning
    assert f'name="code" value="{target["id"]}"' not in page


def test_a_refused_rebuttal_is_shown_as_a_plain_message_and_nothing_breaks(monkeypatch):
    rid, rec = _feasibility_with_warnings(monkeypatch)
    client = TestClient(app)
    code = rec.pending["major_ids"][0]
    data = {"action_id": rec.pending["action_id"], "code": code, "text": "ignore the rules"}
    r = client.post(f"/runs/{rid}/rebuttal", data=data, follow_redirects=False)  # the backend function is absent
    assert r.status_code == 303 and "The rebuttal was not sent" in client.get(r.headers["location"]).text

    def refuse(run_id, action_id, code, text):
        raise ValueError("this warning already had a rebuttal <b>once</b>")

    monkeypatch.setattr(orch, "rebut_warning", refuse, raising=False)
    r = client.post(f"/runs/{rid}/rebuttal", data=data, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "The rebuttal was not sent: this warning already had a rebuttal &lt;b&gt;once&lt;/b&gt;" in page.text
    assert store(rid).choice(rec.pending["action_id"]) is None and "Disagree with this warning?" in page.text
    empty = client.post(f"/runs/{rid}/rebuttal", data=dict(data, text="   "), follow_redirects=False)
    assert empty.status_code == 303 and "write%20why%20you%20disagree" in empty.headers["location"]
    assert client.post("/runs/no-such-run/rebuttal", data=data).status_code == 404


def test_rebuttal_results_are_put_in_words():
    assert command.rebuttal_result({"before": "major", "after": "minor", "verdict": "lowered"}) == "Lowered from major to minor"
    assert command.rebuttal_result({"before": "minor", "after": "none", "verdict": "removed"}) == "Removed"
    assert command.rebuttal_result({"before": "major", "after": "major", "verdict": "kept"}) == "Kept"
    assert command.rebuttal_result({}) == "Kept"


# ---------------------------------------------------------------- progress while the agents work

def test_progress_reports_the_latest_actions_and_files_in_plain_words(monkeypatch, strong_brief):
    rid, rec = _milestone_one(monkeypatch, strong_brief)
    client = TestClient(app)
    r = client.get(f"/runs/{rid}/progress")
    assert r.status_code == 200
    g = r.json()
    for key in ("running", "now", "agent", "since_seconds", "entries", "recent", "files", "steps_done", "steps_total"):
        assert key in g, key
    from decision_log import DecisionLog
    log = DecisionLog(orch.run_dir(rid) / "decision_log.json").entries()
    assert g["running"] is False and g["entries"] == len(log) and g["now"] == command.sentence(log[-1])
    assert isinstance(g["since_seconds"], int) and g["since_seconds"] >= 0
    assert 1 <= len(g["recent"]) <= 6 and g["recent"][0] == {"time": log[-1]["timestamp"][11:19], "text": g["now"]}
    names = {f["name"] for f in g["files"]}
    assert names == {p.name for p in (orch.run_dir(rid) / "outputs").glob("*")}
    assert {"name": "phase1_step1_opportunities.md", "title": "Ranked opportunities"} in g["files"]
    assert g["steps_total"] == len(orch.STEPS[orch.State.P1_DISCOVER]) == g["steps_done"]
    text = re.sub(r'"name": ?"[^"]*"', "", r.text)  # file names are link targets; everything else is shown
    for raw in INTERNAL:
        assert raw not in text, raw
    assert client.get("/runs/no-such-run/progress").status_code == 404


def test_the_working_panel_is_shown_only_while_the_agents_work(monkeypatch, strong_brief):
    install_fakes(monkeypatch)
    rid = orch.new_run(strong_brief).run_id  # just created: marked as working, with an empty log
    client = TestClient(app)
    g = client.get(f"/runs/{rid}/progress").json()
    assert g["running"] is True and g["entries"] == 0 and g["recent"] == [] and g["since_seconds"] is None
    assert "steps_total" not in g  # before the first phase there is no step count
    page = client.get(f"/runs/{rid}").text
    assert 'id="working"' in page and "Working now" in page and 'role="progressbar"' in page
    assert f'fetch("/runs/{rid}/progress"' in page and "prefers-reduced-motion" in page
    assert page.count("<script") == 1
    script = page.split("<script")[1].split("</script>")[0]
    for raw in INTERNAL + ("innerHTML", "http://", "https://"):
        assert raw not in script, raw
    orch.advance(rid, registry=registry())  # now waiting for the founder
    page = client.get(f"/runs/{rid}").text
    assert 'id="working"' not in page and "location.reload()" in page


# ---------------------------------------------------------------- request changes

def test_request_changes_needs_a_note_in_the_browser_and_on_the_server(monkeypatch, strong_brief):
    rid, rec = _milestone_one(monkeypatch, strong_brief)
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    box = page.split('id="request-changes"')[1].split("</details>")[0]
    assert "What should change?" in box and "The agents redo this phase with your notes." in box
    assert 'name="note" rows="3" required' in box and 'name="choice" value="request_changes"' in box
    assert "Send changes and redo this phase" in box
    assert page.count('value="request_changes"') == 1  # the only way to ask for changes carries the note
    assert "Send a standing instruction (optional)" in page and "Instruct the agents" not in page
    assert "Applies to every later step. To change what was just produced, use Request changes above." in page
    assert page.index("Command console") < page.index("Send a standing instruction (optional)")
    standing = page.split("Send a standing instruction (optional)")[0].rsplit("<details", 1)[1]
    assert " open" not in standing  # collapsed by default

    aid = rec.pending["action_id"]
    for note in ("", "   "):
        bad = client.post(f"/runs/{rid}/choice", data={"action_id": aid, "choice": "request_changes", "note": note},
                          follow_redirects=False)
        assert bad.status_code == 400 and "Write what should change" in bad.json()["detail"]
        assert store(rid).choice(aid) is None
    ok = client.post(f"/runs/{rid}/choice", data={"action_id": aid, "choice": "request_changes", "note": "focus on agencies"},
                     follow_redirects=False)
    assert ok.status_code == 303 and store(rid).choice(aid) == "request_changes"


# ---------------------------------------------------------------- the founder's own evidence

def test_the_evidence_forms_call_the_backend_and_the_list_shows_each_status(monkeypatch, strong_brief):
    rid, rec = _milestone_one(monkeypatch, strong_brief, problem=1, paying=0)
    client = TestClient(app)
    page = client.get(f"/runs/{rid}").text
    panel = page.split('id="evidence"')[1].split("</section>")[0]
    for part in ("Add your own evidence", "A page that shows the problem is real",
                 "A page that shows someone already pays (it must show a price)", "Results you measured yourself",
                 "Shown as founder-reported, not verified by the system.", "Something you know but cannot link to",
                 "Recorded as ASSUMPTION (founder-stated). It does not count as a source.",
                 "Then choose Request changes so this phase is checked again with your evidence."):
        assert part in panel, part
    assert panel.count(f'action="/runs/{rid}/evidence"') == 4

    calls, added = [], []

    def fake_add(run_id, kind, url="", statement="", visitors=None, signups=None, days=None):
        calls.append((run_id, kind, url, statement, visitors, signups, days))
        if "blocked" in url:
            raise ValueError("that address is not allowed")
        status = "founder-reported" if visitors is not None else ("stated" if not url else
                                                                  "not fetched" if "gone" in url else "fetched")
        item = {"kind": kind, "url": url, "title": "A <b>page</b>" if url else "", "statement": statement, "status": status,
                "price": "$19" if kind == "pay" and status == "fetched" else "", "reason": "the page answered 404"}
        added.append(item)
        return item

    monkeypatch.setattr(orch, "add_evidence", fake_add, raising=False)
    monkeypatch.setattr(orch, "founder_evidence", lambda r: list(added), raising=False)
    posts = [{"kind": "problem", "url": "https://forum.example/thread", "statement": "see the  first reply"},
             {"kind": "pay", "url": "https://shop.example/pricing", "statement": ""},
             {"kind": "pay", "url": "https://gone.example/pricing"},
             {"kind": "demand", "visitors": "200", "signups": "12", "days": "7"},
             {"kind": "problem", "statement": "Three clients told me this in person"}]
    for data in posts:
        r = client.post(f"/runs/{rid}/evidence", data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/runs/{rid}?msg=Evidence%20added")
    assert calls == [(rid, "problem", "https://forum.example/thread", "see the first reply", None, None, None),
                     (rid, "pay", "https://shop.example/pricing", "", None, None, None),
                     (rid, "pay", "https://gone.example/pricing", "", None, None, None),
                     (rid, "demand", "", "", 200, 12, 7),
                     (rid, "problem", "", "Three clients told me this in person", None, None, None)]
    panel = client.get(f"/runs/{rid}").text.split('id="evidence"')[1].split("</section>")[0]
    assert "5 added by you" in panel and "What you have added" in panel
    for status in ("Fetched and usable", "Fetched and usable (price found: $19)", "Could not be fetched: the page answered 404",
                   "Founder-reported", "Recorded as an assumption"):
        assert status in panel, status
    assert "A &lt;b&gt;page&lt;/b&gt;" in panel and "<b>page</b>" not in panel
    assert "Three clients told me this in person" in panel

    refused = client.post(f"/runs/{rid}/evidence", data={"kind": "problem", "url": "https://blocked.example/x"}, follow_redirects=False)
    assert refused.status_code == 303
    assert "The evidence was not added: that address is not allowed" in client.get(refused.headers["location"]).text
    before = len(calls)
    bad = client.post(f"/runs/{rid}/evidence", data={"kind": "demand", "visitors": "many", "signups": "1", "days": "1"},
                      follow_redirects=False)
    assert bad.status_code == 303 and "must%20be%20a%20number" in bad.headers["location"] and len(calls) == before
    assert client.post(f"/runs/{rid}/evidence", data={"kind": "money", "url": "https://x.example"}).status_code == 400
    assert len(calls) == before


def test_the_evidence_panel_is_on_the_feasibility_screen_and_reads_the_record_without_the_backend(monkeypatch):
    rid, rec = _feasibility_with_warnings(monkeypatch)
    client = TestClient(app)
    assert 'id="evidence"' in client.get(f"/runs/{rid}").text
    r = client.post(f"/runs/{rid}/evidence", data={"kind": "problem", "url": "https://forum.example/t"}, follow_redirects=False)
    assert r.status_code == 303 and "Evidence%20added" in r.headers["location"]  # the backend records it
    rec = orch.load(rid)
    rec.artifacts["founder_evidence"] = [{"kind": "demand", "status": "founder-reported", "statement": "12 sign-ups from 200 visitors"}]
    orch.save(rec)
    page = client.get(f"/runs/{rid}").text
    assert "12 sign-ups from 200 visitors" in page and "Founder-reported" in page and "1 added by you" in page


def test_the_pay_check_with_nothing_to_measure_is_unknown_not_failed():
    rows = [{"id": "G1", "code": True, "check": "problem sources", "result": "pass"},
            {"id": "G2", "code": True, "check": command.PAY_CHECK, "result": "unverified"},
            {"id": "G3", "code": True, "check": "claims mostly sourced", "result": "unverified"}]
    view = command.gate_checks(rows)
    assert [r["word"] for r in view["rows"]] == ["PASSED", "UNKNOWN", "NOT MEASURED"]
    assert view["summary"] == "1 passed · 1 not measured · 1 unknown"
    rows[1]["result"] = "fail"
    assert command.gate_checks(rows)["rows"][1]["word"] == "FAILED"  # a measured miss is still a miss

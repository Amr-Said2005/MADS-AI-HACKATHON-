"""The estimate on the "Working now" panel: which step is in flight and how far along it is.

Everything is derived from decision logs and output folders; no model and no network are used."""

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import orchestrator as orch
import settings
from app import command, plain
from app.main import app

INTERNAL = ("P1_DISCOVER", "P2_VALIDATE", "MILESTONE_REVIEW", "SIGN_OFF", "PLAN_REVIEW", "awaiting_approval",
            "FEASIBILITY", "SCREEN_BRIEF", "find_opportunities", "market_research", "interview_guide")
MODEL = "qwen3:8b"


@pytest.fixture(autouse=True)
def fresh_history():
    command.clear_history_cache()
    yield
    command.clear_history_cache()


def entry(agent, action, ago, model=None, outcome=None):
    """One log entry written `ago` seconds before now."""
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=ago)
    e = {"timestamp": at.isoformat().replace("+00:00", "Z"), "agent": agent, "action": action, "sources": [],
         "model": model, "cost": 0, "approved_by": "founder" if agent == "founder" else None}
    if outcome:
        e["outcome"] = outcome
    return e


def phase_one_log(end=5):
    """A first phase up to its third file: the plan took 30 seconds, the search 60, scoring 10, the market 20."""
    return [
        entry("founder", "decide:roadmap:run:abc", end + 120, outcome="approve"),
        entry("guardrails", "check_output:phase1_gate_plan_review.md", end + 90, outcome="pass"),
        entry("planner", "plan", end + 90, MODEL),
        entry("gate", "phase1_plan_review", end + 90, outcome="approved"),
        entry("guardrails", "check_output:phase1_step1_opportunities.md", end + 40, outcome="blocked"),
        entry("guardrails", "check_output:phase1_step1_opportunities.md", end + 30, outcome="pass"),
        entry("researcher", "find_opportunities", end + 30, MODEL),
        entry("scorer", "score", end + 20, MODEL),
        entry("researcher", "competitor_dropped", end + 5, outcome="dropped"),
        entry("guardrails", "check_output:phase1_step3_market.md", end, outcome="pass"),
        entry("researcher", "market_research", end, MODEL),
    ]


FILES = ["phase1_gate_plan_review.md", "phase1_step1_opportunities.md", "phase1_step2_scoring.json", "phase1_step3_market.md"]


def other_run(name, log, status="stopped"):
    d = settings.RUNS_DIR / name
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"run_id": name, "status": status}), encoding="utf-8")
    (d / "decision_log.json").write_text(json.dumps(log), encoding="utf-8")


def running_run(strong_brief, log=None, files=()):
    rec = orch.new_run(strong_brief)  # just created: marked as working
    d = orch.run_dir(rec.run_id)
    if log is not None:
        (d / "decision_log.json").write_text(json.dumps(log), encoding="utf-8")
    (d / "outputs").mkdir(exist_ok=True)
    for name in files:
        (d / "outputs" / name).write_text("x", encoding="utf-8")
    return rec.run_id


# ---------------------------------------------------------------- expected durations

def test_durations_are_the_gaps_before_actions_that_used_a_model():
    took = command.step_durations(phase_one_log())
    assert took == {"planner:plan": [30.0], "researcher:find_opportunities": [60.0], "scorer:score": [10.0],
                    "researcher:market_research": [20.0]}  # checks and notes inside a step do not split it
    log = phase_one_log() + [entry("scorer", "score", 0)]  # no model: code, not timed
    assert command.step_durations(log)["scorer:score"] == [10.0]
    assert command.step_durations([]) == {} and command.step_durations([None, {"agent": "planner"}, "x"]) == {}


def test_expected_duration_comes_from_this_run_then_other_runs_then_the_default():
    own = command.step_durations(phase_one_log())
    other_run("20261101T000000Z-older", [entry("gate", "phase1_plan_review", 500, outcome="approved"),
                                        entry("researcher", "find_opportunities", 460, MODEL),
                                        entry("scorer", "score", 455, MODEL),
                                        entry("researcher", "market_research", 440, MODEL),
                                        entry("interviewer", "interview_guide", 400, MODEL)])
    other_run("20261102T000000Z-newer", [entry("gate", "phase1_plan_review", 500, outcome="approved"),
                                        entry("researcher", "find_opportunities", 480, MODEL),
                                        entry("interviewer", "interview_guide", 460, MODEL)], status="awaiting_approval")
    other_run("20261103T000000Z-working", [entry("gate", "phase1_plan_review", 900, outcome="approved"),
                                          entry("interviewer", "interview_guide", 100, MODEL)], status="running")
    (settings.RUNS_DIR / "20261104T000000Z-broken").mkdir()
    (settings.RUNS_DIR / "20261104T000000Z-broken" / "state.json").write_text('{"status": "stop', encoding="utf-8")
    history = command.history_durations(settings.RUNS_DIR)
    assert history["interviewer:interview_guide"] == 30.0  # the median of 40 and 20; the working run is not read
    assert command.expected_seconds("researcher:find_opportunities", own, history) == (60.0, "this run")
    assert command.expected_seconds("interviewer:interview_guide", own, history) == (30.0, "earlier runs")
    assert command.expected_seconds("checker:verify", own, history) == (command.DEFAULT_STEP_SECONDS, "a typical step")
    assert command.expected_seconds("checker:verify", {}, None) == (command.DEFAULT_STEP_SECONDS, "a typical step")
    low, high = command.STEP_SECONDS_RANGE
    assert command.expected_seconds("a:b", {"a:b": [0.0]}, None)[0] == low
    assert command.expected_seconds("a:b", {}, {"a:b": 99999.0})[0] == high


def test_other_runs_are_capped_and_kept_in_memory_for_a_minute(monkeypatch):
    for i in range(12):
        other_run(f"202611{i + 10}T000000Z-run", [entry("gate", "phase1_plan_review", 100, outcome="approved"),
                                                  entry("researcher", "find_opportunities", 100 - (i + 1) * 5, MODEL)])
    first = command.history_durations(settings.RUNS_DIR)
    # the newest eight took 25 to 60 seconds; the four oldest (5 to 20) are not read
    assert first == {"researcher:find_opportunities": 42.5}
    other_run("20261130T000000Z-late", [entry("gate", "phase1_plan_review", 900, outcome="approved"),
                                        entry("researcher", "find_opportunities", 400, MODEL)])
    assert command.history_durations(settings.RUNS_DIR) is first  # within the minute: nothing is read again
    started = time.perf_counter()
    for _ in range(200):
        command.history_durations(settings.RUNS_DIR)
    assert time.perf_counter() - started < 0.5
    real = time.monotonic
    monkeypatch.setattr(command.time, "monotonic", lambda: real() + command.HISTORY_TTL + 1)
    assert command.history_durations(settings.RUNS_DIR) != first
    assert command.history_durations(settings.RUNS_DIR / "nowhere") == {}


# ---------------------------------------------------------------- the step in flight

def test_the_step_in_flight_is_the_next_unwritten_step_of_the_phase():
    flight = command.step_in_flight(phase_one_log(), FILES)
    assert (flight["agent"], flight["action"], flight["index"]) == ("interviewer", "interview_guide", 4)
    assert len(flight["steps"]) == len(orch.STEPS[orch.State.P1_DISCOVER]) + 2  # the plan, the files, the check
    unwritten = [s for s in orch.STEPS[orch.State.P1_DISCOVER] if s.output not in FILES]
    assert flight["action"] == unwritten[0].func
    g = command.progress(phase_one_log(), FILES, True)
    assert g["step_label"] == plain.STEP_NOW["interview_guide"] == "Writing the customer interview guide"
    assert (g["phase_step"], g["phase_steps"]) == (5, 6) and g["phase_words"].startswith("Step 5 of 6.")
    # with no log to follow, the files alone decide
    assert command.step_in_flight([], FILES)["action"] == "interview_guide"
    assert command.step_in_flight([], FILES[:1])["action"] == "find_opportunities"
    assert command.step_in_flight([], []) is None


def test_the_step_in_flight_follows_the_log_through_a_phase():
    log = [entry("guardrails", "screen_brief", 300, outcome="go"), entry("gate", "brief_screen_gate", 300, outcome="go")]
    assert command.step_in_flight(log)["action"] == "assess"
    log += [entry("assessor", "assess", 280, MODEL), entry("gate", "feasibility_check", 280, outcome="awaiting_founder")]
    assert command.step_in_flight(log) is None  # waiting for the founder
    log += [entry("founder", "decide:feasibility:run:1", 270, outcome="continue")]
    assert command.step_in_flight(log)["action"] == "roadmap"
    log += [entry("planner", "roadmap", 260, MODEL), entry("gate", "roadmap_check", 260, outcome="awaiting_founder")]
    assert command.step_in_flight(log) is None
    log += [entry("founder", "decide:roadmap:run:2", 250, outcome="approve")]
    assert command.step_in_flight(log)["action"] == "plan"
    log += [entry("planner", "plan", 240, MODEL), entry("gate", "phase1_plan_review", 240, outcome="approved")]
    for i, step in enumerate(orch.STEPS[orch.State.P1_DISCOVER]):
        assert command.step_in_flight(log)["action"] == step.func
        log.append(entry(step.agent, step.func, 230 - i * 10, MODEL))
    assert command.step_in_flight(log)["action"] == "verify"
    log += [entry("checker", "verify", 150, MODEL), entry("gate", "phase1_verification", 150, outcome="defects")]
    assert command.step_in_flight(log)["action"] == "find_opportunities"  # the files are made again
    log += [entry("researcher", "find_opportunities_not_written", 140, outcome="blocked")]
    assert command.step_in_flight(log)["action"] == "score"
    log += [entry("gate", "phase1_verification", 130, outcome="verified"), entry("gate", "phase1_evidence_gate", 130, outcome="go"),
            entry("gate", "milestone_review_m1", 130, outcome="awaiting_founder")]
    assert command.step_in_flight(log) is None
    log += [entry("founder", "decide:milestone_m1:run:3", 100, outcome="continue")]
    flight = command.step_in_flight(log)
    assert flight["action"] == "plan" and len(flight["steps"]) == len(orch.STEPS[orch.State.P2_VALIDATE]) + 2
    assert command.step_in_flight(log[:-1] + [entry("founder", "decide:milestone_m1:run:3", 100, outcome="request_changes")])[
        "action"] == "find_opportunities"
    assert command.step_in_flight(log[:-1] + [entry("founder", "decide:milestone_m1:run:3", 100, outcome="stop")]) is None


def test_an_unknown_step_falls_back_to_plain_words_and_no_figures():
    log = [entry("gate", "milestone_review_m1", 30, outcome="awaiting_founder"),
           entry("founder", "decide:milestone_m1:run:3", 20, outcome="pivot")]
    g = command.progress(log, [], True)
    assert g["step_label"] == "Working on the next step" and g["step_elapsed"] >= 20
    for key in ("step_expected", "step_fraction", "phase_fraction", "eta_seconds", "phase_steps"):
        assert key not in g, key
    assert command.progress([], [], True)["step_label"] == "Working on the next step"
    assert command.progress([], [], True)["step_elapsed"] is None
    assert "step_label" not in command.progress(log, [], False)  # nothing is in flight when the run is not working


# ---------------------------------------------------------------- fractions and time left

def test_the_figures_of_a_step_that_has_just_started():
    g = command.progress(phase_one_log(end=5), FILES, True, history={"interviewer:interview_guide": 40.0})
    assert g["step_elapsed"] in (5, 6) and g["step_expected"] == 40 and g["step_basis"] == "earlier runs"
    assert 0 < g["step_fraction"] <= 0.15 and g["step_overdue"] is False
    assert g["step_words"] == "Usually takes about 40 seconds."
    assert g["phase_fraction"] == pytest.approx((4 + g["step_fraction"]) / 6, abs=0.002)
    # the guide (40) and the check (the default) are left, less the seconds already spent
    assert g["eta_seconds"] in (54, 55)
    assert g["phase_words"] == f"Step 5 of 6. About {plain.about(g['eta_seconds'])} left in this phase."


@pytest.mark.parametrize("ago", [0, 1, 19, 20, 21, 59, 60, 61, 600, 86400])
def test_the_step_never_looks_finished_and_time_left_is_never_negative(ago):
    g = command.progress(phase_one_log(end=ago), FILES, True)
    assert 0 <= g["step_fraction"] <= command.STEP_FRACTION_CAP == 0.95
    assert 0 <= g["phase_fraction"] < 1
    assert g.get("eta_seconds", 1) >= 1  # left out when nothing is left; never zero, never negative
    shown = g["step_words"] + " " + g["phase_words"]
    assert not re.search(r"(^|[^0-9])-\d|\b0 (seconds|minutes)", shown), shown
    if ago > command.DEFAULT_STEP_SECONDS:
        assert g["step_overdue"] is True and g["step_words"] == "Taking longer than usual."
        assert g["phase_words"] == "Step 5 of 6."  # no time left is promised once the estimate was wrong
    elif ago < command.DEFAULT_STEP_SECONDS - 2:
        assert g["step_overdue"] is False and "Usually takes about" in g["step_words"]
        assert "About" in g["phase_words"] and "left in this phase" in g["phase_words"]


def test_rough_lengths_of_time_are_never_exact_or_zero():
    assert [plain.about(s) for s in (-5, 0, 4, 12, 20, 54, 60, 100, 170)] == [
        "a few seconds", "a few seconds", "a few seconds", "10 seconds", "20 seconds", "55 seconds", "a minute",
        "2 minutes", "3 minutes"]


# ---------------------------------------------------------------- the route and the page

def test_a_half_written_or_missing_log_gives_a_valid_fallback(strong_brief):
    client = TestClient(app)
    rid = running_run(strong_brief, phase_one_log(), FILES)
    good = client.get(f"/runs/{rid}/progress").json()
    assert good["step_label"] == "Writing the customer interview guide" and good["entries"] == len(phase_one_log())
    path = orch.run_dir(rid) / "decision_log.json"
    for broken in ('[{"timestamp": "2026-11-17T09:0', "", '{"not": "a list"}', "[1, null, \"x\"]"):
        path.write_text(broken, encoding="utf-8")
        r = client.get(f"/runs/{rid}/progress")
        assert r.status_code == 200, broken
        assert isinstance(r.json()["step_label"], str) and r.json()["step_label"]
    path.write_text('[{"timestamp": "2026-11-17T09:0', encoding="utf-8")
    assert client.get(f"/runs/{rid}/progress").json()["step_label"] == "Writing the customer interview guide"  # last readable log
    assert client.get(f"/runs/{rid}").status_code == 200

    other = running_run(strong_brief)  # a run that was never readable
    (orch.run_dir(other) / "decision_log.json").write_text('[{"timest', encoding="utf-8")
    g = client.get(f"/runs/{other}/progress").json()
    assert g["running"] is True and g["step_label"] == "Working on the next step" and g["step_elapsed"] is None
    assert g["entries"] == 0 and "step_fraction" not in g and "eta_seconds" not in g
    (orch.run_dir(other) / "decision_log.json").unlink()
    g = client.get(f"/runs/{other}/progress").json()
    assert g["step_label"] == "Working on the next step" and "step_fraction" not in g
    # a bad log of another run does not break the estimate either
    other_run("20261101T000000Z-bad", [])
    (settings.RUNS_DIR / "20261101T000000Z-bad" / "decision_log.json").write_text("[{", encoding="utf-8")
    command.clear_history_cache()
    assert client.get(f"/runs/{rid}/progress").status_code == 200


def test_the_page_and_the_json_show_the_estimate_without_internal_names(strong_brief):
    client = TestClient(app)
    rid = running_run(strong_brief, phase_one_log(), FILES)
    r = client.get(f"/runs/{rid}/progress")
    g = r.json()
    for key in ("step_label", "step_elapsed", "step_expected", "step_fraction", "phase_fraction", "eta_seconds",
                "phase_step", "phase_steps", "step_words", "phase_words"):
        assert key in g, key
    text = re.sub(r'"name": ?"[^"]*"', "", r.text)  # file names are link targets; everything else is shown
    for raw in INTERNAL:
        assert raw not in text, raw

    page = client.get(f"/runs/{rid}").text
    panel = page.split('id="working"')[1].split("</section>")[0]
    assert "Writing the customer interview guide" in panel and "Step 5 of 6." in panel
    assert "Usually takes about 20 seconds." in panel and "left in this phase." in panel
    assert panel.count('role="progressbar"') == 2 and 'class="bar known"' in panel and "100%" not in panel
    assert "estimates" in panel
    shown = re.sub(r'href="[^"]*"', "", panel)
    for raw in INTERNAL:
        assert raw not in shown, raw
    assert page.count("<script") == 1 and "prefers-reduced-motion" in page
    script = page.split("<script")[1].split("</script>")[0]
    for raw in INTERNAL + ("innerHTML", "http://", "https://"):
        assert raw not in script, raw
    assert "location.reload()" in script  # the page still reloads when the run pauses or ends

    late = running_run(strong_brief, phase_one_log(end=3600), FILES)
    panel = client.get(f"/runs/{late}").text.split('id="working"')[1].split("</section>")[0]
    assert "Taking longer than usual." in panel and 'class="bar"' in panel and 'class="bar known"' not in panel
    assert "left in this phase" not in panel


def test_progress_answers_fast_once_the_other_runs_are_in_memory(strong_brief):
    for i in range(8):
        other_run(f"202611{i + 10}T000000Z-run", phase_one_log())
    client = TestClient(app)
    rid = running_run(strong_brief, phase_one_log(), FILES)
    client.get(f"/runs/{rid}/progress")
    rec = orch.load(rid)
    import app.main as main
    started = time.perf_counter()
    for _ in range(20):
        main._progress(rec)
    assert (time.perf_counter() - started) / 20 < 0.05

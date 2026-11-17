"""promote(): the only path into outputs/ and decision_log.json. Several runs sit side by side."""

import json

import pytest

import decision_log
import orchestrator as orch
import settings
from decision_log import DecisionLog


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scratch outputs/ and decision_log.json, so the real ones are never touched by tests."""
    monkeypatch.setattr(settings, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(settings, "ROOT_LOG", tmp_path / "decision_log.json")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / ".gitkeep").write_text("", encoding="utf-8")
    decision_log.reset(tmp_path / "decision_log.json")
    (tmp_path / "briefs").mkdir()
    for f in settings.BRIEFS_DIR.glob("*.json"):  # the example briefs, so only a typed idea counts as new
        (tmp_path / "briefs" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(settings, "BRIEFS_DIR", tmp_path / "briefs")
    return tmp_path


def finished_run(brief, files, start, model="open-model", mode="live", status="stopped"):
    """A run folder as a finished live run leaves it (the pipeline itself is covered elsewhere)."""
    rec = orch.new_run(brief)
    rec.mode, rec.model, rec.status, rec.state = mode, model, status, "STOPPED"
    orch.save(rec)
    d = orch.run_dir(rec.run_id)
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / "outputs" / name).write_text(text, encoding="utf-8")
    for i, action in enumerate(["screen_brief", "assess", "feasibility_gate"]):
        decision_log.append({"timestamp": f"2026-11-17T{start}:0{i}Z", "agent": "assessor", "action": action,
                             "sources": [], "model": model if action == "assess" else None, "cost": 0,
                             "approved_by": None}, d / "decision_log.json")
    return rec.run_id


def names(repo):
    return sorted(p.name for p in (repo / "outputs").iterdir() if p.name != ".gitkeep")


def test_main_run_keeps_standard_names_and_other_runs_add_a_label(repo, strong_brief):
    strong = finished_run(strong_brief, {"phase1_gate_feasibility.md": "strong", "phase1_step2_scoring.json": "{}",
                                         "payment_test_log.md": "log"}, "10:00")
    weak = finished_run(strong_brief, {"phase1_gate_feasibility.md": "weak"}, "09:00")
    orch.promote(strong)
    written = orch.promote(weak, label="weak")
    assert [p.name for p in written] == ["phase1_gate_feasibility_weak.md"]
    assert names(repo) == ["payment_test_log.md", "phase1_gate_feasibility.md", "phase1_gate_feasibility_weak.md",
                           "phase1_step2_scoring.json"]
    assert (repo / "outputs" / "phase1_gate_feasibility.md").read_text(encoding="utf-8") == "strong"  # not overwritten
    assert (repo / "outputs" / "phase1_gate_feasibility_weak.md").read_text(encoding="utf-8") == "weak"


def test_log_holds_every_promoted_run_in_time_order(repo, strong_brief):
    later = finished_run(strong_brief, {"phase1_gate_feasibility.md": "a"}, "10:00")
    earlier = finished_run(strong_brief, {"phase1_gate_feasibility.md": "b"}, "09:00")
    orch.promote(later)
    orch.promote(earlier, label="thin_evidence")
    entries = json.loads((repo / "decision_log.json").read_text(encoding="utf-8"))
    stamps = [e["timestamp"] for e in entries]
    assert len(entries) == 6 and stamps == sorted(stamps) and stamps[0].startswith("2026-11-17T09:00")
    for e in entries:
        decision_log.validate(e)


def test_a_second_run_never_overwrites_the_first(repo, strong_brief):
    first = finished_run(strong_brief, {"phase1_gate_feasibility.md": "first"}, "10:00")
    second = finished_run(strong_brief, {"phase1_gate_feasibility.md": "second"}, "11:00")
    orch.promote(first)
    with pytest.raises(orch.PromoteError, match="label"):
        orch.promote(second)
    assert (repo / "outputs" / "phase1_gate_feasibility.md").read_text(encoding="utf-8") == "first"
    assert len(decision_log.read(repo / "decision_log.json")) == 3  # nothing was added


def test_the_same_run_is_not_promoted_twice(repo, strong_brief):
    run = finished_run(strong_brief, {"phase1_gate_feasibility.md": "x"}, "10:00")
    orch.promote(run)
    with pytest.raises(orch.PromoteError, match="already"):
        orch.promote(run, label="again")
    assert names(repo) == ["phase1_gate_feasibility.md"]


def test_replace_starts_outputs_and_the_log_over(repo, strong_brief):
    old = finished_run(strong_brief, {"phase1_gate_feasibility.md": "old", "phase1_step1_opportunities.md": "old"}, "09:00")
    new = finished_run(strong_brief, {"phase1_gate_feasibility.md": "new"}, "10:00")
    orch.promote(old)
    orch.promote(new, replace=True)
    assert names(repo) == ["phase1_gate_feasibility.md"] and (repo / "outputs" / ".gitkeep").exists()
    assert (repo / "outputs" / "phase1_gate_feasibility.md").read_text(encoding="utf-8") == "new"
    assert [e["timestamp"][11:16] for e in decision_log.read(repo / "decision_log.json")] == ["10:00"] * 3


@pytest.mark.parametrize("label", ["Weak", "weak brief", "../x", "weak-brief", "_weak", ""])
def test_bad_labels_are_refused(repo, strong_brief, label):
    run = finished_run(strong_brief, {"phase1_gate_feasibility.md": "x"}, "10:00")
    with pytest.raises(orch.PromoteError):
        orch.promote(run, label=label)
    assert names(repo) == []


def test_mock_and_unfinished_runs_are_refused(repo, strong_brief):
    mock = finished_run(strong_brief, {"phase1_gate_feasibility.md": "x"}, "10:00", mode="mock")
    with pytest.raises(orch.PromoteError, match="mock"):
        orch.promote(mock)
    mocked_model = finished_run(strong_brief, {"phase1_gate_feasibility.md": "x"}, "10:00", model="MOCKED-llm")
    with pytest.raises(orch.PromoteError, match="mock"):
        orch.promote(mocked_model)
    running = finished_run(strong_brief, {"phase1_gate_feasibility.md": "x"}, "10:00", status="running")
    with pytest.raises(orch.PromoteError, match="in progress"):
        orch.promote(running)
    assert names(repo) == [] and decision_log.read(repo / "decision_log.json") == []


def test_labelled_name():
    assert orch.labelled_name("phase1_gate_decision.md", "thin_evidence") == "phase1_gate_decision_thin_evidence.md"
    assert orch.labelled_name("phase2_step2_landing_page.html", "weak") == "phase2_step2_landing_page_weak.html"
    assert orch.labelled_name("payment_test_log.md", None) == "payment_test_log.md"


def test_a_typed_idea_is_kept_under_briefs_when_its_run_is_promoted(repo, strong_brief):
    own = {"idea": "A checklist that helps first-time conference speakers prepare their talk", "skills": ["writing"],
           "budget_usd": 40.0, "hours_per_week": 6.0, "target_market": "Engineers giving their first talk", "values": []}
    example = finished_run(strong_brief, {"phase1_gate_feasibility.md": "strong"}, "10:00")
    typed = finished_run(dict(own), {"phase1_gate_feasibility.md": "own"}, "11:00")
    orch.promote(example)
    assert not (repo / "briefs" / "promoted").exists()  # an example brief is in briefs/ already
    orch.promote(typed, label="own_idea")
    kept = repo / "briefs" / "promoted" / "own_idea.json"
    assert json.loads(kept.read_text(encoding="utf-8")) == own
    assert orch.brief_file(own) == kept and orch.keep_brief(orch.load(typed), "another_label") == kept  # kept once

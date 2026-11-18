"""Every output file opens with a header written by code, a gate file states its decision first,
and a claim line ends with its source or the word ASSUMPTION. Offline, with scripted model replies."""

import json
import re

import gates
import orchestrator as orch
import sources
from test_agents import outputs, web  # noqa: F401  (web is a fixture)
from test_phase345 import ALL, drive

DECISIONS = ("GO", "REPEAT", "PIVOT", "STOP", "PENDING")
# The gate decision is verified by code now (no checker model call), so its header names no model.
NO_MODEL_FILES = ["phase1_gate_brief_screen.md", "phase2_step3_demand_test.md", "phase3_gate_tests.md",
                  "payment_test_log.md", "phase5_step4_publish_log.md", "phase1_gate_decision.md"]
MODEL_FILES = ["phase1_gate_feasibility.md", "phase1_gate_roadmap.md", "phase1_step1_opportunities.md",
               "phase1_gate_plan_review.md", "phase3_step2_product.md"]


def full_run(strong_brief):
    rec, seen = drive(strong_brief, ALL, calls=[])
    assert [s[0] for s in seen][:7] == ["feasibility", "roadmap", "M1", "M2", "M3", "M4", "M5"], rec.stop_reasons or rec.blocked_reason
    return rec, {p.name: p.read_text(encoding="utf-8") for p in outputs(rec.run_id).iterdir()}


def first_line(text):
    return text.split("\n", 1)[0]


def test_every_output_file_opens_with_one_header_written_by_code(web, strong_brief):  # noqa: F811
    rec, files = full_run(strong_brief)
    assert len(files) >= 25
    for name, text in files.items():
        if name.endswith(".json"):
            header = json.loads(text)[orch.HEADER_KEY]
            assert list(json.loads(text))[0] == orch.HEADER_KEY and set(header) == {"file", "agent", "model", "run_started"}
            assert header["run_started"] == rec.created_at and header["file"].startswith("Phase 1, step 2")
            continue
        line = first_line(text)
        assert orch.has_header(text), name
        assert line.startswith("<!-- " if name.endswith(".html") else "> "), name
        assert f"| Run started: {rec.created_at}" in line and text.count(" | Run started: ") == 1, name  # appended to, not repeated
        assert line.lstrip("> <!-").startswith(orch.file_place(name) + " | Agent: "), name
    assert first_line(files["phase1_gate_feasibility.md"]) == (
        f"> Phase 1, gate: feasibility | Agent: assessor | Model: {rec.model} | Run started: {rec.created_at}")
    for name in NO_MODEL_FILES:
        assert f"| Model: {orch.NO_MODEL} |" in first_line(files[name]), name
    for name in MODEL_FILES:
        assert f"| Model: {rec.model} |" in first_line(files[name]), name


def test_reading_a_file_back_gives_the_text_without_the_header():
    text = orch.with_header("phase3_step2_product.md", "Draft: x\n\n# Guide\n", "builder", "m", "2026-11-17T09:00:00Z")
    assert text.startswith("> Phase 3, step 2: product | Agent: builder | Model: m | Run started: 2026-11-17T09:00:00Z\n\nDraft: x")
    assert orch.strip_header(text) == "Draft: x\n\n# Guide\n"
    assert orch.with_header("phase3_step2_product.md", text, "builder", "m", "later") == text  # never a second header
    page = orch.with_header("phase5_step1_store_page.html", "<!doctype html><p>x</p>", "launcher", None, "t")
    assert page.startswith("<!-- Phase 5, step 1: store page | Agent: launcher | Model: none (written by code) | Run started: t -->\n<!doctype")
    assert orch.strip_header(page) == "<!doctype html><p>x</p>"
    assert orch.strip_header("# No header here\n") == "# No header here\n"
    scoring = json.loads(orch.with_header("phase1_step2_scoring.json", '{"pick": "O1"}', "scorer", "m", "t"))
    assert scoring == {"_header": {"file": "Phase 1, step 2: scoring", "agent": "scorer", "model": "m", "run_started": "t"}, "pick": "O1"}


def test_every_gate_file_states_the_decision_on_the_first_line_after_the_header(web, strong_brief):  # noqa: F811
    rec, files = full_run(strong_brief)
    gate_files = {n: t for n, t in files.items() if re.fullmatch(r"phase\d+_gate_\w+\.md", n)}
    assert len(gate_files) >= 14
    for name, text in gate_files.items():
        line = first_line(orch.strip_header(text))
        m = re.match(r"Decision: ([A-Z]+)\.", line)
        assert m and m.group(1) in DECISIONS, (name, line)
        assert text.count("\nDecision: ") == 1, name  # one decision line, at the top
    assert first_line(orch.strip_header(files["phase1_gate_roadmap.md"])) == "Decision: GO. Reasons: the founder approved the roadmap."
    m1 = orch.strip_header(files["phase1_gate_decision.md"])
    assert first_line(m1).startswith("Decision: GO. Reasons: all evidence thresholds met") and first_line(m1).endswith("Founder chose: continue.")
    assert m1.index("| Check | Result | Detail |") < m1.index("## Verification report (checker)") < m1.index("## Founder decision")
    assert first_line(orch.strip_header(files["phase1_gate_plan_review.md"])).startswith("Decision: GO. Reasons: every criterion has")


def test_a_stop_states_its_reasons_on_the_first_line():
    gate = gates.GateResult("phase1_evidence", gates.Outcome.STOP,
                            ["no paying source with a price was found", "sources come from 1 domain, 3 are needed"])
    text = gate.to_markdown("Phase 1 gate")
    assert first_line(text) == ("Decision: STOP. Reasons: no paying source with a price was found; "
                                "sources come from 1 domain, 3 are needed.")
    assert gates.decision_line("go") == "Decision: GO."
    long = gates.decision_line("stop", ["word " * 200])
    assert len(long) < 460 and long.endswith(" ....")


def test_claim_lines_end_with_the_source_url_or_the_word_assumption(web, strong_brief):  # noqa: F811
    assert sources.claim_line("Translators track deadlines by hand.", "https://a.example/x") == (
        "- Translators track deadlines by hand. Source: https://a.example/x")
    assert sources.claim_line("They would pay for a tracker", None) == "- They would pay for a tracker. ASSUMPTION"
    assert sources.claim_line("Is there demand?", None) == "- Is there demand? ASSUMPTION"
    _, files = full_run(strong_brief)
    ending = re.compile(r"(?:https?://\S+|ASSUMPTION)$")
    for name in ["phase1_step1_opportunities.md", "phase1_step3_market.md", "phase2_step1_lean_canvas.md",
                 "phase4_step1_pricing.md"]:
        claims = [l for l in files[name].splitlines() if l.lstrip().startswith("- ") and ("Source: http" in l or "ASSUMPTION" in l)]
        assert claims, name
        for line in claims:
            assert ending.search(line.rstrip()), (name, line)
        assert "[source](" not in files[name] and "**ASSUMPTION**" not in files[name], name

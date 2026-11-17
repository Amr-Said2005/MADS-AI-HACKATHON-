"""The optional site step of the Build phase: off by default, text products only, checked by the gate. Offline."""

import site_builder
import orchestrator as orch
import settings
from agents import builder
from test_agents import outputs, web  # noqa: F401  (web is a fixture)
from test_phase345 import ALL, drive

COPY = {"tagline": "A deadline board for freelance translators", "about": "Templates that keep clients, deadlines and word counts in one place.",
        "summary": "One board for every client deadline.", "for_whom": "Freelance translators who work with several agencies."}


def test_the_site_step_is_off_by_default(web, strong_brief):  # noqa: F811
    assert settings.load().site_templates is False
    rec, _ = drive(strong_brief, ALL, calls=[])
    assert not [p.name for p in outputs(rec.run_id).iterdir() if p.name.startswith(site_builder.OUTPUT_PREFIX)]
    assert orch.SITE_STEP.key not in rec.artifacts


def test_a_text_product_gets_a_site_that_passes_its_checks(web, strong_brief, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SITE_TEMPLATES", "true")
    calls = []
    rec, seen = drive(strong_brief, dict(ALL, SiteCopy=COPY), calls=calls)
    assert [s[0] for s in seen][:5] == ["feasibility", "roadmap", "M1", "M2", "M3"], rec.stop_reasons or rec.blocked_reason
    pages = {p.name: p.read_text(encoding="utf-8") for p in outputs(rec.run_id).iterdir() if p.name.startswith(site_builder.OUTPUT_PREFIX)}
    assert site_builder.OUTPUT_PREFIX + "_home.html" in pages and any("_item_" in n for n in pages) and any("_lesson_" in n for n in pages)
    assert site_builder.check_site({n: orch.strip_header(t) for n, t in pages.items()}) == []
    for name, text in pages.items():
        assert orch.has_header(text) and "Draft: facts not verified. Founder must review before use." in text, name
        assert "<script" not in text and "<form" not in text
    item = next(t for n, t in pages.items() if "_item_" in n)
    assert "MOCKED" in item and "No payment is taken" in item and "Price not set yet" in item
    assert "A deadline board for freelance translators" in pages[site_builder.OUTPUT_PREFIX + "_home.html"]
    tests = (outputs(rec.run_id) / "phase3_gate_tests.md").read_text(encoding="utf-8")
    assert "| the site pages pass their checks | pass |" in tests
    assert [c[1] for c in calls].count("SiteCopy") == 1  # one model call; the model writes no HTML
    copy_call = next(c for c in calls if c[1] == "SiteCopy")
    assert "<html" not in copy_call[2][-1]["content"].lower()


def test_a_software_product_gets_no_site(web, strong_brief, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SITE_TEMPLATES", "true")
    monkeypatch.setattr(builder, "site_applies", lambda ctx: False)
    rec, _ = drive(strong_brief, ALL, calls=[])
    assert not [p.name for p in outputs(rec.run_id).iterdir() if p.name.startswith(site_builder.OUTPUT_PREFIX)]
    entries = orch.DecisionLog(orch.run_dir(rec.run_id) / "decision_log.json").entries()
    assert any(e["action"] == "skipped_build_site" and e["outcome"] == "not_applicable" for e in entries)


def test_the_site_applies_to_text_families_only():
    class Ctx:
        def __init__(self, family):
            self.family = family

        def artifact(self, key):
            return {"data": {"family": self.family}}

    assert [builder.site_applies(Ctx(f)) for f in ("Written content", "Courses and learning", "Templates and tools")] == [True] * 3
    assert [builder.site_applies(Ctx(f)) for f in ("Software", "Media and design", "Access", "unsure")] == [False] * 4

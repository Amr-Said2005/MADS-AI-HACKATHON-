"""Offline tests for site_builder.py and the templates in site_templates/."""
import copy
import re
from pathlib import Path

import pytest

import site_builder as sb

CONTENT = {
    "site": {
        "name": "Circuit Notes",
        "tagline": "Clear notes and short courses for electrical engineering students",
        "audience": "Second and third year electrical engineering students",
        "about": "Notes and courses written to match what is taught in circuit analysis and signals courses.",
    },
    "items": [
        {
            "kind": "notes", "title": "Circuit Analysis Notes",
            "summary": "One page per topic, with worked examples.",
            "for_whom": "Students preparing for a first circuits exam.",
            "whats_inside": ["Nodal and mesh analysis", "Thevenin and Norton", "First-order transients"],
            "lessons": [
                {"title": "Nodal analysis", "paragraphs": ["Pick a reference node.", "Write one equation per node."]},
                {"title": "Mesh analysis", "paragraphs": ["Assign a current to each mesh."]},
            ],
        },
        {
            "kind": "course", "title": "Signals in Ten Lessons", "price_usd": 25,
            "summary": "A short course on signals and systems.",
            "for_whom": "Students who want a guided path.",
            "whats_inside": ["Ten lessons", "Practice questions"],
        },
    ],
    "faq": [{"q": "Is this a finished product?", "a": "No. It is a draft for the founder to review."}],
}


def build(**changes):
    content = copy.deepcopy(CONTENT)
    content.update(changes)
    return sb.build_site(content)


def test_every_page_is_built_with_convention_names():
    pages = build()
    assert set(pages) == {
        "phase3_step4_site_home.html",
        "phase3_step4_site_item_circuit_analysis_notes.html",
        "phase3_step4_site_item_signals_in_ten_lessons.html",
        "phase3_step4_site_lesson_circuit_analysis_notes_1.html",
        "phase3_step4_site_lesson_circuit_analysis_notes_2.html",
    }
    assert all(re.fullmatch(r"phase3_step4_[a-z0-9_]+\.html", name) for name in pages)


def test_built_site_passes_its_own_checks():
    assert sb.check_site(build()) == []


def test_every_page_carries_the_draft_banner():
    assert all("Draft: facts not verified" in html for html in build().values())


def test_buy_control_is_labelled_mocked_and_takes_no_payment():
    html = build()["phase3_step4_site_item_signals_in_ten_lessons.html"]
    assert "MOCKED" in html and "No payment is taken" in html and "$25" in html
    assert "<form" not in html and "<input" not in html and "<button" not in html


def test_missing_price_is_stated_not_invented():
    html = build()["phase3_step4_site_item_circuit_analysis_notes.html"]
    assert "Price not set yet" in html and "$" not in html


def test_content_cannot_inject_markup():
    content = copy.deepcopy(CONTENT)
    content["site"]["about"] = 'Great notes <script>alert(1)</script> & "more"'
    content["items"][0]["lessons"][0]["paragraphs"] = ['<img src=x onerror="alert(1)">Use V = IR']
    pages = sb.build_site(content)
    joined = "\n".join(pages.values())
    assert "<script" not in joined and "onerror" not in joined
    assert "Great notes alert(1) &amp; &#34;more&#34;" in pages["phase3_step4_site_home.html"]
    assert sb.check_site(pages) == []


def test_internal_links_and_lesson_order():
    pages = build()
    first = pages["phase3_step4_site_lesson_circuit_analysis_notes_1.html"]
    second = pages["phase3_step4_site_lesson_circuit_analysis_notes_2.html"]
    assert "Previous" not in first and "phase3_step4_site_lesson_circuit_analysis_notes_2.html" in first
    assert "Next" not in second and "phase3_step4_site_lesson_circuit_analysis_notes_1.html" in second


def test_templates_hold_no_social_proof_or_urgency_blocks():
    text = "\n".join(p.read_text() for p in Path(sb.TEMPLATE_DIR).glob("*.j2")).lower()
    for word in ("testimonial", "review", "countdown", "limited time", "customers say", "earn", "guarantee", "sign-ups"):
        assert word not in text.replace("founder's review", "").replace("must review", ""), word
    assert "<script" not in text and "http://" not in text and "https://" not in text


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["site"].update(name=""), "site.name is missing"),
    (lambda c: c.update(items=[]), "items must hold"),
    (lambda c: c["items"][0].update(kind="software"), "kind must be one of"),
    (lambda c: c["items"][0].update(whats_inside=["only one"]), "whats_inside must hold"),
    (lambda c: c["items"][1].update(title="Circuit Analysis Notes"), "slug repeats"),
    (lambda c: c["items"][0]["lessons"][0].update(paragraphs=[]), "paragraphs is missing"),
    (lambda c: c["items"][1].update(price_usd="cheap"), "price_usd must be a number"),
])
def test_bad_content_is_rejected_with_a_clear_message(mutate, message):
    content = copy.deepcopy(CONTENT)
    mutate(content)
    with pytest.raises(sb.SiteContentError, match=message):
        sb.build_site(content)


def test_check_site_reports_problems():
    pages = build()
    name = "phase3_step4_site_home.html"
    pages[name] = pages[name].replace("Draft: facts not verified", "").replace(
        'href="phase3_step4_site_item_signals_in_ten_lessons.html"', 'href="missing.html"')
    problems = sb.check_site(pages)
    assert any("draft banner missing" in p for p in problems)
    assert any("broken link to missing.html" in p for p in problems)

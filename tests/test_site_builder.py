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


# ---- the sharper templates: themes, logo mark, lesson bodies, price card, no outside resources ----

BODY = """Ohm's law links voltage, current and resistance. Use **V = IR** with `R` in ohms.

### Steps
1. Label <b>every</b> node
2. Write KCL & solve

- First bullet with a <i>tag</i>
- Second bullet
  carries on here

| Quantity | Unit |
|---|---|
| Voltage & EMF | volt |
| Current | ampere |
"""


def lesson_page(lesson):
    content = copy.deepcopy(CONTENT)
    content["items"][0]["lessons"] = [lesson, {"title": "Mesh analysis", "paragraphs": ["Assign a current to each mesh."]}]
    pages = sb.build_site(content)
    assert sb.check_site(pages) == []
    return pages["phase3_step4_site_lesson_circuit_analysis_notes_1.html"]


def test_lesson_body_renders_lists_tables_bold_and_code_with_text_escaped():
    html = lesson_page({"title": "Nodal analysis", "body": BODY})
    prose = re.search(r'<article class="prose">(.*?)</article>', html, re.S).group(1)
    assert "<strong>V = IR</strong>" in prose and "<code>R</code>" in prose and "Ohm&#39;s law" in prose
    assert re.search(r"<h2>Steps</h2>", prose)
    assert re.search(r"<ol>\s*<li>Label every node</li>\s*<li>Write KCL &amp; solve</li>\s*</ol>", prose)
    assert re.search(r"<ul>\s*<li>First bullet with a tag</li>\s*<li>Second bullet carries on here</li>\s*</ul>", prose)
    assert '<th scope="col">Quantity</th><th scope="col">Unit</th>' in prose
    assert "<td>Voltage &amp; EMF</td><td>volt</td>" in prose and "---" not in prose
    assert set(re.findall(r"<(\w+)", prose)) <= {"p", "h2", "ul", "ol", "li", "div", "table", "thead", "tbody", "tr", "th", "td",
                                                  "strong", "em", "code", "pre"}


def test_lesson_text_is_escaped_before_any_tag_is_built():
    for text in ['**bold "quoted" & <u>x</u>** then `a<b>c` and 5 > 3', "- item <script>alert(1)</script> & more\n- two"]:
        html = lesson_page({"title": "T", "paragraphs": [text]})
        assert "<script" not in html and "<u>" not in html
    assert sb.inline('a "q" & <i> **b**') == 'a &#34;q&#34; &amp; &lt;i&gt; <strong>b</strong>'
    assert sb.inline("`**not bold**` and 2 * 3 * 4") == "<code>**not bold**</code> and 2 * 3 * 4"
    assert sb.inline("see [the notes](https://example.com/x)") == "see the notes"


def test_lists_and_tables_collapsed_onto_one_line_are_recovered():
    html = lesson_page({"title": "Structure", "paragraphs": [
        "- **Semester**: Fall 2024 - **Course Code**: EE 101 - **Topic**: sub-sections",
        "1. Pick a node 2. Write the equation 3. Solve",
        "| A | B | |---|---| | 1 | 2 |",
        "Plain text - with a dash, and 2. nothing else."]})
    assert html.count("<li>") == 6 + 2 and "<li><strong>Course Code</strong>: EE 101</li>" in html  # 2 in the contents list
    assert "<li>Write the equation</li>" in html and '<th scope="col">A</th>' in html and "<td>1</td><td>2</td>" in html
    assert "<p>Plain text - with a dash, and 2. nothing else.</p>" in html


def test_lesson_page_has_progress_contents_and_neighbours():
    pages = build()
    second = pages["phase3_step4_site_lesson_circuit_analysis_notes_2.html"]
    assert "Lesson 2 of 2" in second and 'aria-label="Contents"' in second
    assert re.search(r'aria-current="page">Mesh analysis</a>', second) and second.count('aria-current="') == 1
    assert "Lesson 1 of 2" in pages["phase3_step4_site_lesson_circuit_analysis_notes_1.html"]


def test_a_lesson_with_nothing_to_show_is_rejected_and_an_emptied_page_is_reported():
    content = copy.deepcopy(CONTENT)
    content["items"][0]["lessons"][0] = {"title": "Empty", "paragraphs": ["---"]}
    with pytest.raises(sb.SiteContentError, match="no readable content"):
        sb.build_site(content)
    pages = build()
    name = "phase3_step4_site_lesson_circuit_analysis_notes_1.html"
    pages[name] = re.sub(r'(<article class="prose">).*?(</article>)', r"\1 \2", pages[name], flags=re.S)
    home = "phase3_step4_site_home.html"
    pages[home] = re.sub(r'(<ul class="grid">).*?(</ul>)', r"\1\2", pages[home], count=1, flags=re.S)
    problems = sb.check_site(pages)
    assert f"{name}: empty lesson" in problems and f"{home}: empty section (What is inside)" in problems


def test_price_card_shows_the_price_or_the_not_set_state():
    pages = build()
    priced = pages["phase3_step4_site_item_signals_in_ten_lessons.html"]
    unpriced = pages["phase3_step4_site_item_circuit_analysis_notes.html"]
    assert '<p class="price">$25</p>' in priced and "Price not set yet" not in priced
    assert '<p class="price unset">Price not set yet</p>' in unpriced and "decided at the price and policies step" in unpriced
    for html in (priced, unpriced):
        assert '<span class="buy" aria-disabled="true">Buy</span>' in html and '<span class="mocked">MOCKED</span>' in html
    content = copy.deepcopy(CONTENT)
    content["items"][1]["price_usd"] = 12.5
    assert '<p class="price">$12.50</p>' in sb.build_site(content)["phase3_step4_site_item_signals_in_ten_lessons.html"]


def test_theme_and_logo_are_chosen_by_code_and_stay_the_same():
    assert sb.pick_theme("Circuit Notes") == sb.pick_theme("  circuit   notes ") and sb.pick_theme("Circuit Notes") in sb.THEMES
    assert 4 <= len(sb.THEMES) <= 5
    assert len({sb.pick_theme(f"Site {n}") for n in range(60)}) == len(sb.THEMES)  # every preset is reachable
    assert sb.pick_theme("Circuit Notes", "Plum") == "plum" and sb.pick_theme("Circuit Notes", "neon") == sb.pick_theme("Circuit Notes")
    assert build() == build()
    home = build()["phase3_step4_site_home.html"]
    accent = sb.THEMES[sb.pick_theme("Circuit Notes")]
    assert f"--accent:{accent['light']['accent']};" in home and f"--accent:{accent['dark']['accent']};" in home
    assert "prefers-color-scheme: dark" in home and "clamp(" in home
    content = copy.deepcopy(CONTENT)
    content["site"]["theme"] = "forest"
    assert f"--accent:{sb.THEMES['forest']['light']['accent']};" in sb.build_site(content)["phase3_step4_site_home.html"]
    assert sb.monogram("Circuit Notes") == "CN" and sb.monogram("<b>") == "B" and sb.monogram("") == "?"
    assert sb.logo_svg("Circuit Notes") == sb.logo_svg("Circuit Notes") and ">CN</text>" in home
    assert sb.icon_name("Practice questions") == "pencil" and sb.icon_name("zzz") == sb.icon_name("zzz") and sb.icon_name("zzz") in sb.ICONS


def _luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    red, green, blue = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", sorted(sb.THEMES))
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_text_colours_have_enough_contrast_in_both_themes(theme, mode):
    c = {**sb.BASE[mode], **sb.THEMES[theme][mode]}
    pairs = [("ink", "bg"), ("ink", "surface"), ("muted", "bg"), ("muted", "surface"), ("note-ink", "note-bg"),
             ("accent", "bg"), ("accent", "surface"), ("accent", "tint"), ("on-accent", "accent"), ("ink", "tint")]
    for text, background in pairs:
        assert _contrast(c[text], c[background]) >= 4.5, (theme, mode, text, background)


def test_no_page_requests_anything_from_another_host():
    content = copy.deepcopy(CONTENT)
    content["items"][0]["lessons"][0] = {
        "title": "Links", "body": "Read https://example.com/a and [this](http://example.com/b.png).\n\n![x](http://example.com/c.png)"}
    pages = sb.build_site(content)
    assert sb.check_site(pages) == []
    for name, html in pages.items():
        assert not re.search(r"<(?:img|link|iframe|script|object|embed|video|audio|source|base)\b", html, re.I), name
        assert not re.search(r"""\s(?:src|href|srcset|action|data|poster)\s*=\s*["']?\s*(?:[a-z][a-z0-9+.-]*:|//)""", html, re.I), name
        assert not re.search(r"url\(|@import|@font-face|xmlns", html, re.I), name
        assert len(html.encode("utf-8")) < 40_000, name
    bad = dict(pages)
    bad["phase3_step4_site_home.html"] = bad["phase3_step4_site_home.html"].replace("</style>", '@import "//example.com/x.css";</style>')
    assert any("style loads from an outside address" in p for p in sb.check_site(bad))


def test_every_page_has_the_accessibility_basics():
    for name, html in build().items():
        assert html.count("<h1") == 1, name
        assert '<html lang="en">' in html and 'name="viewport" content="width=device-width, initial-scale=1"' in html
        assert '<a class="skip" href="#main">' in html and '<main id="main"' in html
        assert html.count("<header") == 1 and html.count("<footer") == 1 and "<nav" in html
        assert ":focus-visible" in html and "position: sticky" in html
        ids = re.findall(r'\sid="([^"]+)"', html)
        assert len(ids) == len(set(ids)), name
        assert set(re.findall(r'aria-labelledby="([^"]+)"', html)) <= set(ids), name


def test_home_page_sections():
    home = build()["phase3_step4_site_home.html"]
    for heading in ("About", "What is inside", "What you can get", "How it works", "Questions"):
        assert re.search(rf"<h2[^>]*>{heading}</h2>", home), heading
    assert '<a class="button" href="phase3_step4_site_item_circuit_analysis_notes.html">See what is inside' in home
    assert home.count('<svg class="icon"') >= 5 and "<details" in home and "MOCKED" in home
    assert "Questions</h2>" not in build(faq=[])["phase3_step4_site_home.html"]
    custom = build(steps=[{"title": "Open <b>it</b>", "text": "Read & use it."}])["phase3_step4_site_home.html"]
    assert "<h3>Open it</h3><p>Read &amp; use it.</p>" in custom and "Pick what you need" not in custom


@pytest.mark.parametrize("price", [float("nan"), float("inf"), "nan", "1e999"])
def test_a_price_that_is_not_a_finite_number_is_refused_not_a_crash(price):
    content = copy.deepcopy(CONTENT)
    content["items"][1]["price_usd"] = price
    with pytest.raises(sb.SiteContentError):
        sb.build_site(content)


def test_a_numbered_list_with_blank_lines_between_entries_stays_one_list():
    content = copy.deepcopy(CONTENT)
    content["items"][0]["lessons"][0]["paragraphs"] = ["1. Pick a node.", "2. Write the equation.", "3. Solve."]
    pages = sb.build_site(content)
    html = pages[sb.OUTPUT_PREFIX + "_lesson_circuit_analysis_notes_1.html"]
    assert html.count("<ol>") + html.count("<ol ") == 2  # the lesson list and the contents sidebar
    assert sb.parse_blocks("- a\n\n- b\n\nText after.")[0]["items"] == ["a", "b"]
    assert [b["type"] for b in sb.parse_blocks("- a\n\n- b\n\nText after.")] == ["ul", "p"]


def test_a_numbered_list_that_starts_later_keeps_its_first_number():
    content = copy.deepcopy(CONTENT)
    content["items"][0]["lessons"][0]["body"] = "Steps so far are done.\n\n3. Solve the equations.\n4. Check the answer."
    html = sb.build_site(content)[sb.OUTPUT_PREFIX + "_lesson_circuit_analysis_notes_1.html"]
    assert '<ol start="3">' in html

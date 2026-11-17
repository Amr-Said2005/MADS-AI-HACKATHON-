"""Offline tests for market_research.py. No network and no model are used."""
import sys
import types

import pytest

import market_research as mr

TERMS = mr.BriefTerms(
    customer="electrical engineering students",
    problem="circuit analysis",
    product="digital notes and courses",
    topic="electrical engineering",
)


# ---- roles ---------------------------------------------------------------

def test_wikipedia_is_reference_even_from_general_search():
    assert mr.role_for_url("https://en.wikipedia.org/wiki/World_Wide_Web", mr.ROLE_GENERAL) == mr.ROLE_REFERENCE


def test_forum_is_community_and_shop_is_general():
    assert mr.role_for_url("https://electronics.stackexchange.com/q/1", mr.ROLE_GENERAL) == mr.ROLE_COMMUNITY
    assert mr.role_for_url("https://www.example-courses.com/circuits", mr.ROLE_GENERAL) == mr.ROLE_GENERAL


# ---- queries -------------------------------------------------------------

def test_queries_use_the_brief_words_and_cover_each_purpose():
    queries = mr.build_queries(TERMS)
    texts = [q.text for q in queries]
    purposes = {q.purpose for q in queries}
    assert purposes == {mr.PURPOSE_SAY, mr.PURPOSE_PAY, mr.PURPOSE_MARKETPLACE}
    assert "electrical engineering students struggling with circuit analysis" in texts
    assert any("price" in t and "digital notes and courses" in t for t in texts)
    assert any(t.endswith("Udemy") for t in texts)
    assert len(texts) == len(set(texts))


def test_empty_brief_fields_produce_no_broken_queries():
    queries = mr.build_queries(mr.BriefTerms(customer="", problem="", product="study notes"))
    assert queries and all("  " not in q.text and q.text == q.text.strip() for q in queries)
    assert all(q.purpose != mr.PURPOSE_SAY for q in queries)


def test_model_queries_need_three_words_and_a_brief_word():
    assert not mr.accept_model_query("online platform", TERMS)
    assert not mr.accept_model_query("history of the internet", TERMS)
    assert mr.accept_model_query("circuit analysis exam help", TERMS)


def test_at_most_two_model_queries_are_added():
    base = mr.build_queries(TERMS)
    out = mr.add_model_queries(base, ["circuit analysis exam help", "what is the web",
                                      "engineering students notes pdf", "circuit analysis tutor cost"], TERMS)
    assert len(out) == len(base) + 2


# ---- search --------------------------------------------------------------

def _adapter(name, role, results=None, error=None, calls=None):
    def fn(query, max_results):
        if calls is not None:
            calls.append((name, query))
        if error:
            raise error
        return results or []
    return mr.Adapter(name, role, fn)


def test_general_adapters_are_tried_in_order_with_fallback():
    calls = []
    first = _adapter("ddgs", mr.ROLE_GENERAL, error=RuntimeError("rate limited"), calls=calls)
    second = _adapter("searxng", mr.ROLE_GENERAL,
                      results=[{"title": "Circuits course", "href": "https://shop.example.com/c", "body": "$49"}],
                      calls=calls)
    report = mr.search([mr.Query("circuit analysis course price", mr.PURPOSE_PAY)], [first, second])
    assert [c[0] for c in calls] == ["ddgs", "searxng"]
    assert [r.adapter for r in report.results] == ["searxng"]
    assert report.failures[0]["adapter"] == "ddgs" and "rate limited" in report.failures[0]["reason"]


def test_community_adapters_answer_only_say_queries():
    calls = []
    general = _adapter("ddgs", mr.ROLE_GENERAL, results=[{"title": "t", "href": "https://a.example.com/1"}])
    forum = _adapter("hn", mr.ROLE_COMMUNITY, results=[{"title": "t", "url": "https://news.ycombinator.com/item?id=1"}],
                     calls=calls)
    mr.search([mr.Query("notes price", mr.PURPOSE_PAY), mr.Query("students struggling with circuits", mr.PURPOSE_SAY)],
              [general, forum])
    assert calls == [("hn", "students struggling with circuits")]


def test_duplicates_are_removed_and_domains_are_capped():
    items = [{"title": str(i), "href": f"https://one.example.com/{i}"} for i in range(5)]
    items.append({"title": "dup", "href": "https://one.example.com/0"})
    report = mr.search([mr.Query("q one two", mr.PURPOSE_PAY)], [_adapter("ddgs", mr.ROLE_GENERAL, results=items)])
    assert len(report.results) == 2


def test_nothing_collected_is_reported_with_reasons():
    report = mr.search(mr.build_queries(TERMS), [_adapter("ddgs", mr.ROLE_GENERAL),
                                                 _adapter("searxng", mr.ROLE_GENERAL, error=TimeoutError("timed out"))])
    assert report.collected_nothing
    reasons = {f["reason"] for f in report.failures}
    assert "no results" in reasons and any("TimeoutError" in r for r in reasons)


def test_ddgs_adapter_maps_library_results(monkeypatch):
    fake = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, **kwargs):
            assert kwargs == {"max_results": 3}
            return [{"title": "Notes", "href": "https://notes.example.com", "body": "EE notes $12"}]

    fake.DDGS = DDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    report = mr.search([mr.Query("ee notes price", mr.PURPOSE_PAY)],
                       [mr.Adapter("ddgs", mr.ROLE_GENERAL, mr.ddgs_search)], max_results=3)
    assert report.results[0].url == "https://notes.example.com" and report.results[0].snippet == "EE notes $12"


# ---- prices and tags -----------------------------------------------------

@pytest.mark.parametrize("text", ["Enroll for $49 today", "Only 9.99 USD", "EGP 250 per course", "€ 15", "1,200 dollars"])
def test_prices_are_found(text):
    assert mr.find_price(text)


@pytest.mark.parametrize("text", ["Free for everyone, $0", "Study 5 hours per week", "Founded in 2024",
                                  "Chapter 3 of 12", ""])
def test_non_prices_are_not_found(text):
    assert mr.find_price(text) is None


def test_pay_needs_a_price_on_a_general_page():
    assert mr.confirm_tag("PAY", "Course costs $30", mr.ROLE_GENERAL)[0] == "PAY"
    tag, note = mr.confirm_tag("PAY", "A great course for students", mr.ROLE_GENERAL)
    assert tag == "SAY" and "no price" in note
    assert mr.confirm_tag("PAY", "I paid $30 for it", mr.ROLE_COMMUNITY)[0] == "SAY"


def test_reference_pages_are_never_evidence():
    assert mr.confirm_tag("SAY", "anything", mr.ROLE_REFERENCE)[0] is None
    assert mr.confirm_tag("PAY", "costs $5", mr.ROLE_REFERENCE)[0] is None


# ---- competitors ---------------------------------------------------------

PAGES = [
    mr.Page(1, "https://www.circuitcademy.example.com/courses", role=mr.ROLE_GENERAL, title="CircuitCademy",
            text="CircuitCademy offers a full circuit analysis course for engineering students. Enroll now for $39."),
    mr.Page(2, "https://en.wikipedia.org/wiki/World_Wide_Web", role=mr.ROLE_REFERENCE, title="World Wide Web",
            text="The World Wide Web is an information system that enables content sharing over the Internet."),
    mr.Page(3, "https://openlectures.example.org/ee", role=mr.ROLE_GENERAL, title="Open Lectures",
            text="Open Lectures publishes free electrical engineering lecture notes for anyone to download."),
    mr.Page(4, "https://notesplus.example.com", role=mr.ROLE_GENERAL, title="NotesPlus",
            text="NotesPlus sells handwritten electrical engineering notes. Contact us for a quote."),
]


def entry(name, page, kind, quote, offer="offers something"):
    return mr.CompetitorEntry(name=name, page=page, kind=kind, what_they_offer=offer, quote=quote)


def test_supported_direct_competitor_is_kept_with_its_price():
    report = mr.verify_competitors([entry("CircuitCademy", 1, "direct", "a full circuit analysis course")], PAGES)
    kept = report.kept[0]
    assert (kept.name, kept.kind, kept.price) == ("CircuitCademy", mr.KIND_DIRECT, "$39")


def test_world_wide_web_is_dropped():
    report = mr.verify_competitors(
        [entry("World Wide Web", 2, "direct", "an information system that enables content sharing"),
         entry("The Internet", 1, "free_alternative", "a full circuit analysis course")], PAGES)
    assert not report.kept and len(report.dropped) == 2


def test_reference_page_cannot_supply_a_competitor():
    report = mr.verify_competitors([entry("Tim Berners-Lee Courses", 2, "direct",
                                          "an information system that enables content sharing")], PAGES)
    assert not report.kept and "reference" in report.dropped[0]["reason"]


def test_invented_quote_is_dropped():
    report = mr.verify_competitors([entry("CircuitCademy", 1, "direct", "the market leader with one million users")], PAGES)
    assert not report.kept and "quote not found" in report.dropped[0]["reason"]


def test_quote_matching_ignores_case_spacing_and_curly_quotes():
    report = mr.verify_competitors([entry("CircuitCademy", 1, "direct", "A  Full Circuit\nAnalysis course")], PAGES)
    assert len(report.kept) == 1


def test_name_must_be_on_the_cited_page():
    report = mr.verify_competitors([entry("Coursera", 1, "direct", "a full circuit analysis course")], PAGES)
    assert not report.kept and "name not found" in report.dropped[0]["reason"]


def test_direct_without_a_price_is_listed_as_unpriced():
    report = mr.verify_competitors([entry("NotesPlus", 4, "direct", "sells handwritten electrical engineering notes")], PAGES)
    assert report.kept[0].kind == mr.KIND_UNPRICED and report.kept[0].price is None


def test_free_alternative_is_kept_and_unfetched_page_is_dropped():
    report = mr.verify_competitors(
        [entry("Open Lectures", 3, "free_alternative", "free electrical engineering lecture notes"),
         entry("Ghost Academy", 9, "direct", "some text that is long enough")], PAGES)
    assert [c.name for c in report.kept] == ["Open Lectures"]
    assert "not fetched" in report.dropped[0]["reason"]


def test_not_a_competitor_and_unknown_kinds_are_not_printed():
    report = mr.verify_competitors(
        [entry("CircuitCademy", 1, "not_a_competitor", "a full circuit analysis course"),
         entry("CircuitCademy", 1, "partner", "a full circuit analysis course")], PAGES)
    assert not report.kept and len(report.dropped) == 2


def test_rendering_states_plainly_when_nothing_is_confirmed():
    assert mr.render_competitors(mr.CompetitorReport()) == mr.NO_COMPETITOR_LINE
    text = mr.render_competitors(mr.verify_competitors(
        [entry("CircuitCademy", 1, "direct", "a full circuit analysis course", offer="A paid circuits course.")], PAGES))
    assert "CircuitCademy" in text and "$39" in text and "https://www.circuitcademy.example.com/courses" in text


# ---- excerpt -------------------------------------------------------------

def test_excerpt_prefers_passages_that_match_the_query():
    filler = "Lorem ipsum dolor sit amet. " * 120
    page = filler + "\n\nOur circuit analysis course costs $39 for students.\n\n" + filler
    out = mr.excerpt(page, "circuit analysis course price", limit=300)
    assert "circuit analysis course costs $39" in out and len(out) <= 300


def test_excerpt_falls_back_to_the_start_of_the_page():
    page = "Alpha beta gamma. " * 200
    assert mr.excerpt(page, "circuit analysis", limit=100) == page.strip()[:100]

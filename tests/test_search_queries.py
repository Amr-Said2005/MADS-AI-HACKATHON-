"""Offline tests for how search queries are built from the founder brief. No network and no model are used."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_research as mr
from agents import researcher

ROOT = Path(__file__).resolve().parent.parent
EE_BRIEF = {
    "idea": ("I want to develop an educational website that helps undergraduate engineering students enrolled in "
             "electrical engineering courses ace their courses by providing Digital notes, summaries, and cheat sheets"),
    "target_market": "Electrical Engineering undergraduates",
}
FILLER = ("want", "develop", "build", "create", "make", "help", "helps", "website", "educational")


def _ctx(brief, feasibility=None, repeats=0):
    data = {"customer": brief["target_market"], "problem": "", "search_terms": []}
    data.update(feasibility or {})
    return SimpleNamespace(brief=brief, repeats=repeats, artifact=lambda name: {"data": data} if name == "feasibility" else {})


def _market_queries(brief, **kw):
    return researcher.built_queries(_ctx(brief, **kw), (mr.PURPOSE_PAY, mr.PURPOSE_MARKETPLACE))[:mr.MAX_QUERIES]


# ---- product words ---------------------------------------------------------

def test_product_words_are_what_is_delivered_not_what_the_customer_attends():
    words = mr.derive_products(EE_BRIEF["idea"], EE_BRIEF["target_market"])
    assert [w.search for w in words] == ["notes", "summaries", "cheat sheets"]
    assert words[0].text == "digital notes" and {w.family for w in words} == {"Written content"}


@pytest.mark.parametrize("idea, market, expected", [
    ("A course for students taking statistics courses", "statistics students", ["course"]),
    ("Worksheets for teachers to use in their lessons", "primary school teachers", ["worksheets"]),
    ("A website with printable checklists for new parents", "new parents", ["printable checklists"]),
    ("A newsletter for people who run newsletters", "people who run newsletters", []),  # every match is the customer's
    ("A mobile app that tracks study time", "university students", []),  # a channel word: the assessor's word is used
    ("A website for dog owners", "dog owners", []),
    ("An online platform that sells digital notes and courses", "students", ["notes", "courses"]),
])
def test_customer_context_and_channel_words_are_dropped(idea, market, expected):
    assert [w.search for w in mr.derive_products(idea, market)] == expected


def test_a_word_after_a_delivery_cue_goes_first():
    words = mr.derive_products("A guide for hobby bakers, including a planner", "hobby bakers")
    assert [w.search for w in words] == ["planner", "guide"]


# ---- topic -----------------------------------------------------------------

def test_topic_is_the_phrase_the_idea_and_the_market_share():
    assert mr.derive_topic(EE_BRIEF["idea"], EE_BRIEF["target_market"]) == "electrical engineering"


def test_topic_never_holds_filler_or_product_words():
    topic = mr.derive_topic("I want to build a website that helps people make sourdough bread with a guide", "home bakers")
    assert topic == "sourdough bread"
    assert mr.derive_topic("", "Collectors of vintage typewriters") == "collectors vintage typewriters"
    assert mr.derive_topic("", "") == ""


# ---- the committed brief ---------------------------------------------------

def test_committed_brief_gives_topic_and_deliverable_queries_best_first():
    queries = _market_queries(EE_BRIEF, feasibility={"family": "Courses and learning", "product_type": "courses"})
    assert queries[0] == "electrical engineering notes price"
    assert all("electrical engineering" in q for q in queries)
    assert all(any(w in q for w in ("notes", "summaries", "cheat sheets")) for q in queries)
    assert not any("want develop educational" in q or "course" in q for q in queries)
    assert not any(w in q.split() for q in queries for w in FILLER)
    sites = [q for q in queries if q.startswith("site:")]
    assert sites[0] == "site:stuvia.com electrical engineering notes" and any("studocu.com" in q for q in sites)
    assert "best electrical engineering notes for undergraduates" in queries
    assert queries[-1] == "electrical engineering notes"  # the relaxed fallback: no operator, no intent word
    assert len(queries) == len({q.lower() for q in queries}) <= mr.MAX_QUERIES


def test_rungs_come_in_order_and_a_repeat_leads_with_the_next_product_word():
    terms = researcher.brief_terms(_ctx(EE_BRIEF))
    rungs = [q.rung for q in mr.build_queries(terms)]
    assert rungs == sorted(rungs) and set(rungs) == {1, 2, 3}
    first, again = _market_queries(EE_BRIEF), _market_queries(EE_BRIEF, repeats=1)
    assert again[0] == "electrical engineering summaries price" and first[0] != again[0]
    assert again[4].startswith("site:")  # the order of the rungs is the same on a repeat: nothing is reversed
    one_product = json.loads((ROOT / "briefs" / "example_weak.json").read_text(encoding="utf-8"))
    assert _market_queries(one_product) == _market_queries(one_product, repeats=2)


def test_marketplaces_follow_the_product_family():
    assert mr.marketplace_hosts("Written content", ["notes"])[:2] == ("stuvia.com", "studocu.com")
    assert mr.marketplace_hosts("Written content", ["guide"]) == ("gumroad.com", "etsy.com")
    assert mr.marketplace_hosts("Courses and learning", ["course"]) == ("udemy.com", "coursera.org")
    assert "notion.so templates" in mr.marketplace_hosts("Templates and tools")
    assert mr.marketplace_hosts("Software") == mr.DEFAULT_MARKETPLACE_HOSTS
    course = mr.build_queries(mr.BriefTerms(customer="", problem="", product="course", topic="watercolour", family="Courses and learning"))
    assert [q.text for q in course if q.rung == 2] == ["site:udemy.com watercolour course", "site:coursera.org watercolour course"]


# ---- the example briefs ------------------------------------------------------

@pytest.mark.parametrize("name, topic, product, site", [
    ("example_strong", "freelance translators", "notion template pack", "site:gumroad.com freelance translators notion template pack"),
    ("example_weak", "mechanical typewriters", "guide", "site:gumroad.com mechanical typewriters guide"),
    ("thin_evidence", "seed swap", "checklist", "site:etsy.com seed swap checklist"),
])
def test_example_briefs_give_sensible_queries(name, topic, product, site):
    brief = json.loads((ROOT / "briefs" / f"{name}.json").read_text(encoding="utf-8"))
    queries = _market_queries(brief)
    assert queries[0] == f"{topic} {product} price" and f"buy {topic} {product}" in queries
    assert site in queries and queries[-1] == f"{topic} {product}"
    assert all(topic in q for q in queries) and len(queries) <= mr.MAX_QUERIES
    assert not any(w in q.split() for q in queries for w in FILLER + ("helps", "that"))
    assert all(len(q.split()) == len({w.lower() for w in q.split()}) for q in queries)  # no word twice in a query


def test_without_a_product_word_in_the_idea_the_assessors_word_is_used():
    brief = {"idea": "Something useful for dog owners", "target_market": "dog owners"}
    queries = _market_queries(brief, feasibility={"family": "Written content", "product_type": "guide"})
    assert queries[0] == "dog owners guide price" and any(q.startswith("site:gumroad.com") for q in queries)


def test_problem_queries_still_come_from_the_customer_and_problem():
    ctx = _ctx(EE_BRIEF, feasibility={"customer": "electrical engineering students", "problem": "circuit analysis",
                                      "search_terms": ["circuit analysis exam help", "what is the web"]})
    queries = researcher.built_queries(ctx, (mr.PURPOSE_SAY,))
    assert queries[0] == "electrical engineering students struggling with circuit analysis"
    assert "circuit analysis exam help" in queries and "what is the web" not in queries


def test_the_founders_own_product_is_not_read_as_customer_context():
    words = [w.search for w in mr.derive_products("I sell my online course on Python and our templates to nurses", "nurses")]
    assert "online course" in words and "templates" in words
    # the customer's own things are still dropped
    assert mr.derive_products("An app that organises their notes and your courses", "students") == []


def test_a_repeated_product_word_stays_one_short_word():
    words = mr.derive_products("ebook " * 200 + "for gardeners", "gardeners")
    assert [w.search for w in words] == ["ebook"]
    long_run = mr.derive_products("notion template pack planner tracker spreadsheet worksheet for teams", "teams")
    assert all(len(w.text.split()) <= mr.MAX_JOINED_WORDS for w in long_run)


def test_customer_punctuation_and_markup_stay_out_of_the_query():
    terms = mr.BriefTerms(customer="Designers, <b>freelance</b> & (busy)", problem="", product="templates",
                          topic="invoice", products=("templates",), family="Templates and tools")
    best = next(q.text for q in mr.build_queries(terms) if q.text.startswith("best "))
    assert best.startswith("best invoice templates for designers")
    assert "," not in best and "(" not in best and "&" not in best and not best.endswith(">")


def test_empty_and_odd_briefs_build_no_queries_and_do_not_fail():
    for idea, market in (("", ""), (None, None), ("!!! ??? 123", "456"), ("   ", "x")):
        assert mr.derive_products(idea, market) == []
        assert isinstance(mr.derive_topic(idea, market), str)
    assert mr.build_queries(mr.BriefTerms(customer="", problem="", product="")) == []


"""Relevance scoring before fetching, the fixed relaxation rounds and the per-run search report. Offline."""

from types import SimpleNamespace

import httpx
import pytest

import agents
import search_relevance as rel
import sources
from sources import SearchHit, SourceRegistry

BODY = ("<p>Electrical engineering students say circuit analysis notes are hard to find in one place. "
        "This page collects worked examples for nodal analysis and sells the full set for $12.</p>") * 3


def article(title="Circuit analysis notes"):
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>{BODY}</article></body></html>"


def make_ctx(handler=None, max_sources=10):
    requested = []

    def wrapped(req):
        requested.append(req.headers["host"])
        return handler(req) if handler else httpx.Response(200, html=article())

    reg = SourceRegistry(max_sources=max_sources, transport=httpx.MockTransport(wrapped), resolver=lambda host: ["93.184.216.34"])
    return SimpleNamespace(registry=reg, settings=SimpleNamespace(searxng_url="")), requested


@pytest.fixture(autouse=True)
def clean_report(monkeypatch):
    monkeypatch.setattr(sources, "SEARCH_REPORT", {})
    sources.reset_search_report()
    yield
    sources.reset_search_report()


def hit(host, title, snippet="", adapter="searxng", path="/page"):
    return SearchHit(f"https://{host}{path}", title, snippet, adapter)


# ------------------------------------------------------------------ pure scoring

def test_a_hit_sharing_no_content_word_with_the_query_scores_zero():
    q = "circuit analysis notes for engineering students price"
    assert rel.score_hit("Ten best cat videos", "funny cats and dogs", "https://cats.example/videos", q) == 0
    # "price" and "best" say what kind of page is wanted, not what it is about: they do not make a page on-topic.
    assert rel.score_hit("Best price on garden chairs", "cheap chairs", "https://chairs.example/", q) == 0
    assert rel.score_hit("Circuit analysis notes", "worked examples", "https://ee.example/notes", q) > 0


def test_title_matches_count_more_than_snippet_or_url_matches():
    q = "circuit analysis notes"
    in_title = rel.score_hit("Circuit analysis notes", "", "https://a.example/", q)
    in_snippet = rel.score_hit("Welcome", "circuit analysis notes", "https://a.example/", q)
    in_url = rel.score_hit("Welcome", "", "https://a.example/circuit-analysis-notes", q)
    assert in_title == 3 * rel.TITLE_POINTS and in_snippet == in_url == 3 * rel.TEXT_POINTS
    assert rel.score_hit("Circuits and their analysis", "", "https://a.example/", q) == 2 * rel.TITLE_POINTS  # plural forms match


def test_terms_from_the_brief_are_used_when_passed_and_the_query_otherwise():
    assert rel.query_terms('"invoice template" site:etsy.com price') == ["invoice", "template"]
    assert rel.query_terms("price forum") == ["price", "forum"]  # nothing topical left: the content words are used
    assert rel.score_hit("Invoice template", "", "https://a.example/", "something else", terms=["invoice", "translators"]) == 2
    assert rel.score_hit("Invoice template", "", "https://a.example/", "something else") == 0
    assert rel.score_hit("Invoice template", "", "https://a.example/", "", terms=[]) == 0


def test_a_price_and_a_marketplace_host_add_points_only_for_pay_searches():
    say, pay = "circuit analysis notes forum", "buy circuit analysis notes"
    assert not rel.is_pay_query(say) and rel.is_pay_query(pay) and rel.is_pay_query("circuit analysis notes Udemy")
    base = rel.score_hit("Circuit analysis notes", "full set", "https://ee.example/", pay)
    priced = rel.score_hit("Circuit analysis notes", "full set for $12", "https://ee.example/", pay)
    market = rel.score_hit("Circuit analysis notes", "full set", "https://www.udemy.com/course/circuits/", pay)
    assert priced == base + rel.PRICE_POINTS and market == base + rel.MARKETPLACE_POINTS
    assert rel.score_hit("Circuit analysis notes", "full set for $12", "https://www.udemy.com/x", say) == base
    assert rel.score_hit("Circuit analysis notes", "free, $0", "https://ee.example/", pay) == base  # zero is not a price
    # The bonus never rescues an off-topic page.
    assert rel.score_hit("Knitting course", "only $12", "https://www.udemy.com/course/knitting/", pay) == 0
    assert rel.score_hit("Circuit analysis notes", "", "https://ee.example/", say, pay=True) == base


def test_rank_keeps_hits_at_or_above_the_threshold_best_first_and_stable():
    assert rel.rank([(1, "a"), (0, "b"), (5, "c"), (1, "d"), (3, "e")]) == [(5, "c"), (3, "e"), (1, "a"), (1, "d")]


def test_relaxed_queries_strip_quotes_and_operators_and_stop_after_the_fixed_rounds():
    assert rel.RELAX_ROUNDS == 2
    q = ['"circuit analysis" notes for engineering students site:etsy.com price', "circuit analysis notes"]
    assert rel.strip_operators(q[0]) == "circuit analysis notes for engineering students price"
    assert rel.relaxed_queries(q, 1) == ["circuit analysis notes engineering students"]
    assert rel.relaxed_queries(q, 2) == []  # "circuit analysis notes" is one of the original queries
    assert rel.relaxed_queries(q[:1], 2) == ["circuit analysis notes"]
    assert rel.relaxed_queries(q[:1], 2, tried=["Circuit Analysis Notes"]) == []
    assert rel.relaxed_queries(q, 3) == [] and rel.relaxed_queries(q, 0) == []


# ------------------------------------------------------------------ gather

def test_gather_fetches_the_best_hit_first_and_never_requests_an_off_topic_page(monkeypatch):
    def fake(query, **kw):
        return [hit("cats.example", "Ten best cat videos", "funny cats"),
                hit("weak.example", "Study tips", "some notes for students"),
                hit("strong.example", "Circuit analysis notes", "worked examples for $12")]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, requested = make_ctx()
    pages = agents.gather(ctx, ["buy circuit analysis notes"], want=2)
    assert [p.url for p in pages] == ["https://strong.example/page", "https://weak.example/page"]
    assert requested == ["strong.example", "weak.example"]  # ranked order; the off-topic page was never requested
    assert not ctx.registry.is_registered("https://cats.example/page")
    statuses = {h["url"]: h["status"] for h in sources.search_report()["hits"]}
    assert statuses == {"https://cats.example/page": sources.HIT_OFF_TOPIC,
                        "https://strong.example/page": sources.HIT_FETCHED, "https://weak.example/page": sources.HIT_FETCHED}


def test_gather_searches_every_query_before_fetching_and_keeps_one_page_per_domain(monkeypatch):
    asked = []

    def fake(query, **kw):
        asked.append(query)
        if "forum" in query:
            return [hit("a.example", "Notes", "circuit notes", path="/1"), hit("skip.youtube.com", "Circuit analysis notes")]
        return [hit("a.example", "Circuit analysis notes", "the full set", path="/2"),
                hit("b.example", "Circuit analysis notes for students")]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, requested = make_ctx()
    pages = agents.gather(ctx, ["circuit analysis forum", "circuit analysis notes"], want=3)
    assert asked[:2] == ["circuit analysis forum", "circuit analysis notes"]
    assert [p.url for p in pages] == ["https://a.example/2", "https://b.example/page"]  # the better page of a.example
    assert "skip.youtube.com" not in requested and requested.count("a.example") == 1
    by_url = {h["url"]: h for h in sources.search_report()["hits"]}
    assert by_url["https://a.example/1"]["status"] == sources.HIT_DUPLICATE
    assert by_url["https://skip.youtube.com/page"]["status"] == sources.HIT_SKIPPED


def test_gather_relaxes_the_queries_for_exactly_two_rounds_then_stops(monkeypatch):
    asked = []
    monkeypatch.setattr(agents, "web_search", lambda query, **kw: asked.append(query) or [])
    ctx, requested = make_ctx()
    original = '"circuit analysis" notes for engineering students site:etsy.com price'
    assert agents.gather(ctx, [original], want=3) == []
    assert asked == [original, "circuit analysis notes engineering students", "circuit analysis notes"]
    assert requested == []


def test_gather_does_not_relax_when_enough_pages_were_fetched(monkeypatch):
    asked = []

    def fake(query, **kw):
        asked.append(query)
        return [hit("a.example", "Circuit analysis notes"), hit("b.example", "Circuit analysis notes"),
                hit("c.example", "Circuit analysis notes")]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, requested = make_ctx()
    pages = agents.gather(ctx, ['"circuit analysis" notes for engineering students'], want=2)
    assert len(pages) == 2 and len(asked) == 1 and requested == ["a.example", "b.example"]
    kept = [h for h in sources.search_report()["hits"] if h["status"] == sources.HIT_KEPT]
    assert [h["url"] for h in kept] == ["https://c.example/page"] and kept[0]["reason"] == "enough pages in hand"


def test_a_relaxed_round_fills_the_gap_left_by_the_first_round(monkeypatch):
    def fake(query, **kw):
        if query.startswith('"'):
            return [hit("a.example", "Circuit analysis notes")]
        return [hit("a.example", "Circuit analysis notes", path="/other"), hit("b.example", "Circuit analysis for students")]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, _ = make_ctx()
    pages = agents.gather(ctx, ['"circuit analysis" notes for engineering students'], want=2)
    assert [p.url for p in pages] == ["https://a.example/page", "https://b.example/page"]


def test_fetch_failures_are_reported_with_their_reason_and_nothing_is_registered(monkeypatch):
    def handler(req):
        host = req.headers["host"]
        if host == "forbidden.example":
            return httpx.Response(403, html=article())
        if host == "empty.example":
            return httpx.Response(200, html="<html><body><script>var x = 1;</script></body></html>")
        if host == "pdf.example":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")
        if host == "blocked.example":
            return httpx.Response(200, html=article("Just a moment..."))
        return httpx.Response(200, html=article())

    monkeypatch.setattr(agents, "web_search", lambda query, **kw: [
        hit(h, "Circuit analysis notes") for h in ("forbidden.example", "empty.example", "pdf.example", "blocked.example",
                                                   "good.example")] if query == "circuit analysis notes" else [])
    ctx, _ = make_ctx(handler)
    pages = agents.gather(ctx, ["circuit analysis notes"], want=3)
    assert [p.url for p in pages] == ["https://good.example/page"] and ctx.registry.urls() == ["https://good.example/page"]
    reasons = {h["url"].split("/")[2]: (h["status"], h["reason"]) for h in sources.search_report()["hits"]}
    assert reasons == {"forbidden.example": (sources.HIT_FAILED, "HTTP 403"),
                       "empty.example": (sources.HIT_FAILED, "no main text"),
                       "pdf.example": (sources.HIT_FAILED, "unsupported type"),
                       "blocked.example": (sources.HIT_FAILED, "bot-block"),
                       "good.example": (sources.HIT_FETCHED, "")}
    assert sources.SEARCH_REPORT["pages"] == ("5 found, 1 fetched, 4 failed to fetch "
                                              "(HTTP 403 x1, bot-block x1, no main text x1, unsupported type x1)")
    md = sources.search_report_markdown()
    assert "- failed to fetch (HTTP 403): https://forbidden.example/page via searxng, score 6" in md
    assert "- fetched: https://good.example/page via searxng, score 6" in md


def test_gather_stops_at_the_source_budget_and_survives_a_failing_search(monkeypatch):
    def fake(query, **kw):
        if query == "broken":
            raise RuntimeError("adapter exploded")
        return [hit(f"s{i}.example", "Circuit analysis notes") for i in range(4)]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, requested = make_ctx(max_sources=2)
    pages = agents.gather(ctx, ["broken", "circuit analysis notes"], want=4)
    assert len(pages) == 2 and len(requested) == 2  # the budget is the registry's, and gather stops there
    assert {"query": "broken", "adapter": "search", "outcome": "RuntimeError: adapter exploded"} in sources.search_report()["queries"]


def test_pages_already_in_hand_count_and_their_domains_are_not_fetched_again(monkeypatch):
    monkeypatch.setattr(agents, "web_search", lambda query, **kw: [hit("a.example", "Circuit analysis notes", path="/new"),
                                                                    hit("b.example", "Circuit analysis notes")])
    ctx, requested = make_ctx()
    first = ctx.registry.fetch("https://a.example/page")
    requested.clear()
    pages = agents.gather(ctx, ["circuit analysis notes"], want=2, have=[first])
    assert [p.url for p in pages] == ["https://a.example/page", "https://b.example/page"] and requested == ["b.example"]


def test_a_lead_from_no_known_adapter_is_kept_at_the_lowest_passing_score(monkeypatch):
    monkeypatch.setattr(agents, "web_search", lambda query, **kw: [hit("lead.example", "t", "s", adapter="handed-in"),
                                                                    hit("cats.example", "Cat videos", adapter="ddgs")])
    ctx, requested = make_ctx()
    assert [p.url for p in agents.gather(ctx, ["circuit analysis notes"], want=2)] == ["https://lead.example/page"]
    assert requested == ["lead.example"]

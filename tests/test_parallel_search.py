"""Searches and page downloads that run at the same time, the early market searches and the
founder's pages. Offline: search is faked with an artificial delay and pages come from an
httpx.MockTransport. The time bounds are generous: under half of what one-after-another would take."""

import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import agents
import orchestrator as orch
import settings
import sources
from agents import researcher
from llm import LLMClient
from sources import SearchHit, SourceRegistry

BODY = "<p>" + "Worked examples of circuit analysis for first-year engineering students, with step-by-step solutions. " * 8 + "</p>"
PRICED = "<p>" + "The circuit analysis tracker template costs $19 as a one-time purchase for students. " * 8 + "</p>"
STATEMENT = "Twelve translators told me in person that they lose track of deadlines every month"


def article(title="Circuit analysis notes", body=BODY):
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>{body}</article></body></html>"


def hit(host, title="Circuit analysis notes", path="/page", adapter="searxng"):
    return SearchHit(f"https://{host}{path}", title, "", adapter)


def make_registry(handler=None, max_sources=10, delay=0.0, delays=None):
    """A registry whose pages come from a MockTransport. Returns it with the list of requested hosts."""
    requested, lock = [], threading.Lock()

    def respond(req):
        host = req.headers["host"]
        with lock:
            requested.append(host)
        time.sleep((delays or {}).get(host, delay))
        return handler(req) if handler else httpx.Response(200, html=article(body=PRICED if host.startswith("paid") else BODY))

    reg = SourceRegistry(max_sources=max_sources, transport=httpx.MockTransport(respond), resolver=lambda host: ["93.184.216.34"])
    return reg, requested


def make_ctx(**kw):
    reg, requested = make_registry(**kw)
    return SimpleNamespace(registry=reg, settings=SimpleNamespace(searxng_url="")), requested


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(sources, "SEARCH_REPORT", {})
    monkeypatch.delenv("SEARCH_WORKERS", raising=False)
    monkeypatch.delenv("SEARCH_PREFETCH", raising=False)
    sources.reset_search_report()
    sources.main_text(article())  # the slow first import of the text extractor is not what these tests time
    yield
    sources.reset_search_report()
    with researcher._prefetch_lock:
        for started in researcher._prefetches.values():
            started.cancel()
        researcher._prefetches.clear()


# ------------------------------------------------------------------ the knob

@pytest.mark.parametrize("value, workers", [(None, 4), ("1", 1), ("0", 1), ("-3", 1), ("6", 6), ("99", 8), ("many", 4), ("", 4)])
def test_search_workers_defaults_to_four_and_stays_between_one_and_eight(monkeypatch, value, workers):
    if value is not None:
        monkeypatch.setenv("SEARCH_WORKERS", value)
    assert sources.search_workers() == workers


# ------------------------------------------------------------------ searches at the same time

QUERIES = [f"circuit analysis notes part{i}" for i in range(4)]
SEARCH_DELAYS = dict(zip(QUERIES, (0.5, 0.4, 0.3, 0.2)))  # the first query answers last


def slow_search(finished, threads=None):
    def fake(query, **kw):
        time.sleep(SEARCH_DELAYS[query])
        sources.report_query(query, "fake", "1 hits")  # what a real adapter records while it searches
        finished.append(query)
        if threads is not None:
            threads.append(threading.current_thread().name)
        return [hit(f"site{QUERIES.index(query)}.example")]
    return fake


def test_slow_queries_run_together_and_the_hits_and_the_report_stay_in_query_order(monkeypatch):
    finished, threads = [], []
    monkeypatch.setattr(agents, "web_search", slow_search(finished, threads))
    ctx, _ = make_ctx()
    started = time.monotonic()
    ranked = agents.collect_hits(ctx, QUERIES, 5)
    elapsed = time.monotonic() - started
    assert elapsed < sum(SEARCH_DELAYS.values()) / 2, elapsed  # one after another takes 1.4 s
    assert finished[0] == QUERIES[-1] and finished[-1] == QUERIES[0]  # they finished in another order
    assert all(name.startswith("search") for name in threads)
    assert [q for _, q, _ in ranked] == QUERIES  # equal scores keep the query order
    assert [h.url for _, _, h in ranked] == [f"https://site{i}.example/page" for i in range(4)]
    assert sources.search_report()["queries"] == [{"query": q, "adapter": "fake", "outcome": "1 hits"} for q in QUERIES]


def test_one_worker_searches_one_after_another_and_finds_the_same(monkeypatch):
    finished = []
    monkeypatch.setattr(agents, "web_search", slow_search(finished))
    ctx, _ = make_ctx()
    together = agents.collect_hits(ctx, QUERIES, 5)
    rows = sources.search_report()["queries"]
    sources.reset_search_report()
    finished.clear()
    monkeypatch.setenv("SEARCH_WORKERS", "1")
    main = threading.current_thread().name
    monkeypatch.setattr(agents, "web_search", lambda q, **kw: (finished.append(threading.current_thread().name),
                                                                slow_search([])(q, **kw))[1])
    in_turn = agents.collect_hits(ctx, QUERIES, 5)
    assert finished == [main] * 4  # no worker thread at all
    assert in_turn == together and sources.search_report()["queries"] == rows


def test_a_failing_query_is_reported_in_its_place_and_the_others_still_count(monkeypatch):
    def fake(query, **kw):
        time.sleep(0.05 if query == "broken" else 0.15)
        if query == "broken":
            raise RuntimeError("adapter exploded")
        sources.report_query(query, "fake", "1 hits")
        return [hit("a.example" if "first" in query else "b.example")]

    monkeypatch.setattr(agents, "web_search", fake)
    ctx, _ = make_ctx()
    ranked = agents.collect_hits(ctx, ["circuit analysis first", "broken", "circuit analysis last"], 5)
    assert [h.url for _, _, h in ranked] == ["https://a.example/page", "https://b.example/page"]
    assert [(r["query"], r["adapter"]) for r in sources.search_report()["queries"]] == [
        ("circuit analysis first", "fake"), ("broken", "search"), ("circuit analysis last", "fake")]


def test_at_most_two_searches_are_in_flight_against_one_adapter(monkeypatch):
    lock, in_flight, peak = threading.Lock(), {}, {}

    def handler(req):
        host = req.url.host
        with lock:
            in_flight[host] = in_flight.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), in_flight[host])
        time.sleep(0.1)
        with lock:
            in_flight[host] -= 1
        if host == "hn.algolia.com":  # the first adapter finds nothing, so every query goes on to the second
            return httpx.Response(200, json={"hits": []})
        return httpx.Response(200, json={"items": [{"link": "https://a.example/q", "title": "Circuit analysis", "body": "x"}]})

    monkeypatch.setenv("SEARXNG_URL", "")
    transport = httpx.MockTransport(handler)
    results, rows = [], []

    def one(i):
        with sources.capture_search_report() as events:
            results.append(sources.search(f"circuit analysis {i}", transport=transport))
        rows.append(events)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 6 and all(len(r) == 1 for r in results)
    assert peak == {"hn.algolia.com": 2, "api.stackexchange.com": 2}  # six callers, never more than two per adapter
    assert sources.search_report()["queries"] == [] and sources.SEARCH_REPORT == {}  # captured, not written by the workers
    assert all([e[0] for e in events] == ["adapter", "query", "adapter", "query"] for events in rows)
    sources.replay_search_report(rows[0])
    assert [r["adapter"] for r in sources.search_report()["queries"]] == ["hackernews", "stackexchange"]
    assert sources.SEARCH_REPORT == {"hackernews": "no results"}


def test_a_timed_out_adapter_is_asked_once_per_query_and_the_waits_are_short(monkeypatch):
    asked = []

    def handler(req):
        asked.append(req.url.host)
        if req.url.host == "hn.algolia.com":
            raise httpx.ReadTimeout("timed out", request=req)
        return httpx.Response(200, json={"items": [{"link": "https://a.example/q", "title": "Circuit analysis", "body": "x"}]})

    monkeypatch.setenv("SEARXNG_URL", "")
    assert len(sources.search("circuit analysis", transport=httpx.MockTransport(handler))) == 1
    assert asked == ["hn.algolia.com", "api.stackexchange.com"]  # no second try of the adapter that timed out
    assert sources.SEARCH_TIMEOUT <= 6 and sources.FETCH_TIMEOUT <= 8


# ------------------------------------------------------------------ downloads at the same time

def hosts_search(*hosts):
    return lambda query, **kw: [hit(h) if isinstance(h, str) else h for h in hosts]


def test_pages_download_together_and_are_registered_in_ranked_order(monkeypatch):
    hosts = [f"s{i}.example" for i in range(6)]
    monkeypatch.setattr(agents, "web_search", hosts_search(*hosts))
    # the best hit is the slowest to download, so the downloads finish in another order than the ranking
    ctx, requested = make_ctx(delays={"s0.example": 0.4, "s1.example": 0.3, "s2.example": 0.3, "s3.example": 0.2})
    started = time.monotonic()
    pages = agents.gather(ctx, ["circuit analysis notes"], want=4)
    elapsed = time.monotonic() - started
    assert elapsed < (0.4 + 0.3 + 0.3 + 0.2) / 2, elapsed  # one after another takes 1.2 s
    assert [p.url for p in pages] == [f"https://s{i}.example/page" for i in range(4)]
    assert ctx.registry.urls() == [p.url for p in pages] and ctx.registry.fetched_in_step == 4
    assert len(requested) <= 4 + agents.SPARE_DOWNLOADS
    # the two spare downloads were not needed: they are reported so, and never registered or counted
    spare = {h["url"]: (h["status"], h["reason"]) for h in sources.search_report()["hits"]
             if h["url"] not in ctx.registry.urls()}
    assert spare == {f"https://s{i}.example/page": (sources.HIT_KEPT, agents.NOT_NEEDED) for i in (4, 5)}
    assert not ctx.registry.is_registered("https://s4.example/page")


def test_parallel_fetch_keeps_every_registry_rule(monkeypatch):
    def handler(req):
        host = req.headers["host"]
        if host == "junk.example":
            return httpx.Response(200, html=article("Just a moment..."))
        if host == "gone.example":
            return httpx.Response(404, html=article())
        return httpx.Response(200, html=article())

    found = [hit("a.example", path="/1"), hit("a.example", path="/2"), hit("junk.example"), hit("gone.example"),
             hit("cats.example", "Ten best cat videos"), hit("b.example"), hit("c.example"), hit("d.example"),
             hit("e.example"), hit("f.example")]

    def run(workers):
        sources.reset_search_report()
        monkeypatch.setenv("SEARCH_WORKERS", workers)
        monkeypatch.setattr(agents, "web_search", hosts_search(*found))
        ctx, requested = make_ctx(handler=handler, delays={"a.example": 0.15})
        pages = agents.gather(ctx, ["circuit analysis notes"], want=3)
        return ctx, requested, pages, {h["url"]: (h["status"], h["reason"]) for h in sources.search_report()["hits"]}

    ctx, requested, pages, report = run("4")
    urls = [p.url for p in pages]
    assert urls == ["https://a.example/1", "https://b.example/page", "https://c.example/page"]  # ranked order
    assert ctx.registry.urls() == urls and ctx.registry.fetched_in_step == 3  # never more than `want`
    assert len({agents.domain(u) for u in urls}) == 3 and requested.count("a.example") == 1  # one page per domain
    assert not ctx.registry.is_registered("https://junk.example/page") and "cats.example" not in requested
    assert report["https://a.example/2"][0] == sources.HIT_DUPLICATE
    assert report["https://junk.example/page"] == (sources.HIT_FAILED, "bot-block")
    assert report["https://gone.example/page"] == (sources.HIT_FAILED, "HTTP 404")
    assert report["https://cats.example/page"][0] == sources.HIT_OFF_TOPIC
    # the first five candidates (three wanted, two spare) held three good pages, so the rest were never asked for
    assert report["https://d.example/page"] == (sources.HIT_KEPT, "enough pages in hand")
    assert not {"d.example", "e.example", "f.example"} & set(requested)

    one_ctx, one_requested, one_pages, one_report = run("1")
    assert [p.url for p in one_pages] == urls and one_ctx.registry.urls() == urls  # the same pages as one after another
    assert one_requested == ["a.example", "junk.example", "gone.example", "b.example", "c.example"]  # nothing spare
    assert one_report["https://d.example/page"] == (sources.HIT_KEPT, "enough pages in hand")
    assert {u: s for u, (s, _) in one_report.items()} == {u: s for u, (s, _) in report.items()}


def test_parallel_fetch_never_goes_past_the_source_budget(monkeypatch):
    monkeypatch.setattr(agents, "web_search", hosts_search(*[f"s{i}.example" for i in range(8)]))
    ctx, requested = make_ctx(max_sources=2)
    pages = agents.gather(ctx, ["circuit analysis notes"], want=5)
    assert [p.url for p in pages] == ["https://s0.example/page", "https://s1.example/page"]
    assert ctx.registry.fetched_in_step == 2 and len(ctx.registry.urls()) == 2 and sorted(requested) == ["s0.example", "s1.example"]
    failed = [h for h in sources.search_report()["hits"] if h["status"] == sources.HIT_FAILED]
    assert [(h["url"], h["reason"]) for h in failed] == [("https://s2.example/page", "source budget")]


def test_fetch_from_many_threads_registers_no_more_than_the_budget():
    reg, _ = make_registry(max_sources=3, delay=0.05)
    errors = []

    def one(i):
        try:
            reg.fetch(f"https://t{i}.example/page")
        except sources.FetchError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(reg.urls()) == 3 and reg.fetched_in_step == 3
    assert len(errors) == 7 and all("source budget" in e for e in errors)


def test_download_registers_nothing_and_register_counts_once():
    reg, requested = make_registry()
    page = reg.download("https://a.example/page")
    assert page.text and reg.urls() == [] and reg.fetched_in_step == 0 and not reg.is_registered(page.url)
    assert reg.register(page) is page and reg.register(reg.download(page.url)) is page  # the first page is kept
    assert reg.urls() == ["https://a.example/page"] and reg.fetched_in_step == 1
    assert reg.fetch(page.url) is page and len(requested) == 2  # fetch still answers from the registry
    with pytest.raises(sources.FetchError, match="non-public"):
        SourceRegistry(resolver=lambda host: ["10.0.0.5"]).download("https://inside.example/")


# ------------------------------------------------------------------ the researcher: early searches, founder pages

OPPORTUNITIES = {"evidence": [], "opportunities": [
    {"title": "Deadline tracker", "customer": "freelance translators", "problem": "missed deadlines", "pages": [], "assumptions": []},
    {"title": "Word count log", "customer": "translators with direct clients", "problem": "word counts", "pages": [],
     "assumptions": []}]}
MARKET = {"claims": [{"page": 1, "kind": "PAY", "text": "a paid tracker template is sold"}], "competitors": [], "gaps": []}


def shop_search(calls, block=None):
    """Every query finds four shops. With `block` set, a call from an early-search thread waits on it."""
    def fake(query, **kw):
        early = threading.current_thread().name == "search-prefetch"
        calls.append((query, early))
        if early and block is not None:
            block()
        words = " ".join(query.split()[:3])
        return [SearchHit(f"https://paid{i}.example/page", f"{words} template", query, "searxng") for i in range(4)]
    return fake


def research_ctx(brief, messages):
    def respond(msgs, agent, schema):
        messages.append(msgs)
        return json.dumps({"Opportunities": OPPORTUNITIES, "Market": MARKET}[schema.__name__])

    reg, requested = make_registry()
    rec = orch.new_run(brief)
    return orch.RunContext(rec, settings.load(), LLMClient(settings.load(), mock_responder=respond), reg), requested


def founder_item(ctx, kind, host=None, statement="", status=None):
    url = f"https://{host}/mine" if host else ""
    if host and status is None:
        ctx.registry.fetch(url)
    item = {"kind": kind, "url": url, "title": "", "statement": statement, "price": None,
            "status": status or ("fetched" if host else "stated")}
    ctx.rec.artifacts.setdefault("founder_evidence", {"items": []})["items"].append(item)
    return url


def test_the_market_searches_start_during_step_one_and_are_not_run_again(monkeypatch, strong_brief):
    calls, messages = [], []
    monkeypatch.setattr(agents, "web_search", shop_search(calls))
    ctx, _ = research_ctx(strong_brief, messages)
    wanted = researcher._market_queries(ctx)
    researcher.find_opportunities(ctx)
    early = researcher._prefetches[ctx.run_id]
    for t in early._threads:
        t.join(5)
    assert sorted(q for q, was_early in calls if was_early) == sorted(wanted)
    assert ctx.registry.fetched_in_step <= researcher.SAY_PAGES  # the early searches fetched and registered nothing
    registered = ctx.registry.urls()
    sources.reset_search_report()
    del calls[:]
    ctx.registry.begin_step()
    result = researcher.market_research(ctx)
    assert calls == []  # every market query was answered by the early searches
    assert result.data["queries"] == wanted and len(result.data["pages"]) == min(researcher.PAY_PAGES, len(result.data["pages"]))
    assert result.data["pages"] and set(result.data["pages"]) <= set(ctx.registry.urls())
    assert set(ctx.registry.urls()) - set(registered) <= set(result.data["pages"])
    assert ctx.run_id not in researcher._prefetches


def test_an_early_search_that_raises_does_not_break_the_market_step(monkeypatch, strong_brief):
    calls, messages = [], []

    def boom():
        raise RuntimeError("the early search exploded")

    monkeypatch.setattr(agents, "web_search", shop_search(calls, block=boom))
    ctx, _ = research_ctx(strong_brief, messages)
    researcher._start_market_prefetch(ctx)
    result = researcher.market_research(ctx)
    wanted = researcher._market_queries(ctx)
    assert sorted(q for q, was_early in calls if not was_early) == sorted(wanted)  # the step searched for itself
    assert len(result.data["pages"]) == researcher.PAY_PAGES and "paid0.example" in result.content
    assert "the early search exploded" not in sources.search_report_markdown()  # a failed early search leaves no row


def test_an_early_search_that_hangs_does_not_block_the_market_step(monkeypatch, strong_brief):
    calls, messages, release = [], [], threading.Event()
    monkeypatch.setattr(agents, "web_search", shop_search(calls, block=lambda: release.wait(20)))
    monkeypatch.setattr(researcher, "PREFETCH_JOIN_SECONDS", 0.2)
    ctx, _ = research_ctx(strong_brief, messages)
    try:
        researcher._start_market_prefetch(ctx)
        early = researcher._prefetches[ctx.run_id]
        started = time.monotonic()
        result = researcher.market_research(ctx)
        assert time.monotonic() - started < 5
        assert len(result.data["pages"]) == researcher.PAY_PAGES
        assert early._stopped() and early._results == {}
    finally:
        release.set()
    for t in early._threads:
        t.join(5)
    assert not any(t.is_alive() for t in early._threads) and early._results == {}  # late answers are dropped
    assert all(t.daemon for t in early._threads)


def test_starting_the_early_searches_never_raises(monkeypatch, strong_brief):
    ctx, _ = research_ctx(strong_brief, [])
    monkeypatch.setattr(researcher, "_market_queries", lambda ctx: 1 / 0)
    researcher._start_market_prefetch(ctx)
    assert researcher._take_market_prefetch(ctx) == {}
    monkeypatch.setattr(researcher, "SearchPrefetch", lambda *a, **k: 1 / 0)
    researcher._start_market_prefetch(ctx)
    assert researcher._prefetches == {}


def test_no_early_search_with_one_worker_or_when_switched_off(monkeypatch, strong_brief):
    ctx, _ = research_ctx(strong_brief, [])
    monkeypatch.setenv("SEARCH_WORKERS", "1")
    researcher._start_market_prefetch(ctx)
    monkeypatch.setenv("SEARCH_WORKERS", "4")
    monkeypatch.setenv("SEARCH_PREFETCH", "false")
    researcher._start_market_prefetch(ctx)
    assert researcher._prefetches == {}


def test_founder_pages_come_first_and_count_toward_the_pages_wanted(monkeypatch, strong_brief):
    calls, messages = [], []
    monkeypatch.setattr(agents, "web_search", shop_search(calls))
    ctx, requested = research_ctx(strong_brief, messages)
    mine = [founder_item(ctx, "pay", "paidmine1.example"), founder_item(ctx, "pay", "paidmine2.example")]
    founder_item(ctx, "problem", "forum.example")              # another kind: not a market page
    founder_item(ctx, "pay", "paidlost.example", status="not fetched")  # never fetched: not in the registry
    ctx.registry.begin_step()
    del requested[:]
    result = researcher.market_research(ctx)
    pages = result.data["pages"]
    assert pages[:2] == mine and len(pages) == researcher.PAY_PAGES  # two of the four wanted were the founder's
    assert len(requested) <= 2 + agents.SPARE_DOWNLOADS and ctx.registry.fetched_in_step == 2
    assert "https://paidlost.example/mine" not in pages and not ctx.registry.is_registered("https://paidlost.example/mine")
    assert "Source URL: " + mine[0] in messages[-1][-1]["content"]
    assert calls and sources.search_report()["notes"] == []


def test_enough_founder_pages_skip_the_search_and_the_report_says_so(monkeypatch, strong_brief):
    calls, messages = [], []
    monkeypatch.setattr(agents, "web_search", shop_search(calls))
    monkeypatch.setenv("SEARCH_PREFETCH", "false")
    ctx, requested = research_ctx(strong_brief, messages)
    pay = [founder_item(ctx, "pay", f"paidmine{i}.example") for i in range(researcher.PAY_PAGES)]
    say = [founder_item(ctx, "problem", f"forum{i}.example") for i in range(researcher.SAY_PAGES)]
    del requested[:]

    market = researcher.market_research(ctx)
    assert market.data["pages"] == pay and market.data["queries"] == []
    assert "Search queries used: none (search skipped: the founder's evidence covers this step)" in market.content
    found = researcher.find_opportunities(ctx)
    assert found.data["pages"] == say and found.data["queries"] == []
    assert calls == [] and requested == []  # nothing was searched and nothing was fetched
    line = "search skipped: the founder's evidence covers this step"
    assert sources.search_report()["notes"] == [f"market_research: {line}", f"find_opportunities: {line}"]
    assert f"- market_research: {line}" in sources.search_report_markdown().splitlines()
    sources.reset_search_report()
    assert sources.search_report()["notes"] == [] and sources.search_report_markdown() == ""


def test_no_early_market_search_when_the_founder_pages_cover_the_market_step(monkeypatch, strong_brief):
    calls = []
    monkeypatch.setattr(agents, "web_search", shop_search(calls))
    ctx, _ = research_ctx(strong_brief, [])
    for i in range(researcher.PAY_PAGES):
        founder_item(ctx, "pay", f"paidmine{i}.example")
    researcher._start_market_prefetch(ctx)
    assert researcher._prefetches == {} and calls == []


def test_a_founder_statement_is_in_the_prompt_and_is_never_a_source(monkeypatch, strong_brief):
    calls, messages = [], []
    monkeypatch.setattr(agents, "web_search", shop_search(calls))
    monkeypatch.setenv("SEARCH_PREFETCH", "false")
    ctx, _ = research_ctx(strong_brief, messages)
    founder_item(ctx, "problem", statement=STATEMENT)
    founder_item(ctx, "pay", "paidlost.example", statement="Agencies pay 30 dollars for this", status="not fetched")
    linked = founder_item(ctx, "problem", "forum.example", statement="This thread shows the problem")
    for step in (researcher.find_opportunities, researcher.market_research):
        result = step(ctx)
        system, user = messages[-1][-2]["content"], messages[-1][-1]["content"]
        assert f"Founder-stated, not a source: {STATEMENT}" in user.splitlines()
        assert "Founder-stated, not a source: Agencies pay 30 dollars for this" in user.splitlines()
        assert "paidlost.example" not in user  # a link that could not be fetched is not shown as a page
        assert "Founder-stated, not a source: This thread shows the problem" not in user  # that one has a fetched page
        assert STATEMENT not in system
        assert all(c.url is None or ctx.registry.is_registered(c.url) for c in result.claims)
        assert not any(STATEMENT in str(c.url) or STATEMENT in c.text for c in result.claims)
        assert all(STATEMENT not in p for p in result.data["pages"]) and "## Sources fetched in this run" in result.content
        assert STATEMENT not in result.content.split("## Sources fetched in this run")[1]
    assert linked in ctx.registry.urls() and not any(STATEMENT in u or "paidlost" in u for u in ctx.registry.urls())


def test_founder_evidence_of_an_unexpected_shape_is_ignored():
    for artifacts in ({}, {"founder_evidence": None}, {"founder_evidence": {"items": "x"}},
                      {"founder_evidence": {"items": [None, "x", {"kind": "pay"}, {"status": "fetched", "kind": "pay", "url": 3}]}}):
        ctx = SimpleNamespace(rec=SimpleNamespace(artifacts=artifacts), registry=SourceRegistry())
        assert researcher.founder_pages(ctx, researcher.PAY_FOUNDER_KINDS, 4) == []
        assert researcher.founder_statements(ctx) == ""
    assert researcher.founder_pages(SimpleNamespace(registry=SourceRegistry()), ("pay",), 4) == []

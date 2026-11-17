"""The fixed-step review loop of the Build phase, offline: code checks, a scripted model, the decision log."""

import json
import re

import pytest

import orchestrator as orch
import review_loop
import settings
from agents import builder
from llm import LLMClient
from test_pipeline import registry

TITLES = ["Client table", "Deadline calendar", "Word count log"]
SPEC = {"product_name": "Translator Deadline Board", "product_type": "template pack", "format": "a Markdown pack",
        "sections": [{"title": t, "purpose": f"gives the translator a {t.lower()}"} for t in TITLES]}
GOOD = ("Copy this table into a new page. Each row is one client. Fill in the name, the agency, the language "
        "pair and the usual turnaround. Check it every Monday and archive clients you no longer work with.\n\n"
        "| Client | Agency | Language pair | Turnaround |\n|---|---|---|---|\n| Example GmbH | Direct | EN-DE | 3 days |")
VAGUE = ("This section offers a clear way to keep track of every client. The notes will explain how the table is "
         "organised and why it matters for a translator who works with several agencies at the same time. Users can "
         "adapt the idea to their own workflow and come back to it whenever their client list changes over time.")
THIN = "A table for the translator to copy. " * 5  # passes the schema, under the minimum section length
DOCS = {"quick_start": ["Open the file", "Copy the first table into your workspace", "Add your current clients"],
        "faq": [{"question": "Can I change the columns?", "answer": "Yes, they are plain tables."},
                {"question": "How often should I update it?", "answer": "Once a week is enough."}],
        "limits": ["It does not send reminders"]}


def product(*bodies):
    return {"sections": [{"title": t, "body": b} for t, b in zip(TITLES, bodies)]}


def make_ctx(brief, replies, monkeypatch, steps="1"):
    """A real run context with a scripted model. `replies` maps a schema name to a list of replies, used in order
    (the last one repeats). Returns the context and the list of (schema name, task heading) calls."""
    monkeypatch.setenv("REVIEW_STEPS", steps)
    calls = []

    def respond(messages, agent, schema):
        task = re.findall(r"^## Task: (\w+)", " \n".join(m["content"] for m in messages), re.M)
        calls.append((schema.__name__, task[-1] if task else ""))
        queue = replies[schema.__name__]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply if isinstance(reply, str) else json.dumps(reply)

    rec = orch.new_run(brief)
    ctx = orch.RunContext(rec, settings.load(), LLMClient(settings.load(), mock_responder=respond), registry())
    rec.artifacts["phase3_step1"] = {"output": "phase3_step1_mvp_spec.md", "data": SPEC}
    return ctx, calls


def review_entries(ctx):
    return [e for e in ctx.log.entries() if e["action"].startswith("review_")]


# ---------------------------------------------------------------- the generic loop

def test_the_loop_stops_at_the_fixed_count_and_keeps_the_best_draft():
    asked = []

    def revise(draft, defects, step):
        asked.append((step, list(defects)))
        return draft + "x"  # still has the defect

    r = review_loop.run("draft", lambda d: ["too short"], revise, steps=2)
    assert [a[0] for a in asked] == [1, 2] and r.draft == "draft" and r.defects == ["too short"]
    assert [(s["revised"], s["kept"]) for s in r.steps] == [(True, False), (True, False)]
    assert review_loop.run("draft", lambda d: ["a"], revise, steps=99).revisions == review_loop.MAX_STEPS


def test_a_clean_draft_and_a_switched_off_loop_ask_nothing():
    def never(*a):
        raise AssertionError("no revision is asked for")

    assert review_loop.run("ok", lambda d: [], never, steps=3).steps == []
    off = review_loop.run("bad", lambda d: ["defect"], never, steps=0)
    assert off.draft == "bad" and off.steps == [] and off.defects == []


def test_a_better_revision_is_kept_and_a_worse_or_invalid_one_is_dropped():
    drafts = iter(["a b c d", None, "a"])  # worse, invalid, better
    r = review_loop.run("a b", lambda d: d.split()[1:], lambda d, defects, step: next(drafts), steps=3)
    assert r.draft == "a" and r.defects == []
    assert [(s["revised"], s["kept"], s["defects_after"]) for s in r.steps] == [(True, False, 1), (False, False, 1), (True, True, 0)]


# ---------------------------------------------------------------- the code checks

def test_product_checks_find_each_kind_of_defect():
    sections = [{"title": "Client table", "body": "short"},
                {"title": "Deadline calendar", "body": VAGUE},
                {"title": "Extra", "body": GOOD + "\n\n### More\n\nSource: a well-known study\nReference: https://made-up.example/x"}]
    found = builder.product_defects(TITLES, sections, registry())
    text = "\n".join(found)
    assert 'the section "Word count log" from the spec is missing' in text
    assert '"Client table" has 5 characters' in text
    assert '"Deadline calendar" describes its content ("This section offers")' in text
    assert '"Extra" is not in the spec' in text and '"Extra" has headings inside its body' in text
    assert '"Extra" has a "Source:" or "Reference:" line without a fetched URL' in text
    assert builder.product_defects(TITLES, product(GOOD, GOOD, GOOD)["sections"], registry()) == []
    # a phrase such as "users can" is fine when the section also holds the content itself
    assert builder.product_defects(TITLES, product(GOOD + "\n\nUsers can add columns.", GOOD, GOOD)["sections"]) == []


def test_headings_are_demoted_and_unsourced_lines_removed_by_code():
    body = "Intro.\n\n## Part one\n\n### **Step**\n\n```\n# a comment in code\n```\nSource: a study\n- Reference: none\nSource: https://a.example/p"
    assert builder.demote_headings(body).splitlines()[2] == "**Part one**" and "**Step**" in builder.demote_headings(body)
    assert "# a comment in code" in builder.demote_headings(body)
    cleaned, removed = builder.strip_unsourced(body)
    assert removed == 2 and "a study" not in cleaned and "Source: https://a.example/p" in cleaned
    assert builder.strip_unsourced(body, registry())[1] == 3  # with a registry only a fetched URL counts
    assert builder.strip_unsourced("No such line.") == ("No such line.", 0)


def test_docs_checks_flag_what_the_product_does_not_have():
    bad = {"quick_start": ["Sign in to your account", "Download the PDF", "Open the mobile app"],
           "faq": [{"question": "Can I share the link with a colleague?", "answer": "Yes, for free with a subscription."},
                   {"question": "What does it cost?", "answer": "It costs $9."}],
           "limits": ["It has no app and no account"]}
    found = "\n".join(builder.docs_defects(bad, "a Markdown pack"))
    for label in ("an account or sign-in", "a PDF download", "an app", "link sharing", "a subscription", 'a price or "free"'):
        assert f"mentions {label}" in found, label
    assert builder.docs_defects(DOCS, "a Markdown pack") == []
    assert builder.docs_defects({"quick_start": ["Open the PDF"], "faq": []}, "a PDF guide") == []  # the spec has it
    only_limits = dict(DOCS, limits=["It is not an app and needs no account"])
    assert builder.docs_defects(only_limits, "") == []
    third_party = dict(DOCS, faq=[{"question": "Do I need a paid plan?", "answer": "No, the free plan is enough."}] * 2)
    assert builder.docs_defects(third_party, "") == []


# ---------------------------------------------------------------- wired into the builder

@pytest.mark.parametrize("steps", ["1", "2", "3", "7"])
def test_a_defective_product_gets_exactly_the_fixed_number_of_revisions(strong_brief, monkeypatch, steps):
    ctx, calls = make_ctx(strong_brief, {"Product": [product(VAGUE, GOOD, GOOD)]}, monkeypatch, steps=steps)
    res = builder.build_product(ctx)
    want = min(int(steps), review_loop.MAX_STEPS)
    assert calls == [("Product", "build_product")] + [("Product", "review_product")] * want
    entries = review_entries(ctx)
    assert [e["action"] for e in entries] == [f"review_product_step{i}" for i in range(1, want + 1)]
    assert all(e["model"] == "MOCKED-llm" and e["outcome"] == "revision dropped" and e["agent"] == "builder" for e in entries)
    assert len(res.data["review"]["steps"]) == want and len(res.data["review"]["defects_left"]) == 1
    assert ctx.llm.usage.calls == 1 + want  # counted in the run budget like every other call


def test_a_clean_product_gets_no_revision_call(strong_brief, monkeypatch):
    ctx, calls = make_ctx(strong_brief, {"Product": [product(GOOD, GOOD, GOOD)]}, monkeypatch, steps="3")
    res = builder.build_product(ctx)
    assert calls == [("Product", "build_product")] and review_entries(ctx) == []
    assert res.data["review"] == {"steps": [], "defects_left": []} and all(res.data["tests"].values())


def test_review_steps_zero_keeps_the_single_call(strong_brief, monkeypatch):
    ctx, calls = make_ctx(strong_brief, {"Product": [product(VAGUE, THIN, GOOD)]}, monkeypatch, steps="0")
    res = builder.build_product(ctx)
    assert calls == [("Product", "build_product")] and review_entries(ctx) == []
    assert VAGUE in res.content and not res.data["tests"]["every section has enough content"]


def test_a_better_revision_replaces_the_draft_and_the_prompt_lists_the_defects(strong_brief, monkeypatch):
    ctx, calls = make_ctx(strong_brief, {"Product": [product(VAGUE, GOOD, GOOD), product(GOOD, GOOD, GOOD)]}, monkeypatch)
    seen = []
    inner = ctx.llm.mock_responder
    ctx.llm.mock_responder = lambda messages, agent, schema: (seen.append(messages), inner(messages, agent, schema))[1]
    res = builder.build_product(ctx)
    assert len(calls) == 2 and VAGUE not in res.content and res.data["review"]["defects_left"] == []
    assert review_entries(ctx)[0]["outcome"] == "revision kept"
    revise_user = seen[1][-1]["content"]
    assert '1. the section "Client table" describes its content' in revise_user and "Your draft, as JSON:" in revise_user
    assert "## Task: review_product" in " ".join(m["content"] for m in seen[1] if m["role"] == "system")


def test_a_worse_or_invalid_revision_is_discarded(strong_brief, monkeypatch):
    worse = product(VAGUE, THIN, THIN)
    ctx, calls = make_ctx(strong_brief, {"Product": [product(VAGUE, GOOD, GOOD), worse, "not json", "not json"]}, monkeypatch, steps="2")
    res = builder.build_product(ctx)
    assert VAGUE in res.content and res.content.count(GOOD.splitlines()[0]) == 2  # the first draft is the one written
    assert [e["outcome"] for e in review_entries(ctx)] == ["revision dropped", "reply invalid"]
    assert len(calls) == 4  # the draft, one revision, and the invalid reply with its one schema retry


def test_inner_headings_are_demoted_so_the_sections_match_the_spec(strong_brief, monkeypatch):
    split = GOOD + "\n\n## A second lesson\n\nMore text for the same section.\n\n### Detail\n\nSource: common knowledge"
    ctx, _ = make_ctx(strong_brief, {"Product": [product(split, split, split)]}, monkeypatch, steps="0")
    res = builder.build_product(ctx)
    assert re.findall(r"(?m)^## (.+)$", res.content) == TITLES and "**A second lesson**" in res.content
    assert not re.search(r"(?m)^###", res.content) and "common knowledge" not in res.content
    assert [s["title"] for s in builder._product_sections(res.content)] == TITLES
    assert res.data["source_lines_removed"] == 3
    logged = [e for e in ctx.log.entries() if e["action"] == "strip_unsourced_lines"]
    assert len(logged) == 1 and logged[0]["outcome"] == "3 removed" and logged[0]["model"] is None


def test_user_docs_are_reviewed_against_the_product(strong_brief, monkeypatch):
    bad = dict(DOCS, quick_start=["Sign in to your account", "Download the PDF", "Add your clients"])
    ctx, calls = make_ctx(strong_brief, {"Docs": [bad, DOCS]}, monkeypatch)
    res = builder.user_docs(ctx)
    assert calls == [("Docs", "user_docs"), ("Docs", "review_user_docs")]
    assert "Sign in" not in res.content and "Open the file" in res.content
    assert [(e["action"], e["outcome"]) for e in review_entries(ctx)] == [("review_user_docs_step1", "revision kept")]

    ctx, calls = make_ctx(strong_brief, {"Docs": [DOCS]}, monkeypatch, steps="3")
    builder.user_docs(ctx)
    assert calls == [("Docs", "user_docs")] and review_entries(ctx) == []


def test_site_lessons_never_outnumber_the_spec_sections(strong_brief, monkeypatch):
    ctx, _ = make_ctx(strong_brief, {"Product": [product(GOOD, GOOD, GOOD)]}, monkeypatch)
    text = "# Name\n\n" + "".join(f"## {t}\n\nText for {t}.\n\n" for t in TITLES + ["Bonus", "Appendix"])
    ctx.write_output("phase3_step2_product.md", text)
    ctx.rec.artifacts["phase3_step2"] = {"output": "phase3_step2_product.md", "data": {}}
    copy = builder.SiteCopy(tagline="A board for translators.", about="Three tables to copy.", summary="Three tables.",
                            for_whom="Freelance translators.")
    item = builder.site_content(ctx, copy)["items"][0]
    assert [s["title"] for s in item["lessons"]] == TITLES and item["whats_inside"] == TITLES


# ---------------------------------------------------------------- the setting

def test_review_steps_setting(monkeypatch):
    monkeypatch.delenv("REVIEW_STEPS", raising=False)
    assert settings.load().review_steps == 1
    monkeypatch.setenv("FAST_MODE", "true")
    assert settings.load().review_steps == 0  # fast mode skips the loop
    monkeypatch.setenv("REVIEW_STEPS", "2")
    assert settings.load().review_steps == 2  # unless it is set explicitly
    for raw, want in (("0", 0), ("9", 3), ("-4", 0), ("", 0), ("abc", 1)):
        monkeypatch.setenv("REVIEW_STEPS", raw)
        assert settings.load().review_steps == want, raw
    env = (settings.ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (settings.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^REVIEW_STEPS=1$", env, re.M) and "REVIEW_STEPS: ${REVIEW_STEPS:-}" in compose

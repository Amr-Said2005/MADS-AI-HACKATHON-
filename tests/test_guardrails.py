import pytest

import guardrails
from decision_log import DecisionLog


@pytest.fixture
def log(tmp_path):
    return DecisionLog(tmp_path / "log.json")


def test_every_trick_prompt_is_refused_and_logged(trick_prompts, log):
    prompts = trick_prompts["must_refuse"]
    for p in prompts:
        res = guardrails.screen_request(p["text"], log)
        assert res.refused, p["text"]
    refused = [e for e in log.entries() if e["action"] == "screen_request" and e.get("outcome") == "refused"]
    assert len(refused) == len(prompts)


def test_normal_instructions_are_allowed(trick_prompts, log):
    for text in trick_prompts["must_allow"]:
        assert not guardrails.screen_request(text, log).refused, text


def test_output_check_blocks_and_passes(trick_prompts, log):
    for text in trick_prompts["outputs_must_block"]:
        assert not guardrails.check_output("x.md", text, log).ok, text
    for text in trick_prompts["outputs_must_pass"]:
        assert guardrails.check_output("x.md", text, log).ok, text
    assert all(e["agent"] == "guardrails" for e in log.entries())


def _brief(**kw):
    b = {"idea": "A Notion template pack for freelance translators", "skills": ["Notion"], "budget_usd": 50,
         "hours_per_week": 5, "target_market": "freelance translators", "values": ["honesty"]}
    b.update(kw)
    return b


@pytest.mark.parametrize("idea,category", [
    ("Print-on-demand t-shirts for cat lovers", "physical goods"),
    ("A crypto trading signals newsletter", "financial, investment or crypto product"),
    ("A weight loss meal plan with supplements", "health advice product"),
    ("Legal advice templates for tenants", "legal advice product"),
])
def test_brief_screen_stops_excluded_categories(idea, category, log):
    res = guardrails.screen_brief(_brief(idea=idea), log)
    assert not res.allowed
    assert res.category == category
    assert log.entries()[-1]["outcome"] == "stop"


def test_brief_screen_stops_borderline_with_reason(log):
    res = guardrails.screen_brief(_brief(idea="A fitness tracker spreadsheet"), log)
    assert not res.allowed and "borderline" in res.reasons[0]


def test_brief_screen_founder_fit(log):
    assert not guardrails.screen_brief(_brief(hours_per_week=0), log).allowed
    assert guardrails.screen_brief(_brief(), log).allowed


def test_brief_missing_key_has_clear_message():
    b = _brief()
    del b["values"]
    with pytest.raises(guardrails.BriefError, match="missing: values"):
        guardrails.validate_brief(b)

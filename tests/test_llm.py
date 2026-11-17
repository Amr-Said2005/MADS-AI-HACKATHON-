"""Model client: both backends, schema validation with one retry, health. Offline via httpx.MockTransport."""

import json

import httpx
import pytest
from pydantic import BaseModel

import settings
from llm import LLMClient, LongText, ModelOutputInvalid, ShortText


class Opportunity(BaseModel):
    title: ShortText
    summary: LongText


GOOD = json.dumps({"title": "Client tracker", "summary": "Freelance translators lose track of deadlines across agencies."})


def live(monkeypatch, **env):
    monkeypatch.setenv("LLM_MODE", "live")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return settings.load()


def client(s, handler):
    return LLMClient(s, transport=httpx.MockTransport(handler))


def ollama_reply(content, pt=100, ct=50):
    return httpx.Response(200, json={"model": "qwen3:4b-instruct-2507-q4_K_M", "message": {"content": content},
                                     "prompt_eval_count": pt, "eval_count": ct})


def test_ollama_backend_sends_schema_and_no_thinking(monkeypatch):
    s = live(monkeypatch)
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return ollama_reply(GOOD)

    obj, res = client(s, handler).structured([{"role": "user", "content": "find one"}], Opportunity, agent="researcher")
    path, body = seen[0]
    assert path == "/api/chat" and body["think"] is False and body["format"]["title"] == "Opportunity"
    assert body["options"]["num_ctx"] == 8192 and "JSON schema" in body["messages"][0]["content"]
    assert obj.title == "Client tracker" and res.prompt_tokens == 100 and res.completion_tokens == 50


def test_invalid_reply_is_retried_once_then_fails(monkeypatch):
    s = live(monkeypatch)
    replies = iter([json.dumps({"title": "x", "summary": "short"}), GOOD])
    c = client(s, lambda req: ollama_reply(next(replies)))
    obj, _ = c.structured([{"role": "user", "content": "go"}], Opportunity, agent="researcher")
    assert obj.title == "Client tracker" and c.usage.calls == 2

    c = client(s, lambda req: ollama_reply("not json at all"))
    with pytest.raises(ModelOutputInvalid, match="model_output_invalid"):
        c.structured([{"role": "user", "content": "go"}], Opportunity, agent="researcher")
    assert c.usage.calls == 2


def test_openai_backend_uses_response_format(monkeypatch):
    s = live(monkeypatch, LLM_BACKEND="openai", LLM_BASE_URL="https://llm.example/v1", LLM_MODEL="open-model",
             LLM_PRICE_IN_PER_MTOK="1", LLM_PRICE_OUT_PER_MTOK="2")
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content), req.headers.get("authorization")))
        return httpx.Response(200, json={"model": "open-model", "choices": [{"message": {"content": GOOD}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    c = client(s, handler)
    obj, res = c.structured([{"role": "user", "content": "go"}], Opportunity, agent="scorer")
    url, body, auth = seen[0]
    assert url == "https://llm.example/v1/chat/completions" and body["response_format"]["type"] == "json_schema"
    assert auth == "Bearer placeholder" and abs(res.cost - 0.002) < 1e-9 and c.usage.total_tokens == 1500


def test_mock_mode_structured_never_invents_content(monkeypatch):
    c = LLMClient(settings.load())  # conftest sets LLM_MODE=mock
    with pytest.raises(ModelOutputInvalid):
        c.structured([{"role": "user", "content": "go"}], Opportunity, agent="researcher")
    c = LLMClient(settings.load(), mock_responder=lambda m, a, s: GOOD)
    assert c.structured([{"role": "user", "content": "go"}], Opportunity, agent="researcher")[0].title == "Client tracker"


def test_call_guard_runs_before_every_call(monkeypatch):
    c = LLMClient(settings.load())
    calls = []
    c.before_call = lambda: calls.append(1)
    c.chat([{"role": "user", "content": "hi"}], agent="x")
    assert calls == [1]


def test_health_while_model_is_downloading(monkeypatch):
    s = live(monkeypatch)
    h = client(s, lambda req: httpx.Response(200, json={"models": []})).health()
    assert not h["ok"] and "downloading" in h["message"]


def test_health_when_endpoint_is_down(monkeypatch):
    s = live(monkeypatch)

    def down(req):
        raise httpx.ConnectError("refused")

    h = client(s, down).health()
    assert not h["ok"] and "not reachable" in h["message"]


def test_health_when_model_is_ready(monkeypatch):
    s = live(monkeypatch)
    h = client(s, lambda req: httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct-2507-q4_K_M"}]})).health()
    assert h["ok"]


def test_health_openai_backend(monkeypatch):
    s = live(monkeypatch, LLM_BACKEND="openai", LLM_BASE_URL="https://llm.example/v1", LLM_MODEL="m")
    h = client(s, lambda req: httpx.Response(200, json={"data": None})).health()
    assert not h["ok"]
    h = client(s, lambda req: httpx.Response(200, json={"data": [{"id": "m"}]})).health()
    assert h["ok"]


def test_thinking_is_off_except_for_models_that_cannot_switch_it_off(monkeypatch):
    def think_sent(**env):
        s = live(monkeypatch, **env)
        seen = []
        c = client(s, lambda req: (seen.append(json.loads(req.content)), ollama_reply(GOOD))[1])
        c.structured([{"role": "user", "content": "go"}], Opportunity, agent="researcher")
        return seen[0]["think"]

    assert think_sent(LLM_MODEL="qwen3:4b-instruct-2507-q4_K_M") is False
    assert think_sent(LLM_MODEL="gpt-oss:20b") == "low"
    assert think_sent(LLM_MODEL="gpt-oss:20b", LLM_THINK="medium") == "medium"
    assert think_sent(LLM_MODEL="qwen3:4b-instruct-2507-q4_K_M", LLM_THINK="true") is True
    monkeypatch.setenv("LLM_THINK", "sometimes")
    with pytest.raises(ValueError, match="LLM_THINK"):
        settings.load()

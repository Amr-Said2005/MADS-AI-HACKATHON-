"""Model client: health messages and token/cost counting, offline via httpx.MockTransport."""

import httpx

import settings
from llm import LLMClient


def live(monkeypatch, **env):
    monkeypatch.setenv("LLM_MODE", "live")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return settings.load()


def client(s, handler):
    return LLMClient(s, transport=httpx.MockTransport(handler))


def test_health_while_model_is_still_downloading(monkeypatch):
    s = live(monkeypatch, LLM_MODEL="qwen3:8b")
    h = client(s, lambda req: httpx.Response(200, json={"object": "list", "data": None})).health()
    assert not h["ok"] and "still downloading" in h["message"]


def test_health_when_endpoint_is_down(monkeypatch):
    s = live(monkeypatch)

    def down(req):
        raise httpx.ConnectError("refused")

    h = client(s, down).health()
    assert not h["ok"] and "not reachable" in h["message"]


def test_health_when_model_is_ready(monkeypatch):
    s = live(monkeypatch, LLM_MODEL="qwen3:8b")
    h = client(s, lambda req: httpx.Response(200, json={"data": [{"id": "qwen3:8b"}]})).health()
    assert h["ok"]


def test_chat_counts_tokens_cost_and_strips_thinking(monkeypatch):
    s = live(monkeypatch, LLM_PRICE_IN_PER_MTOK="1", LLM_PRICE_OUT_PER_MTOK="2")
    reply = {"model": "qwen3:8b", "choices": [{"message": {"content": "<think>hmm</think>Answer"}}],
             "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    c = client(s, lambda req: httpx.Response(200, json=reply))
    r = c.chat([{"role": "user", "content": "hi"}], agent="researcher")
    assert r.text == "Answer" and r.model == "qwen3:8b"
    assert abs(r.cost - 0.002) < 1e-9
    assert c.usage.total_tokens == 1500

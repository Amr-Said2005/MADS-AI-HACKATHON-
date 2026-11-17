"""One client for any OpenAI-compatible chat endpoint (Ollama locally, or a free hosted endpoint).

Base URL, key and model come from the environment (see settings.py).
LLM_MODE=mock returns canned text so smoke tests run with no model and no network.
Mock output is labelled MOCKED and the orchestrator never writes it to outputs/ or
the root decision_log.json.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

import httpx

from settings import Settings

MOCK_MODEL_NAME = "MOCKED-llm"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    model: str | None  # "MOCKED-llm" in mock mode, so the log shows no real model was used
    prompt_tokens: int
    completion_tokens: int
    cost: float
    estimated_tokens: bool = False


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, r: LLMResult) -> None:
        with self._lock:
            self.prompt_tokens += r.prompt_tokens
            self.completion_tokens += r.completion_tokens
            self.cost += r.cost
            self.calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost, 6),
        }


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.usage = Usage()
        self._transport = transport

    @property
    def mocked(self) -> bool:
        return self.s.mock

    @property
    def model_label(self) -> str:
        return MOCK_MODEL_NAME if self.mocked else self.s.llm_model

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.s.price_in_per_mtok + completion_tokens * self.s.price_out_per_mtok) / 1_000_000

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        agent: str,
        temperature: float = 0.3,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResult:
        if self.mocked:
            result = self._mock(messages, agent)
        else:
            result = self._live(messages, temperature, json_mode, max_tokens)
        self.usage.add(result)
        return result

    def _mock(self, messages: list[dict[str, str]], agent: str) -> LLMResult:
        prompt = " ".join(m.get("content", "") for m in messages)
        text = f"MOCKED output for agent '{agent}'. No model was called."
        pt, ct = estimate_tokens(prompt), estimate_tokens(text)
        return LLMResult(text, MOCK_MODEL_NAME, pt, ct, 0.0, estimated_tokens=True)

    def _live(self, messages, temperature, json_mode, max_tokens) -> LLMResult:
        body: dict = {"model": self.s.llm_model, "messages": messages, "temperature": temperature, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.s.llm_api_key}"}
        try:
            with httpx.Client(timeout=self.s.llm_timeout_seconds, transport=self._transport) as c:
                r = c.post(f"{self.s.llm_base_url}/chat/completions", json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"model call failed ({self.s.llm_base_url}, {self.s.llm_model}): {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from exc
        text = _THINK.sub("", text).strip()
        usage = data.get("usage") or {}
        estimated = "prompt_tokens" not in usage
        pt = int(usage.get("prompt_tokens") or estimate_tokens(" ".join(m["content"] for m in messages)))
        ct = int(usage.get("completion_tokens") or estimate_tokens(text))
        return LLMResult(text, data.get("model") or self.s.llm_model, pt, ct, self._cost(pt, ct), estimated)

    def health(self) -> dict:
        """Whether the endpoint answers and the model is available. Never raises."""
        if self.mocked:
            return {"ok": True, "mode": "mock", "model": MOCK_MODEL_NAME,
                    "message": "LLM_MODE=mock: canned text, no model is called (MOCKED)."}
        try:
            with httpx.Client(timeout=5, transport=self._transport) as c:
                r = c.get(f"{self.s.llm_base_url}/models", headers={"Authorization": f"Bearer {self.s.llm_api_key}"})
                r.raise_for_status()
                body = r.json()
            # Ollama answers {"data": null} before any model has been pulled.
            data = body.get("data") if isinstance(body, dict) else None
            ids = [str(m.get("id", "")) for m in (data or []) if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "mode": "live", "model": self.s.llm_model,
                    "message": f"Model endpoint not reachable yet at {self.s.llm_base_url} ({type(exc).__name__}). "
                               "If Ollama just started it may still be starting."}
        wanted = self.s.llm_model
        present = any(i == wanted or i == f"{wanted}:latest" or i.split(":")[0] == wanted for i in ids)
        if not present:
            return {"ok": False, "mode": "live", "model": wanted,
                    "message": f"Model {wanted} is not available yet; it is probably still downloading. "
                               "Runs can start once it appears. Available now: " + (", ".join(ids) or "none")}
        return {"ok": True, "mode": "live", "model": wanted, "message": f"Model {wanted} is ready."}

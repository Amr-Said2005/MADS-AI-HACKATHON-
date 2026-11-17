"""One model client, two backends, chosen by LLM_BACKEND.

- ollama: Ollama's native /api/chat, with `format` set to the reply's JSON schema and thinking off.
- openai: any OpenAI-compatible /chat/completions endpoint (a hosted open-weight model),
          with `response_format` set to the JSON schema.

Agents ask for structured replies with `structured(messages, Schema)`: the schema text goes
into the prompt, the reply is validated with Pydantic (text fields carry minimum lengths),
and an invalid reply is retried once with the validation error. A second failure raises
ModelOutputInvalid ("model_output_invalid"); the orchestrator then repeats or stops the
step. No placeholder content is ever written in its place.

LLM_MODE=mock makes no model call: `chat` returns canned text labelled MOCKED and
`structured` raises ModelOutputInvalid unless a test supplies `mock_responder`. Nothing
produced in mock mode is ever copied to outputs/ or the root decision_log.json.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, StringConstraints, ValidationError

from settings import Settings

# Requirement R-REAL-OUTPUTS: in mock mode no model is called and every result carries this model
# name, so the run's decision log shows it; promote() never copies such a run into outputs/.
# Verified by: tests/test_agents.py::test_phase1_slice_with_the_real_agents_reaches_a_go_recommendation
MOCK_MODEL_NAME = "MOCKED-llm"
MODEL_OUTPUT_INVALID = "model_output_invalid"

# Text field types for agent schemas: stripped, with a minimum length, so an empty or
# one-word reply fails validation instead of reaching an output file.
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=400)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=40)]

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model endpoint could not be reached or answered with an error. The run pauses (blocked)."""


class ModelOutputInvalid(RuntimeError):
    """The model answered twice, but neither reply matched the schema."""

    reason = MODEL_OUTPUT_INVALID


class CallBudgetExceeded(RuntimeError):
    """MAX_MODEL_CALLS reached for this run."""


@dataclass
class LLMResult:
    text: str
    model: str | None  # "MOCKED-llm" in mock mode, so the log shows no real model was used
    prompt_tokens: int
    completion_tokens: int
    cost: float
    estimated_tokens: bool = False


# Requirement R-COST: every model call adds its prompt tokens, completion tokens and cost to this
# total; the cost is tokens times the configured price per million tokens (0 by default).
# Verified by: tests/test_llm.py::test_openai_backend_uses_response_format
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
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def schema_prompt(schema: type[BaseModel]) -> str:
    return ("Reply with one JSON object only, no other text, matching this JSON schema exactly:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False))


# Requirement R-OPEN-MODEL: every live model call is an HTTP request to the endpoint set by
# LLM_BACKEND and LLM_BASE_URL: Ollama's /api/chat by default, or an OpenAI-compatible endpoint.
# Verified by: tests/test_llm.py::test_ollama_backend_sends_schema_and_no_thinking
class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None,
                 mock_responder: Callable[[list[dict[str, str]], str, type[BaseModel] | None], str] | None = None):
        self.s = settings
        self.usage = Usage()
        self._transport = transport
        self.mock_responder = mock_responder
        self.before_call: Callable[[], None] | None = None  # the orchestrator's run-budget check

    @property
    def mocked(self) -> bool:
        return self.s.mock

    @property
    def model_label(self) -> str:
        return MOCK_MODEL_NAME if self.mocked else self.s.llm_model

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.s.price_in_per_mtok + completion_tokens * self.s.price_out_per_mtok) / 1_000_000

    # ------------------------------------------------------------ public API

    def chat(self, messages: list[dict[str, str]], *, agent: str, temperature: float = 0.3,
             max_tokens: int | None = None) -> LLMResult:
        """Free-text reply. Prefer `structured` for anything that feeds a gate or an output file."""
        return self._call(messages, agent, None, temperature, max_tokens)

    def structured(self, messages: list[dict[str, str]], schema: type[T], *, agent: str,
                   temperature: float = 0.2, max_tokens: int | None = None) -> tuple[T, LLMResult]:
        """A reply validated against `schema`, with one retry. Raises ModelOutputInvalid on a second failure."""
        msgs = [dict(m) for m in messages]
        msgs.insert(0, {"role": "system", "content": schema_prompt(schema)})
        error = ""
        for attempt in (1, 2):
            if error:
                msgs = msgs + [{"role": "user", "content": f"Your previous reply was invalid: {error}. "
                                                           "Reply again with one JSON object that matches the schema."}]
            result = self._call(msgs, agent, schema, temperature, max_tokens)
            try:
                return schema.model_validate_json(_FENCE.sub("", result.text.strip())), result
            except ValidationError as exc:
                error = "; ".join(f"{'.'.join(str(x) for x in e['loc']) or 'reply'}: {e['msg']}" for e in exc.errors()[:6])
            except ValueError as exc:
                error = f"not valid JSON ({exc})"
        raise ModelOutputInvalid(f"{MODEL_OUTPUT_INVALID}: {agent} reply did not match {schema.__name__} twice: {error}")

    # ------------------------------------------------------------ backends

    def _call(self, messages, agent, schema, temperature, max_tokens) -> LLMResult:
        if self.before_call:
            self.before_call()
        if self.mocked:
            result = self._mock(messages, agent, schema)
        elif self.s.llm_backend == "ollama":
            result = self._ollama(messages, schema, temperature, max_tokens)
        else:
            result = self._openai(messages, schema, temperature, max_tokens)
        self.usage.add(result)
        return result

    def _mock(self, messages, agent, schema) -> LLMResult:
        prompt = " ".join(m.get("content", "") for m in messages)
        if self.mock_responder:
            text = self.mock_responder(messages, agent, schema)
        else:
            text = f"MOCKED output for agent '{agent}'. No model was called."
        return LLMResult(text, MOCK_MODEL_NAME, estimate_tokens(prompt), estimate_tokens(text), 0.0, estimated_tokens=True)

    def _post(self, url: str, body: dict, headers: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=self.s.llm_timeout_seconds, transport=self._transport) as c:
                r = c.post(url, json=body, headers=headers or {})
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"model call failed ({self.s.llm_backend} at {self.s.llm_base_url}, {self.s.llm_model}): {exc}") from exc

    def _think(self) -> bool | str:
        """Ollama's `think` value. Thinking is off by default: it costs tokens and time. The gpt-oss
        models cannot switch it off and take a level instead, so "auto" gives them the lowest one."""
        t = self.s.llm_think
        if t == "auto":
            return "low" if self.s.llm_model.lower().startswith("gpt-oss") else False
        return {"false": False, "true": True}.get(t, t)

    def _ollama(self, messages, schema, temperature, max_tokens) -> LLMResult:
        options: dict[str, Any] = {"temperature": temperature, "num_ctx": self.s.llm_num_ctx}
        if max_tokens:
            options["num_predict"] = max_tokens
        body: dict[str, Any] = {"model": self.s.llm_model, "messages": messages, "stream": False,
                                "think": self._think(), "options": options}
        if schema is not None:
            body["format"] = schema.model_json_schema()
        data = self._post(f"{self.s.llm_base_url}/api/chat", body)
        try:
            text = (data.get("message") or {}).get("content") or ""
        except AttributeError as exc:
            raise LLMError(f"unexpected response shape: {str(data)[:200]}") from exc
        text = _THINK.sub("", text).strip()
        pt, ct = data.get("prompt_eval_count"), data.get("eval_count")
        estimated = pt is None or ct is None
        pt = int(pt if pt is not None else estimate_tokens(" ".join(m["content"] for m in messages)))
        ct = int(ct if ct is not None else estimate_tokens(text))
        return LLMResult(text, data.get("model") or self.s.llm_model, pt, ct, self._cost(pt, ct), estimated)

    def _openai(self, messages, schema, temperature, max_tokens) -> LLMResult:
        body: dict[str, Any] = {"model": self.s.llm_model, "messages": messages, "temperature": temperature, "stream": False}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}
        if max_tokens:
            body["max_tokens"] = max_tokens
        data = self._post(f"{self.s.llm_base_url}/chat/completions", body,
                          {"Authorization": f"Bearer {self.s.llm_api_key}"})
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

    # ------------------------------------------------------------ health

    def health(self) -> dict:
        """Whether the endpoint answers and the model is present. Never raises."""
        if self.mocked:
            return {"ok": True, "mode": "mock", "model": MOCK_MODEL_NAME,
                    "message": "LLM_MODE=mock: canned text, no model is called (MOCKED)."}
        wanted = self.s.llm_model
        ollama = self.s.llm_backend == "ollama"
        try:
            with httpx.Client(timeout=5, transport=self._transport) as c:
                if ollama:
                    r = c.get(f"{self.s.llm_base_url}/api/tags")
                else:
                    r = c.get(f"{self.s.llm_base_url}/models", headers={"Authorization": f"Bearer {self.s.llm_api_key}"})
                r.raise_for_status()
                body = r.json()
            items = (body.get("models") if ollama else body.get("data")) if isinstance(body, dict) else None
            key = "name" if ollama else "id"
            ids = [str(m.get(key) or m.get("model") or "") for m in (items or []) if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "mode": "live", "model": wanted,
                    "message": f"Model endpoint not reachable yet at {self.s.llm_base_url} ({type(exc).__name__}). "
                               "If Ollama just started it may still be starting."}
        present = any(i == wanted or i == f"{wanted}:latest" for i in ids)
        if not present:
            return {"ok": False, "mode": "live", "model": wanted,
                    "message": f"Model {wanted} is downloading or not available yet. "
                               "Runs can start once it appears. Available now: " + (", ".join(ids) or "none")}
        return {"ok": True, "mode": "live", "model": wanted, "message": f"Model {wanted} is ready."}

"""Runtime settings, read from the environment. Every key is listed in .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
ROOT_LOG = ROOT / "decision_log.json"
RUNS_DIR = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))
BRIEFS_DIR = ROOT / "briefs"
PROMPTS_DIR = ROOT / "prompts"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
BACKENDS = ("ollama", "openai")


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_mode: str  # "live" or "mock"
    fast_mode: bool
    max_sources: int
    price_in_per_mtok: float
    price_out_per_mtok: float
    llm_timeout_seconds: float
    n_architects: int = 1  # Phase 1 planners (and roadmap proposals); always 1 in fast mode
    llm_backend: str = "ollama"  # "ollama" (native /api/chat with a JSON schema) or "openai" (hosted endpoint)
    searxng_url: str = ""
    max_model_calls: int = 60  # run budget: model calls per run
    max_run_seconds: float = 900.0  # run budget: seconds spent running (not waiting for the founder)
    llm_num_ctx: int = 8192

    @property
    def mock(self) -> bool:
        return self.llm_mode == "mock"


def load() -> Settings:
    mode = os.environ.get("LLM_MODE", "live").strip().lower()
    if mode not in {"live", "mock"}:
        raise ValueError(f"LLM_MODE must be 'live' or 'mock', got {mode!r}")
    backend = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"LLM_BACKEND must be one of {BACKENDS}, got {backend!r}")
    fast = _bool(os.environ.get("FAST_MODE"))
    max_sources = int(_float(os.environ.get("MAX_SOURCES"), 6))
    n_architects = max(1, int(_float(os.environ.get("N_ARCHITECTS"), 1)))
    if fast:
        max_sources = min(max_sources, 3)
        n_architects = 1
    default_url = "http://localhost:11434" if backend == "ollama" else ""
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", default_url).rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "placeholder"),
        llm_model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        llm_mode=mode,
        fast_mode=fast,
        max_sources=max(1, max_sources),
        price_in_per_mtok=_float(os.environ.get("LLM_PRICE_IN_PER_MTOK"), 0.0),
        price_out_per_mtok=_float(os.environ.get("LLM_PRICE_OUT_PER_MTOK"), 0.0),
        llm_timeout_seconds=_float(os.environ.get("LLM_TIMEOUT_SECONDS"), 300.0),
        n_architects=n_architects,
        llm_backend=backend,
        searxng_url=os.environ.get("SEARXNG_URL", "").rstrip("/"),
        max_model_calls=max(1, int(_float(os.environ.get("MAX_MODEL_CALLS"), 60))),
        max_run_seconds=max(1.0, _float(os.environ.get("MAX_RUN_SECONDS"), 900.0)),
        llm_num_ctx=max(512, int(_float(os.environ.get("LLM_NUM_CTX"), 8192))),
    )

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

    @property
    def mock(self) -> bool:
        return self.llm_mode == "mock"


def load() -> Settings:
    mode = os.environ.get("LLM_MODE", "live").strip().lower()
    if mode not in {"live", "mock"}:
        raise ValueError(f"LLM_MODE must be 'live' or 'mock', got {mode!r}")
    fast = _bool(os.environ.get("FAST_MODE"))
    max_sources = int(_float(os.environ.get("MAX_SOURCES"), 6))
    if fast:
        max_sources = min(max_sources, 3)
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "placeholder"),
        llm_model=os.environ.get("LLM_MODEL", "qwen3:8b"),
        llm_mode=mode,
        fast_mode=fast,
        max_sources=max(1, max_sources),
        price_in_per_mtok=_float(os.environ.get("LLM_PRICE_IN_PER_MTOK"), 0.0),
        price_out_per_mtok=_float(os.environ.get("LLM_PRICE_OUT_PER_MTOK"), 0.0),
        llm_timeout_seconds=_float(os.environ.get("LLM_TIMEOUT_SECONDS"), 300.0),
    )

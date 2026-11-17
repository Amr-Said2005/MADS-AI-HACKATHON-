import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings  # noqa: E402


@pytest.fixture(autouse=True)
def offline_mock_env(tmp_path, monkeypatch):
    """Every test runs in LLM_MODE=mock with runs in a temp folder: no model, no network, no repo writes."""
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("FAST_MODE", "false")
    monkeypatch.setenv("MAX_SOURCES", "10")
    monkeypatch.setattr(settings, "RUNS_DIR", tmp_path / "runs")
    yield


@pytest.fixture
def trick_prompts():
    return json.loads((ROOT / "tests" / "trick_prompts.json").read_text(encoding="utf-8"))


@pytest.fixture
def strong_brief():
    return json.loads((ROOT / "briefs" / "example_strong.json").read_text(encoding="utf-8"))

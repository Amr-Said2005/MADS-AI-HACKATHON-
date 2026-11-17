"""Append-only writer for decision logs.

This module is the only code that writes a decision log. Every entry is checked
against the schema in CLAUDE.md before it is written; a missing or malformed key
raises DecisionLogError and nothing is written.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")
OPTIONAL_KEYS = ("outcome",)

_lock = threading.Lock()


class DecisionLogError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate(entry: dict[str, Any]) -> None:
    if not isinstance(entry, dict):
        raise DecisionLogError("entry must be a dict")
    missing = [k for k in REQUIRED_KEYS if k not in entry]
    if missing:
        raise DecisionLogError(f"decision log entry is missing required keys: {missing}")
    unknown = [k for k in entry if k not in REQUIRED_KEYS + OPTIONAL_KEYS]
    if unknown:
        raise DecisionLogError(f"decision log entry has unknown keys: {unknown}")

    ts = entry["timestamp"]
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise DecisionLogError("timestamp must be a UTC ISO 8601 string ending in 'Z'")
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionLogError(f"timestamp is not ISO 8601: {ts!r}") from exc

    for key in ("agent", "action"):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise DecisionLogError(f"{key} must be a non-empty string")

    sources = entry["sources"]
    if not isinstance(sources, list) or not all(
        isinstance(s, str) and s.startswith(("http://", "https://")) for s in sources
    ):
        raise DecisionLogError("sources must be a list of http(s) URLs (may be empty)")

    if entry["model"] is not None and not isinstance(entry["model"], str):
        raise DecisionLogError("model must be a string or null")

    cost = entry["cost"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        raise DecisionLogError("cost must be a non-negative number in USD")

    if entry["approved_by"] not in ("founder", None):
        raise DecisionLogError("approved_by must be 'founder' or null")

    if "outcome" in entry and not isinstance(entry["outcome"], str):
        raise DecisionLogError("outcome must be a string")

    if entry["action"].startswith("mocked_") and entry["model"] is not None:
        raise DecisionLogError("mocked actions must carry model: null")


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(data, list):
        raise DecisionLogError(f"{path} must contain a JSON list")
    return data


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        # A single-file bind mount (docker compose) cannot be replaced; write in place.
        path.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)


def append(entry: dict[str, Any], path: Path) -> dict[str, Any]:
    """Validate and append one entry. Raises DecisionLogError on any schema problem."""
    validate(entry)
    with _lock:
        entries = read(path)
        entries.append(entry)
        _write(path, entries)
    return entry


def reset(path: Path) -> None:
    """Start a log over as an empty list (used before promoting a fresh demo run)."""
    with _lock:
        _write(path, [])


class DecisionLog:
    """Convenience wrapper bound to one log file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def record(
        self,
        agent: str,
        action: str,
        *,
        sources: list[str] | None = None,
        model: str | None = None,
        cost: float = 0,
        approved_by: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "timestamp": utc_now(),
            "agent": agent,
            "action": action,
            "sources": list(sources or []),
            "model": model,
            "cost": round(float(cost), 6),
            "approved_by": approved_by,
        }
        if outcome is not None:
            entry["outcome"] = outcome
        return append(entry, self.path)

    def entries(self) -> list[dict[str, Any]]:
        return read(self.path)

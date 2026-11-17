"""Founder approve/reject, enforced in code.

Any function that publishes, sends, spends or signs is decorated with
@requires_approval. It must take `approvals` (an ApprovalStore) and `action_id`
parameters. Calling it raises ApprovalRequired unless the founder has approved
that exact action id, and ApprovalRejected if the founder rejected it.
Mocked outbound actions go through the same decorator.

Decisions are written only by ApprovalStore.decide(), which is called only by the
CLI prompt (prompt_cli) or the web UI route in app/main.py.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import threading
from pathlib import Path
from typing import Any, Callable

from decision_log import DecisionLog, utc_now

_lock = threading.Lock()


class ApprovalError(Exception):
    def __init__(self, action_id: str, message: str):
        super().__init__(message)
        self.action_id = action_id


class ApprovalRequired(ApprovalError):
    pass


class ApprovalRejected(ApprovalError):
    pass


def make_action_id(kind: str, run_id: str, payload: str | bytes = b"") -> str:
    """Action ids bind an approval to one run and one exact piece of content."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return f"{kind}:{run_id}:{hashlib.sha256(data).hexdigest()[:12]}"


class ApprovalStore:
    """Approval requests and decisions for one run, persisted as JSON."""

    def __init__(self, path: Path, log: DecisionLog | None = None):
        self.path = Path(path)
        self.log = log

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def request(self, action_id: str, summary: str) -> None:
        with _lock:
            data = self._load()
            if action_id not in data:
                data[action_id] = {"summary": summary, "requested_at": utc_now(), "decision": None}
                self._save(data)

    def decision(self, action_id: str) -> str | None:
        return self._load().get(action_id, {}).get("decision")

    def record(self, action_id: str) -> dict[str, Any] | None:
        return self._load().get(action_id)

    def pending(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._load().items() if v.get("decision") is None}

    def all(self) -> dict[str, dict[str, Any]]:
        return self._load()

    def decide(self, action_id: str, approve: bool, on_reject: str = "stop", note: str = "") -> None:
        if on_reject not in ("repeat", "stop"):
            raise ValueError("on_reject must be 'repeat' or 'stop'")
        with _lock:
            data = self._load()
            if action_id not in data:
                raise KeyError(f"no approval request with id {action_id!r}")
            if data[action_id].get("decision") is not None:
                raise ValueError(f"{action_id!r} was already decided")
            data[action_id].update(
                decision="approved" if approve else "rejected",
                decided_at=utc_now(),
                decided_by="founder",
                on_reject=None if approve else on_reject,
                note=note,
            )
            self._save(data)
        if self.log is not None:
            self.log.record(
                "founder",
                f"{'approve' if approve else 'reject'}:{action_id}",
                approved_by="founder" if approve else None,
                outcome="approved" if approve else f"rejected_{on_reject}",
            )


def requires_approval(fn: Callable[..., Any]) -> Callable[..., Any]:
    sig = inspect.signature(fn)
    for name in ("approvals", "action_id"):
        if name not in sig.parameters:
            raise TypeError(f"@requires_approval function {fn.__name__} must take a '{name}' parameter")

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        store = bound.arguments["approvals"]
        action_id = bound.arguments["action_id"]
        if not isinstance(store, ApprovalStore):
            raise TypeError("approvals must be an ApprovalStore")
        decision = store.decision(action_id)
        if decision is None:
            store.request(action_id, f"{fn.__module__}.{fn.__name__}")
            raise ApprovalRequired(action_id, f"founder approval required for {action_id}")
        if decision != "approved":
            raise ApprovalRejected(action_id, f"founder rejected {action_id}")
        return fn(*args, **kwargs)

    wrapper.__requires_approval__ = True  # type: ignore[attr-defined]
    return wrapper


def prompt_cli(store: ApprovalStore, action_id: str, input_fn: Callable[[str], str] = input) -> bool:
    """Ask the founder on the terminal. Returns True if approved."""
    rec = store.record(action_id) or {}
    print(f"\nFounder approval needed: {action_id}\n  {rec.get('summary', '')}")
    answer = input_fn("Approve? [y/N] ").strip().lower()
    if answer in ("y", "yes"):
        store.decide(action_id, True)
        return True
    choice = input_fn("Rejected. Repeat this step or stop the run? [repeat/STOP] ").strip().lower()
    store.decide(action_id, False, on_reject="repeat" if choice == "repeat" else "stop")
    return False

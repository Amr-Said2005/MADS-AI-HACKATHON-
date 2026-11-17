"""Founder approve/reject, enforced in code.

Any function that publishes, sends, spends or signs is decorated with
@requires_approval. It must take `approvals` (an ApprovalStore) and `action_id`
parameters. Calling it raises ApprovalRequired unless the founder has approved
that exact action id, and ApprovalRejected if the founder rejected it.
Mocked outbound actions go through the same decorator.

Founder decisions with several choices (roadmap, narrowed idea, milestone review) are
recorded by ApprovalStore.choose(); approve/reject of one outbound action by decide().
Both are called only from the CLI (venture.py, prompt_choice) or the web UI routes in
app/main.py. A choice is never an approval of an outbound action: those have their own ids.
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
from guardrails import redact_secrets

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

    def request(self, action_id: str, summary: str, choices: list[str] | None = None) -> None:
        """Ask the founder. With `choices`, it is a founder decision (roadmap, milestone review),
        answered by choose(); without, an approve/reject of one outbound action, answered by decide()."""
        with _lock:
            data = self._load()
            if action_id not in data:
                data[action_id] = {"summary": summary, "requested_at": utc_now(), "decision": None}
                if choices is not None:
                    data[action_id]["choices"] = list(choices)
                self._save(data)

    def decision(self, action_id: str) -> str | None:
        return self._load().get(action_id, {}).get("decision")

    def choice(self, action_id: str) -> str | None:
        return self._load().get(action_id, {}).get("choice")

    def choose(self, action_id: str, choice: str, note: str = "") -> None:
        """Record a founder decision. Only a choice that was offered is accepted.

        A choice never counts as an approval of an outbound action: those have their own ids.
        """
        with _lock:
            data = self._load()
            rec = data.get(action_id)
            if rec is None:
                raise KeyError(f"no founder decision requested with id {action_id!r}")
            if "choices" not in rec:
                raise ValueError(f"{action_id!r} is an approve/reject request; use decide()")
            if rec.get("decision") is not None:
                raise ValueError(f"{action_id!r} was already decided")
            if choice not in rec["choices"]:
                raise ValueError(f"{choice!r} is not offered here; choose one of: {', '.join(rec['choices'])}")
            note, _ = redact_secrets(note)  # a card number or password in the notes is never stored
            rec.update(decision="chosen", choice=choice, decided_at=utc_now(), decided_by="founder", note=note)
            self._save(data)
        if self.log is not None:
            self.log.record("founder", f"decide:{action_id}", approved_by="founder", outcome=choice)

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
            if "choices" in data[action_id]:
                raise ValueError(f"{action_id!r} asks for a choice; use choose()")
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


def prompt_choice(store: ApprovalStore, action_id: str, input_fn: Callable[[str], str] = input) -> str:
    """Ask the founder for one of the offered choices on the terminal. Returns the choice."""
    rec = store.record(action_id) or {}
    choices = rec.get("choices", [])
    print(f"\nFounder decision needed: {action_id}\n  {rec.get('summary', '')}")
    while True:
        answer = input_fn(f"Choose one of [{' / '.join(choices)}]: ").strip().lower()
        if answer in choices:
            note = input_fn("Notes (optional): ").strip()
            store.choose(action_id, answer, note=note)
            return answer
        print("  not one of the offered choices")

"""Orchestrator: a state machine over the six phases, with a gate at the end of every state.

Each state runs its steps (agents), then a gate returns exactly one of go / repeat /
pivot / stop, and TRANSITIONS gives the next state. States that need the founder
block in AWAITING_APPROVAL until the founder approves or rejects in the UI or CLI.

Every run writes to runs/<run_id>/ (git-ignored): state.json, approvals.json,
decision_log.json and outputs/. Only `promote()` copies a finished *live* run into the
committed outputs/ and decision_log.json; mock runs are refused.
"""

from __future__ import annotations

import importlib
import json
import re
import secrets
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import decision_log
import gates
import guardrails
import settings as settings_mod
from approval import ApprovalStore, make_action_id
from decision_log import DecisionLog
from gates import GateResult, Outcome
from llm import MOCK_MODEL_NAME, LLMClient, LLMError
from sources import Claim, SourceRegistry, claims_to_markdown

CLAIMS_PLACEHOLDER = "{{CLAIMS}}"


class State(str, Enum):
    SCREEN_BRIEF = "SCREEN_BRIEF"
    P1_DISCOVER = "P1_DISCOVER"
    P2_VALIDATE = "P2_VALIDATE"
    P3_BUILD = "P3_BUILD"
    P4_COMMERCIAL = "P4_COMMERCIAL"
    P5_LAUNCH = "P5_LAUNCH"
    P6_OPERATE = "P6_OPERATE"
    DONE = "DONE"
    STOPPED = "STOPPED"


TERMINAL = {State.DONE, State.STOPPED}


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"  # blocking sub-state: waits for the founder
    BLOCKED = "blocked"  # a step is STUBBED or the model is unreachable; resume when fixed
    STOPPED = "stopped"
    DONE = "done"


# Section 4 of the plan. An outcome missing for a state ("—") leads to STOPPED.
TRANSITIONS: dict[State, dict[Outcome, State]] = {
    State.SCREEN_BRIEF: {Outcome.GO: State.P1_DISCOVER, Outcome.STOP: State.STOPPED},
    State.P1_DISCOVER: {Outcome.GO: State.P2_VALIDATE, Outcome.REPEAT: State.P1_DISCOVER,
                        Outcome.PIVOT: State.SCREEN_BRIEF, Outcome.STOP: State.STOPPED},
    State.P2_VALIDATE: {Outcome.GO: State.P3_BUILD, Outcome.REPEAT: State.P2_VALIDATE,
                        Outcome.PIVOT: State.P1_DISCOVER, Outcome.STOP: State.STOPPED},
    State.P3_BUILD: {Outcome.GO: State.P4_COMMERCIAL, Outcome.REPEAT: State.P3_BUILD,
                     Outcome.PIVOT: State.P2_VALIDATE, Outcome.STOP: State.STOPPED},
    State.P4_COMMERCIAL: {Outcome.GO: State.P5_LAUNCH, Outcome.REPEAT: State.P4_COMMERCIAL,
                          Outcome.PIVOT: State.P2_VALIDATE, Outcome.STOP: State.STOPPED},
    State.P5_LAUNCH: {Outcome.GO: State.P6_OPERATE, Outcome.REPEAT: State.P5_LAUNCH,
                      Outcome.PIVOT: State.P4_COMMERCIAL, Outcome.STOP: State.STOPPED},
    State.P6_OPERATE: {Outcome.GO: State.DONE, Outcome.PIVOT: State.P1_DISCOVER, Outcome.STOP: State.STOPPED},
}


def next_state(state: State, outcome: Outcome, attempts: int, pivots: int) -> tuple[State, str]:
    """Pure transition function. Every (state, outcome) pair leads to a defined state."""
    if state in TERMINAL:
        raise ValueError(f"{state.value} is terminal")
    allowed = TRANSITIONS[state]
    if outcome not in allowed:
        return State.STOPPED, f"outcome '{outcome.value}' is not defined for {state.value}; stopping"
    if outcome is Outcome.REPEAT and attempts >= gates.MAX_ATTEMPTS_PER_STATE:
        return State.STOPPED, f"repeat limit reached for {state.value} ({attempts} attempts); stopping"
    if outcome is Outcome.PIVOT and pivots >= gates.MAX_PIVOTS_PER_RUN:
        return State.STOPPED, f"pivot limit reached ({pivots}); stopping"
    return allowed[outcome], ""


# ---------------------------------------------------------------- steps

@dataclass(frozen=True)
class Step:
    phase: int
    key: str
    agent: str
    module: str
    func: str
    output: str
    label: str
    mocked: str = ""  # what part is MOCKED, if any

    def fn(self) -> Callable[..., "StepResult"]:
        return getattr(importlib.import_module(self.module), self.func)


S = Step
STEPS: dict[State, list[Step]] = {
    State.SCREEN_BRIEF: [],
    State.P1_DISCOVER: [
        S(1, "phase1_step1", "researcher", "agents.researcher", "find_opportunities", "phase1_step1_opportunities.md", "Find opportunities"),
        S(1, "phase1_step2", "scorer", "agents.scorer", "score", "phase1_step2_scoring.json", "Score opportunities"),
        S(1, "phase1_step3", "researcher", "agents.researcher", "market_research", "phase1_step3_market.md", "Market and competitors"),
        S(1, "phase1_step4", "interviewer", "agents.interviewer", "interview_guide", "phase1_step4_interview_guide.md", "Mom Test interview guide"),
    ],
    State.P2_VALIDATE: [
        S(2, "phase2_step1", "strategist", "agents.strategist", "lean_canvas", "phase2_step1_lean_canvas.md", "Lean Canvas"),
        S(2, "phase2_step2", "strategist", "agents.strategist", "landing_page", "phase2_step2_landing_page.html", "Landing page"),
        S(2, "phase2_step3", "strategist", "agents.strategist", "demand_test", "phase2_step3_demand_test.md", "Demand test", mocked="sign-up results"),
    ],
    State.P3_BUILD: [
        S(3, "phase3_step1", "builder", "agents.builder", "mvp_spec", "phase3_step1_mvp_spec.md", "MVP spec"),
        S(3, "phase3_step2", "builder", "agents.builder", "build_product", "phase3_step2_product.md", "Build the product"),
        S(3, "phase3_step3", "builder", "agents.builder", "user_docs", "phase3_step3_user_docs.md", "User docs"),
    ],
    State.P4_COMMERCIAL: [
        S(4, "phase4_step1", "commercial", "agents.commercial", "pricing", "phase4_step1_pricing.md", "Pricing"),
        S(4, "phase4_step2", "commercial", "agents.commercial", "policies", "phase4_step2_policies.md", "Draft policies (not legal advice)"),
    ],
    State.P5_LAUNCH: [
        S(5, "phase5_step1", "launcher", "agents.launcher", "store_page", "phase5_step1_store_page.html", "Store page", mocked="publishing"),
        S(5, "phase5_step2", "launcher", "agents.launcher", "launch_posts", "phase5_step2_launch_posts.md", "Launch post drafts", mocked="posting"),
    ],
    State.P6_OPERATE: [
        S(6, "phase6_step1", "operator", "agents.operator", "support_faq", "phase6_step1_support_faq.md", "Support FAQ"),
        S(6, "phase6_step2", "operator", "agents.operator", "pivot_memo", "phase6_step2_pivot_memo.md", "Pivot or persevere memo"),
    ],
}

# Steps that run only after the founder approved the state's gate (outbound actions).
AFTER_APPROVAL: dict[State, list[Step]] = {
    State.P5_LAUNCH: [
        S(5, "phase5_step3", "delivery", "delivery", "payment_test", "payment_test_log.md",
          "Payment, delivery and refund test", mocked="payments, delivery and refunds"),
        S(5, "phase5_step4", "launcher", "agents.launcher", "publish", "phase5_step4_publish_log.md",
          "Publish store page and posts", mocked="publishing and posting"),
    ],
}

GATE_FILES = {
    State.SCREEN_BRIEF: "phase1_gate_brief_screen.md",
    State.P1_DISCOVER: "phase1_gate_decision.md",
    State.P2_VALIDATE: "phase2_gate_decision.md",
    State.P3_BUILD: "phase3_gate_tests.md",
    State.P4_COMMERCIAL: "phase4_gate_decision.md",
    State.P5_LAUNCH: "phase5_gate_decision.md",
    State.P6_OPERATE: "phase6_gate_decision.md",
}


def all_steps() -> list[Step]:
    out: list[Step] = []
    for st in STEPS:
        out += STEPS[st] + AFTER_APPROVAL.get(st, [])
    return out


def step_status(step: Step) -> str:
    """STUBBED if the agent function is a stub, MOCKED if it fakes an outside service, else Implemented."""
    try:
        fn = step.fn()
    except (ImportError, AttributeError):
        return "STUBBED"
    if getattr(fn, "__status__", "") == "STUBBED":
        return "STUBBED"
    if step.mocked:
        return "MOCKED"
    return "Implemented"


# ---------------------------------------------------------------- run context

@dataclass
class StepResult:
    """What an agent function returns. Use CLAIMS_PLACEHOLDER in content where the claims list goes."""
    content: str
    claims: list[Claim] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


class StepStubbed(Exception):
    pass


class OutputBlocked(Exception):
    pass


class PromoteError(RuntimeError):
    pass


@dataclass
class RunRecord:
    run_id: str
    brief: dict[str, Any]
    mode: str
    model: str
    state: str = State.SCREEN_BRIEF.value
    status: str = RunStatus.RUNNING.value
    attempts: dict[str, int] = field(default_factory=dict)
    pivots: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    blocked_reason: str = ""
    stop_reasons: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    registered_urls: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(f"bad run id {run_id!r}")
    return settings_mod.RUNS_DIR / run_id


def _state_path(run_id: str) -> Path:
    return run_dir(run_id) / "state.json"


def save(rec: RunRecord) -> None:
    rec.updated_at = decision_log.utc_now()
    p = _state_path(rec.run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(rec), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def load(run_id: str) -> RunRecord:
    return RunRecord(**json.loads(_state_path(run_id).read_text(encoding="utf-8")))


def list_runs() -> list[RunRecord]:
    if not settings_mod.RUNS_DIR.exists():
        return []
    recs = []
    for p in settings_mod.RUNS_DIR.glob("*/state.json"):
        try:
            recs.append(RunRecord(**json.loads(p.read_text(encoding="utf-8"))))
        except (ValueError, TypeError):
            continue
    return sorted(recs, key=lambda r: r.created_at, reverse=True)


class RunContext:
    def __init__(self, rec: RunRecord, s: settings_mod.Settings, llm: LLMClient | None = None,
                 registry: SourceRegistry | None = None):
        self.rec = rec
        self.settings = s
        self.dir = run_dir(rec.run_id)
        self.outputs_dir = self.dir / "outputs"
        self.log = DecisionLog(self.dir / "decision_log.json")
        self.approvals = ApprovalStore(self.dir / "approvals.json", self.log)
        self.llm = llm or LLMClient(s)
        self.registry = registry or SourceRegistry(max_sources=s.max_sources)
        for url in rec.registered_urls:
            self.registry.restore(url)
        self.attempt = 1
        self.feedback: str | None = None
        self.approved_action_id: str | None = None

    @property
    def run_id(self) -> str:
        return self.rec.run_id

    @property
    def brief(self) -> dict[str, Any]:
        return self.rec.brief

    @property
    def instructions(self) -> list[str]:
        return list(self.rec.instructions)

    def artifact(self, key: str) -> dict[str, Any]:
        return self.rec.artifacts.get(key, {})

    def read_output(self, key_or_name: str) -> str:
        name = self.rec.artifacts.get(key_or_name, {}).get("output", key_or_name)
        p = self.outputs_dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write_output(self, name: str, content: str) -> Path:
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"bad output name {name!r}")
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        p = self.outputs_dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def prompt(self, agent: str) -> str:
        p = settings_mod.PROMPTS_DIR / f"{agent}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


_URL = re.compile(r"https?://[^\s)\]\"'<>]+")


def scrub_unregistered_urls(text: str, registry: SourceRegistry) -> str:
    """Replace any URL this run did not fetch, so a model cannot cite from memory."""
    return _URL.sub(lambda m: m.group(0) if registry.is_registered(m.group(0).rstrip(".,;:")) else "[unverified link removed: ASSUMPTION]", text)


def run_step(ctx: RunContext, step: Step) -> StepResult:
    fn = step.fn()
    if getattr(fn, "__status__", "") == "STUBBED":
        raise StepStubbed(f"{step.key} ({step.module}.{step.func}) is STUBBED")
    ctx.feedback = None
    for attempt in (1, 2):
        calls_before, cost_before = ctx.llm.usage.calls, ctx.llm.usage.cost
        try:
            result = fn(ctx)
        except NotImplementedError as exc:
            raise StepStubbed(f"{step.key} ({step.module}.{step.func}) is STUBBED") from exc
        claims = ctx.registry.stamp_claims(result.claims)
        content = result.content.replace(CLAIMS_PLACEHOLDER, claims_to_markdown(claims))
        content = scrub_unregistered_urls(content, ctx.registry)
        check = guardrails.check_output(step.output, content, ctx.log)
        if check.ok:
            break
        if attempt == 2:
            raise OutputBlocked(f"{step.output} blocked twice by the output check: {'; '.join(check.hits)}")
        ctx.feedback = "Your previous draft was blocked. Remove: " + "; ".join(check.hits)
    used_model = ctx.llm.usage.calls > calls_before
    sources = [u for u in dict.fromkeys(result.sources + [c.url for c in claims if c.url]) if ctx.registry.is_registered(u)]
    ctx.write_output(step.output, content)
    action = f"mocked_{step.func}" if step.mocked and not used_model else step.func
    ctx.log.record(step.agent, action, sources=sources,
                   model=ctx.llm.model_label if used_model else None,
                   cost=ctx.llm.usage.cost - cost_before)
    ctx.rec.artifacts[step.key] = {"output": step.output, "claims": [c.to_dict() for c in claims],
                                   "sources": sources, "data": result.data}
    ctx.feedback = None
    return StepResult(content, claims, sources, result.data)


# ---------------------------------------------------------------- gates per state

def _claims(ctx: RunContext, *keys: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in keys:
        out += ctx.artifact(k).get("claims", [])
    return out


def _gate_screen(ctx: RunContext) -> GateResult:
    screen = guardrails.screen_brief(ctx.brief, ctx.log)
    return gates.brief_screen_gate(screen.allowed, screen.reasons)


def _gate_p1(ctx: RunContext) -> GateResult:
    claims = _claims(ctx, "phase1_step1", "phase1_step3")
    scoring = ctx.artifact("phase1_step2").get("data", {})
    ev = gates.Phase1Evidence(
        problem_source_urls=[c["url"] for c in claims if c["label"] == "SOURCED" and c["kind"] == "problem"],
        paying_source_urls=[c["url"] for c in claims if c["label"] == "SOURCED" and c["kind"] == "paying"],
        budget_usd=float(ctx.brief["budget_usd"]),
        hours_per_week=float(ctx.brief["hours_per_week"]),
        mvp_cost_usd=scoring.get("mvp_cost_usd"),
        mvp_hours_per_week=scoring.get("mvp_hours_per_week"),
        total_claims=len(claims),
        assumption_claims=sum(1 for c in claims if c["label"] != "SOURCED"),
    )
    return gates.phase1_gate(ev, ctx.attempt)


def _gate_p2(ctx: RunContext) -> GateResult:
    evidence = gates.claims_gate("phase2_evidence", [c["label"] for c in _claims(ctx, "phase2_step1")], ctx.attempt)
    if evidence.outcome is not Outcome.GO:
        return evidence
    page = ctx.read_output("phase2_step2")
    return gates.approval_gate("phase2_landing_page", "Approve the landing page (phase2_step2_landing_page.html)",
                               page, evidence.checks)


def _gate_p3(ctx: RunContext) -> GateResult:
    tests = ctx.artifact("phase3_step2").get("data", {}).get("tests", {})
    return gates.tests_gate({str(k): bool(v) for k, v in tests.items()}, ctx.attempt)


def _gate_p4(ctx: RunContext) -> GateResult:
    payload = ctx.read_output("phase4_step1") + ctx.read_output("phase4_step2")
    return gates.approval_gate("phase4_price_and_policies", "Approve the price and the draft policies", payload)


def _gate_p5(ctx: RunContext) -> GateResult:
    payload = ctx.read_output("phase5_step1") + ctx.read_output("phase5_step2")
    return gates.approval_gate("phase5_publish",
                               "Approve publishing the store page and launch posts, and the payment test (all MOCKED)",
                               payload)


def _gate_p6(ctx: RunContext) -> GateResult:
    rec = str(ctx.artifact("phase6_step2").get("data", {}).get("recommendation", "stop")).lower()
    outcome = {"persevere": Outcome.GO, "go": Outcome.GO, "pivot": Outcome.PIVOT}.get(rec, Outcome.STOP)
    return GateResult("phase6_metrics", outcome, [f"operator recommendation: {rec}"])


GATES: dict[State, Callable[[RunContext], GateResult]] = {
    State.SCREEN_BRIEF: _gate_screen, State.P1_DISCOVER: _gate_p1, State.P2_VALIDATE: _gate_p2,
    State.P3_BUILD: _gate_p3, State.P4_COMMERCIAL: _gate_p4, State.P5_LAUNCH: _gate_p5, State.P6_OPERATE: _gate_p6,
}

TITLES = {
    State.SCREEN_BRIEF: "Phase 1 gate: founder brief screen",
    State.P1_DISCOVER: "Phase 1 gate: evidence decision",
    State.P2_VALIDATE: "Phase 2 gate: validation decision",
    State.P3_BUILD: "Phase 3 gate: product tests",
    State.P4_COMMERCIAL: "Phase 4 gate: price and policies",
    State.P5_LAUNCH: "Phase 5 gate: publish and payment test",
    State.P6_OPERATE: "Phase 6 gate: pivot or persevere",
}


def _write_gate(ctx: RunContext, state: State, gate: GateResult, extra: str = "") -> None:
    md = gate.to_markdown(TITLES[state])
    if extra:
        md += "\n" + extra + "\n"
    ctx.write_output(GATE_FILES[state], md)


# ---------------------------------------------------------------- run loop

_run_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(run_id: str) -> threading.Lock:
    with _locks_guard:
        return _run_locks.setdefault(run_id, threading.Lock())


def new_run(brief: dict[str, Any], s: settings_mod.Settings | None = None) -> RunRecord:
    s = s or settings_mod.load()
    guardrails.validate_brief(brief)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", brief["idea"].lower()).strip("-")[:24] or "run"
    rec = RunRecord(run_id=f"{stamp}-{slug}-{secrets.token_hex(2)}", brief=brief, mode=s.llm_mode,
                    model=MOCK_MODEL_NAME if s.mock else s.llm_model, created_at=decision_log.utc_now())
    save(rec)
    return rec


def load_brief(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise guardrails.BriefError(f"brief file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise guardrails.BriefError(f"brief is not valid JSON: {exc}") from exc
    return guardrails.validate_brief(data)


def instruct(run_id: str, text: str, s: settings_mod.Settings | None = None) -> guardrails.RequestScreen:
    """A founder instruction during a run. Screened before any agent sees it."""
    with _lock_for(run_id):
        rec = load(run_id)
        log = DecisionLog(run_dir(run_id) / "decision_log.json")
        screen = guardrails.screen_request(text, log)
        if not screen.refused:
            rec.instructions.append(text)
            save(rec)
        return screen


def advance(run_id: str, s: settings_mod.Settings | None = None, llm: LLMClient | None = None,
            registry: SourceRegistry | None = None, max_transitions: int = 40) -> RunRecord:
    """Run until the run finishes, stops, blocks, or needs the founder. Safe to call again to resume."""
    s = s or settings_mod.load()
    with _lock_for(run_id):
        rec = load(run_id)
        if rec.mode != s.llm_mode:
            raise RuntimeError(f"run {run_id} was started in {rec.mode} mode; current LLM_MODE is {s.llm_mode}")
        if rec.status in (RunStatus.DONE.value, RunStatus.STOPPED.value):
            return rec
        rec.status = RunStatus.RUNNING.value
        rec.blocked_reason = ""
        ctx = RunContext(rec, s, llm, registry)
        try:
            for _ in range(max_transitions):
                if not _tick(ctx):
                    break
            else:
                _finish(ctx, Outcome.STOP, State(rec.state), ["transition limit reached"])
        finally:
            rec.registered_urls = ctx.registry.urls()
            usage = ctx.llm.usage.as_dict()
            prev = rec.usage or {}
            rec.usage = {k: (prev.get(k, 0) + v) for k, v in usage.items()}
            ctx.llm.usage = type(ctx.llm.usage)()
            save(rec)
        return rec


def _finish(ctx: RunContext, outcome: Outcome, state: State, reasons: list[str]) -> None:
    rec = ctx.rec
    rec.state = State.STOPPED.value
    rec.status = RunStatus.STOPPED.value
    rec.stop_reasons = reasons
    rec.history.append({"state": state.value, "outcome": outcome.value, "reasons": reasons,
                        "next": State.STOPPED.value, "at": decision_log.utc_now()})


def _tick(ctx: RunContext) -> bool:
    """One state. Returns False when the loop must pause or end."""
    rec = ctx.rec
    state = State(rec.state)
    if state in TERMINAL:
        rec.status = RunStatus.DONE.value if state is State.DONE else RunStatus.STOPPED.value
        return False

    if rec.pending:
        action_id = rec.pending["action_id"]
        decision = ctx.approvals.decision(action_id)
        if decision is None:
            rec.status = RunStatus.AWAITING_APPROVAL.value
            return False
        ctx.attempt = rec.attempts.get(state.value, 1)
        gate_name = rec.pending["gate"]
        if decision == "approved":
            ctx.approved_action_id = action_id
            outcome, reasons = Outcome.GO, ["founder approved"]
            try:
                for step in AFTER_APPROVAL.get(state, []):
                    run_step(ctx, step)
            except (StepStubbed, LLMError) as exc:  # approval stays on record; resume retries these steps
                rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, str(exc)
                return False
            except OutputBlocked as exc:
                outcome, reasons = Outcome.STOP, ["founder approved", str(exc)]
            ctx.log.record("gate", f"{gate_name}_gate", approved_by="founder", outcome=outcome.value)
        else:
            on_reject = (ctx.approvals.record(action_id) or {}).get("on_reject") or "stop"
            outcome = Outcome.REPEAT if on_reject == "repeat" else Outcome.STOP
            reasons = [f"founder rejected; chose {on_reject}"]
            ctx.log.record("gate", f"{gate_name}_gate", outcome=outcome.value)
        rec.pending = None
        gate = GateResult(gate_name, outcome, reasons)
        _write_gate(ctx, state, gate)
    else:
        rec.attempts[state.value] = rec.attempts.get(state.value, 0) + 1
        ctx.attempt = rec.attempts[state.value]
        try:
            for step in STEPS[state]:
                run_step(ctx, step)
            gate = GATES[state](ctx)
        except StepStubbed as exc:
            rec.attempts[state.value] -= 1  # a stub is not an attempt; resume retries it
            rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, str(exc)
            return False
        except LLMError as exc:
            rec.attempts[state.value] -= 1
            rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, f"model unavailable: {exc}"
            return False
        except OutputBlocked as exc:
            gate = GateResult(f"{state.value.lower()}_output_check", Outcome.STOP, [str(exc)])

        if gate.needs_approval and gate.outcome is Outcome.GO:
            action_id = make_action_id(gate.gate, rec.run_id, gate.approval_payload)
            ctx.approvals.request(action_id, gate.needs_approval)
            rec.pending = {"action_id": action_id, "gate": gate.gate, "summary": gate.needs_approval}
            rec.status = RunStatus.AWAITING_APPROVAL.value
            _write_gate(ctx, state, GateResult(gate.gate, Outcome.GO, gate.reasons, gate.checks),
                        extra=f"**Founder decision pending** for action `{action_id}`.")
            ctx.log.record("gate", f"{gate.gate}_gate", outcome="awaiting_approval")
            return False
        outcome, reasons = gate.outcome, gate.reasons
        ctx.log.record("gate", f"{gate.gate}_gate", outcome=outcome.value)
        _write_gate(ctx, state, gate)
        if outcome is Outcome.PIVOT and gate.revised_brief:
            rec.brief = guardrails.validate_brief(gate.revised_brief)

    nxt, note = next_state(state, outcome, rec.attempts.get(state.value, 1), rec.pivots)
    if note:
        reasons = reasons + [note]
    if outcome is Outcome.PIVOT and nxt is not State.STOPPED:
        rec.pivots += 1
    if nxt is not state:
        rec.attempts.pop(nxt.value, None)  # arriving fresh (not a repeat) resets the attempt count
    rec.history.append({"state": state.value, "outcome": outcome.value, "reasons": reasons,
                        "next": nxt.value, "at": decision_log.utc_now()})
    rec.state = nxt.value
    if nxt is State.STOPPED:
        rec.status, rec.stop_reasons = RunStatus.STOPPED.value, reasons
        return False
    if nxt is State.DONE:
        rec.status = RunStatus.DONE.value
        return False
    return True


# ---------------------------------------------------------------- promote a real run

def promote(run_id: str, subdir: str | None = None, replace: bool = False) -> Path:
    """Copy a finished live run into the committed outputs/ and decision_log.json.

    Refuses mock runs and runs that are still in progress. This is the only code
    path that writes to the repository's outputs/ or root decision_log.json.
    """
    rec = load(run_id)
    src_log = DecisionLog(run_dir(run_id) / "decision_log.json")
    entries = src_log.entries()
    if rec.mode != "live" or any(e.get("model") == MOCK_MODEL_NAME for e in entries):
        raise PromoteError("mock-mode runs are never promoted to outputs/ or decision_log.json")
    if rec.status == RunStatus.RUNNING.value:
        raise PromoteError("run is still in progress")
    if subdir is not None and not re.fullmatch(r"[a-z0-9_]+", subdir):
        raise PromoteError("subdir must be lowercase letters, digits and underscores")
    dest = settings_mod.OUTPUTS_DIR / subdir if subdir else settings_mod.OUTPUTS_DIR
    if replace:
        for p in settings_mod.OUTPUTS_DIR.iterdir() if settings_mod.OUTPUTS_DIR.exists() else []:
            if p.name == ".gitkeep":
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        decision_log.reset(settings_mod.ROOT_LOG)
    dest.mkdir(parents=True, exist_ok=True)
    for f in sorted((run_dir(run_id) / "outputs").glob("*")):
        if f.is_file():
            shutil.copy2(f, dest / f.name)
    for e in entries:
        decision_log.append(e, settings_mod.ROOT_LOG)
    return dest

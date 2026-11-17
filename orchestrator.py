"""Orchestrator: a two-level state machine. Lookup tables choose every next state; the model never does.

Outer machine (TRANSITIONS): one state per phase, plus three states that run once per run
before Phase 1: SCREEN_BRIEF (rules), FEASIBILITY (rubric) and ROADMAP (founder approval).

Inner loop (INNER), inside every phase:
    PLAN -> PLAN_REVIEW -> MAKE -> VERIFY -> SIGN_OFF -> MILESTONE_REVIEW
Planners write the plan and its pass criteria, the checker reviews the plan, the makers
produce, the checker verifies (it never edits), gates.py signs off in code, and the
founder decides at the milestone review. Each back-edge (revise, defects, repeat) may fire
at most gates.MAX_BACK_EDGES times per phase.

The run blocks (status awaiting_approval) whenever it needs the founder: the narrowed idea
in FEASIBILITY, the roadmap, every milestone review, and every outbound action. Outbound
actions (publish, send, spend) each have their own action id, so approving a milestone
never approves a publication by implication.

Every run writes to runs/<run_id>/ (git-ignored): state.json, approvals.json,
decision_log.json and outputs/. Only `promote()` copies a finished *live* run into the
committed outputs/ and decision_log.json; mock runs are refused.
"""

from __future__ import annotations

import importlib
import json
from functools import lru_cache
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel

import decision_log
import gates
import guardrails
import settings as settings_mod
from approval import ApprovalStore, make_action_id
from decision_log import DecisionLog
from gates import GateResult, Outcome
from llm import MOCK_MODEL_NAME, CallBudgetExceeded, LLMClient, LLMError, ModelOutputInvalid, ShortText
import sources as sources_mod
from sources import Claim, SourceRegistry, claims_to_markdown

CLAIMS_PLACEHOLDER = "{{CLAIMS}}"


# ================================================================ outer machine (section 4.1)

class State(str, Enum):
    SCREEN_BRIEF = "SCREEN_BRIEF"
    FEASIBILITY = "FEASIBILITY"
    ROADMAP = "ROADMAP"
    P1_DISCOVER = "P1_DISCOVER"
    P2_VALIDATE = "P2_VALIDATE"
    P3_BUILD = "P3_BUILD"
    P4_COMMERCIAL = "P4_COMMERCIAL"
    P5_LAUNCH = "P5_LAUNCH"
    P6_OPERATE = "P6_OPERATE"
    DONE = "DONE"
    STOPPED = "STOPPED"


TERMINAL = {State.DONE, State.STOPPED}
PHASE_STATES = [State.P1_DISCOVER, State.P2_VALIDATE, State.P3_BUILD, State.P4_COMMERCIAL,
                State.P5_LAUNCH, State.P6_OPERATE]
PHASE_OF = {s: i for i, s in enumerate(PHASE_STATES, start=1)}


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"  # blocking: waits for the founder in the UI or CLI
    BLOCKED = "blocked"  # a step is STUBBED or the model is unreachable; resume when fixed
    STOPPED = "stopped"
    DONE = "done"


# An outcome missing for a state ("—" in the plan) leads to STOPPED.
# For phases, "repeat" is carried out inside the inner loop (back to MAKE), within the loop budget.
# Requirement R-UNTRUSTED-TEXT: this table lists every allowed move between states. Gate outcomes
# computed in code and the founder's choices pick the move; text in a fetched page never does.
# Verified by: tests/test_traceability.py::test_injected_page_text_does_not_steer_the_run
TRANSITIONS: dict[State, dict[Outcome, State]] = {
    State.SCREEN_BRIEF: {Outcome.GO: State.FEASIBILITY, Outcome.STOP: State.STOPPED},
    # The founder decides here: continue (go), revise (repeat: back through the scope screen) or stop.
    State.FEASIBILITY: {Outcome.GO: State.ROADMAP, Outcome.REPEAT: State.SCREEN_BRIEF, Outcome.STOP: State.STOPPED},
    State.ROADMAP: {Outcome.GO: State.P1_DISCOVER, Outcome.REPEAT: State.ROADMAP, Outcome.STOP: State.STOPPED},
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

OUTER_REPEAT_LIMITS = {State.FEASIBILITY: gates.FEASIBILITY_MAX_REPEATS, State.ROADMAP: gates.ROADMAP_MAX_REVISIONS}


# Requirement R-GATES: a gate outcome (go, repeat, pivot or stop) selects the next state from
# TRANSITIONS; an outcome with no entry for the current state leads to STOPPED.
# Verified by: tests/test_gates.py::test_undefined_outcome_stops
def next_state(state: State, outcome: Outcome, repeats_used: int, pivots: int) -> tuple[State, str]:
    """Pure outer transition. Every (state, outcome) pair leads to a defined state."""
    if state in TERMINAL:
        raise ValueError(f"{state.value} is terminal")
    allowed = TRANSITIONS[state]
    if outcome not in allowed:
        return State.STOPPED, f"outcome '{outcome.value}' is not defined for {state.value}; stopping"
    limit = OUTER_REPEAT_LIMITS.get(state, gates.MAX_BACK_EDGES)
    if outcome is Outcome.REPEAT and repeats_used >= limit:
        return State.STOPPED, f"repeat limit reached for {state.value} ({repeats_used} used); stopping"
    if outcome is Outcome.PIVOT and pivots >= gates.MAX_PIVOTS_PER_RUN:
        return State.STOPPED, f"pivot limit reached ({pivots}); stopping"
    return allowed[outcome], ""


# ================================================================ inner loop (section 4.2)

class Stage(str, Enum):
    PLAN = "PLAN"
    PLAN_REVIEW = "PLAN_REVIEW"
    MAKE = "MAKE"
    VERIFY = "VERIFY"
    SIGN_OFF = "SIGN_OFF"
    MILESTONE_REVIEW = "MILESTONE_REVIEW"


class Signal(str, Enum):
    DONE = "done"
    APPROVED = "approved"
    REVISE = "revise"
    VERIFIED = "verified"
    DEFECTS = "defects"
    REPEAT = "repeat"
    GO = "go"
    PIVOT = "pivot"
    STOP = "stop"
    CONTINUE = "continue"
    REQUEST_CHANGES = "request_changes"


class Exit(str, Enum):
    NEXT_PHASE = "NEXT_PHASE"  # the outer GO transition
    PIVOT_PHASE = "PIVOT_PHASE"  # the outer PIVOT transition, entering that phase at PLAN
    STOPPED = "STOPPED"


INNER: dict[Stage, dict[Signal, Stage | Exit]] = {
    Stage.PLAN: {Signal.DONE: Stage.PLAN_REVIEW},
    Stage.PLAN_REVIEW: {Signal.APPROVED: Stage.MAKE, Signal.REVISE: Stage.PLAN, Signal.STOP: Exit.STOPPED},
    Stage.MAKE: {Signal.DONE: Stage.VERIFY},
    Stage.VERIFY: {Signal.VERIFIED: Stage.SIGN_OFF, Signal.DEFECTS: Stage.MAKE},
    # SIGN_OFF never routes to VERIFY: re-verifying an unchanged artefact returns the same result.
    Stage.SIGN_OFF: {Signal.REPEAT: Stage.MAKE, Signal.GO: Stage.MILESTONE_REVIEW,
                     Signal.PIVOT: Stage.MILESTONE_REVIEW, Signal.STOP: Stage.MILESTONE_REVIEW},
    Stage.MILESTONE_REVIEW: {Signal.CONTINUE: Exit.NEXT_PHASE, Signal.REQUEST_CHANGES: Stage.MAKE,
                             Signal.PIVOT: Exit.PIVOT_PHASE, Signal.STOP: Exit.STOPPED},
}

# Back-edges and the budget counter each one spends.
BACK_EDGES: dict[tuple[Stage, Signal], str] = {
    (Stage.PLAN_REVIEW, Signal.REVISE): "revise",
    (Stage.VERIFY, Signal.DEFECTS): "defects",
    (Stage.SIGN_OFF, Signal.REPEAT): "repeat",
    (Stage.MILESTONE_REVIEW, Signal.REQUEST_CHANGES): "repeat",
}
BUDGET_KEYS = ("revise", "defects", "repeat")


def inner_next(stage: Stage, signal: Signal, budget: dict[str, int]) -> tuple[Stage | Exit, Signal, str | None, str]:
    """Pure inner transition. Returns (target, effective signal, budget counter spent or None, note).

    A back-edge whose budget is used up does not loop again: `defects` goes on to SIGN_OFF
    with its open defects listed; `revise` and `repeat` become `stop`.
    """
    row = INNER[stage]
    if signal not in row:
        return Exit.STOPPED, Signal.STOP, None, f"'{signal.value}' is not defined at {stage.value}; stopping"
    edge = BACK_EDGES.get((stage, signal))
    if edge is None:
        return row[signal], signal, None, ""
    if budget.get(edge, 0) < gates.MAX_BACK_EDGES:
        return row[signal], signal, edge, ""
    note = f"{edge} budget used up ({budget.get(edge, 0)} of {gates.MAX_BACK_EDGES})"
    if edge == "defects":
        return Stage.SIGN_OFF, signal, None, note + "; signing off with the open defects listed"
    if stage is Stage.SIGN_OFF:
        return Stage.MILESTONE_REVIEW, Signal.STOP, None, note + "; recommend STOP"
    return Exit.STOPPED, Signal.STOP, None, note + "; stopping"


# ================================================================ steps

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
    needs: tuple[str, ...] = ()  # outbound action keys; the step runs only if one was approved

    def fn(self) -> Callable[..., "StepResult"]:
        return getattr(importlib.import_module(self.module), self.func)


S = Step

# Makers, per phase (section 5).
STEPS: dict[State, list[Step]] = {
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
        S(6, "phase6_step2", "operator", "agents.operator", "pivot_memo", "phase6_step2_pivot_memo.md", "Pivot or persevere memo",
          mocked="metrics"),
    ],
}


@dataclass(frozen=True)
class Outbound:
    """One outbound action the founder approves or rejects on its own, at the milestone review."""
    key: str
    summary: str
    payload_keys: tuple[str, ...]  # the artefacts whose exact content is approved


OUTBOUND: dict[State, list[Outbound]] = {
    State.P2_VALIDATE: [
        Outbound("publish_landing_page", "Publish the landing page (MOCKED: nothing leaves this machine)", ("phase2_step2",)),
    ],
    State.P5_LAUNCH: [
        Outbound("publish_store_page", "Publish the store page (MOCKED: nothing leaves this machine)", ("phase5_step1",)),
        Outbound("send_launch_posts", "Post the launch post drafts (MOCKED: nothing is posted)", ("phase5_step2",)),
        Outbound("payment_test", "Run the payment, delivery and refund test (MOCKED: no money moves)", ("phase4_step1",)),
    ],
}

# Steps that run after the founder chose "continue", each only if one of its outbound actions was approved.
AFTER_APPROVAL: dict[State, list[Step]] = {
    State.P2_VALIDATE: [
        S(2, "phase2_step4", "launcher", "agents.launcher", "publish_landing", "phase2_step4_publish_log.md",
          "Publish landing page", mocked="publishing", needs=("publish_landing_page",)),
    ],
    State.P5_LAUNCH: [
        S(5, "phase5_step3", "delivery", "delivery", "payment_test", "payment_test_log.md",
          "Payment, delivery and refund test", mocked="payments, delivery and refunds", needs=("payment_test",)),
        S(5, "phase5_step4", "launcher", "agents.launcher", "publish", "phase5_step4_publish_log.md",
          "Publish store page and posts", mocked="publishing and posting", needs=("publish_store_page", "send_launch_posts")),
    ],
}

# Role agents: their findings go into gate files written by code.
ROLES: dict[str, Step] = {
    "assess": S(1, "feasibility", "assessor", "agents.assessor", "assess", "phase1_gate_feasibility.md", "Feasibility check"),
    "roadmap": S(1, "roadmap", "planner", "agents.planner", "roadmap", "phase1_gate_roadmap.md", "Milestone roadmap"),
    "reconcile_roadmap": S(1, "roadmap", "checker", "agents.checker", "reconcile_roadmap", "phase1_gate_roadmap.md", "Reconcile roadmap proposals"),
    "plan": S(0, "plan", "planner", "agents.planner", "plan", "phase{n}_gate_plan_review.md", "Plan and pass criteria"),
    "verify": S(0, "verify", "checker", "agents.checker", "verify", "phase{n}_gate_decision.md", "Verification"),
}

SCREEN_FILE = "phase1_gate_brief_screen.md"
FEASIBILITY_FILE = "phase1_gate_feasibility.md"
ROADMAP_FILE = "phase1_gate_roadmap.md"
TESTS_FILE = "phase3_gate_tests.md"


def plan_review_file(n: int) -> str:
    return f"phase{n}_gate_plan_review.md"


def decision_file(n: int) -> str:
    return f"phase{n}_gate_decision.md"


def _role(name: str, n: int) -> Step:
    r = ROLES[name]
    return Step(n or r.phase, f"phase{n}_{r.key}" if n else r.key, r.agent, r.module, r.func,
                r.output.format(n=n), r.label)


def all_steps() -> list[Step]:
    out: list[Step] = []
    for st in PHASE_STATES:
        out += STEPS[st] + AFTER_APPROVAL.get(st, [])
    return out


def _func_status(module: str, func: str) -> str:
    try:
        fn = getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError):
        return "STUBBED"
    return "STUBBED" if getattr(fn, "__status__", "") == "STUBBED" else "Implemented"


def step_status(step: Step) -> str:
    """STUBBED if the agent function is a stub, MOCKED if it fakes an outside service, else Implemented."""
    status = _func_status(step.module, step.func)
    if status == "STUBBED":
        return "STUBBED"
    return "MOCKED" if step.mocked else "Implemented"


_RANK = {"STUBBED": 2, "MOCKED": 1, "Implemented": 0}


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK[s])


def catalog() -> list[dict[str, Any]]:
    """Every output file the pipeline writes, who writes it, and its build status (for the UI and README check)."""
    def row(phase, label, agent, output, status, mocked=""):
        return {"phase": phase, "label": label, "agent": agent, "output": output, "status": status, "mocked": mocked}

    plan_st = _func_status("agents.planner", "plan")
    verify_st = _func_status("agents.checker", "verify")
    rows = [
        row(1, "Scope screen (gate)", "guardrails", SCREEN_FILE, "Implemented"),
        row(1, "Feasibility check (gate)", "assessor", FEASIBILITY_FILE, _func_status("agents.assessor", "assess")),
        # Reconciling two roadmaps (checker, N_ARCHITECTS=2) is optional and not on the default path.
        row(1, "Milestone roadmap (founder approval)", "planner", ROADMAP_FILE, _func_status("agents.planner", "roadmap")),
    ]
    for st in PHASE_STATES:
        n = PHASE_OF[st]
        makers = STEPS[st]
        rows.append(row(n, "Plan and plan review (review in code)", "planner, gate", plan_review_file(n), plan_st))
        rows += [row(n, s.label, s.agent, s.output, step_status(s), s.mocked) for s in makers]
        maker_st = _worst(*[_func_status(s.module, s.func) for s in makers])
        if st is State.P3_BUILD:
            rows.append(row(n, "Product tests (gate)", "gate", TESTS_FILE, maker_st))
        rows.append(row(n, "Verification, sign-off and founder decision", "checker, gate, founder",
                        decision_file(n), _worst(verify_st, maker_st)))
        rows += [row(n, s.label, s.agent, s.output, step_status(s), s.mocked) for s in AFTER_APPROVAL.get(st, [])]
    return rows


# ================================================================ run record and context

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
    stage: str | None = None  # inner-loop stage while in a phase
    status: str = RunStatus.RUNNING.value
    pivots: int = 0
    budget: dict[str, dict[str, int]] = field(default_factory=dict)  # per state: revise / defects / repeat used
    phase_data: dict[str, Any] = field(default_factory=dict)  # plans, criteria, verification, sign-off
    roadmap: list[dict[str, Any]] = field(default_factory=list)
    roadmap_action_id: str = ""
    passed: list[str] = field(default_factory=list)
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
        if getattr(self.registry, "log", None) is None:
            self.registry.log = self.log  # injection_suspected entries go to this run's decision log
        pages_file = self.dir / "pages.json"
        if pages_file.exists():  # text of pages this run already fetched, kept across founder pauses
            try:
                self.registry.load(json.loads(pages_file.read_text(encoding="utf-8")))
            except ValueError:
                pass
        for url in rec.registered_urls:
            self.registry.restore(url)
        self.attempt = 1
        self.loop_feedback: str | None = None  # review notes, defects or founder notes for this pass
        self.feedback: str | None = None  # what the agent sees: loop feedback plus any output-check hits
        self.phase = 0
        self.architect = 0
        self.plans: list[dict[str, Any]] = []
        self.proposals: list[Any] = []
        self.criteria: list[dict[str, Any]] = []
        self.plan_criteria: list[dict[str, Any]] = []  # set while the makers run: they work to the approved plan
        self.repeats = 0  # how many times the gate sent this phase back ("repeat with new queries")
        self.outbound: dict[str, str] = {}

    @property
    def run_id(self) -> str:
        return self.rec.run_id

    @property
    def brief(self) -> dict[str, Any]:
        return self.rec.brief

    @property
    def instructions(self) -> list[str]:
        return list(self.rec.instructions)

    def outbound_id(self, key: str) -> str:
        """The action id the founder approved or rejected for one outbound action."""
        if key not in self.outbound:
            raise RuntimeError(f"no founder decision was requested for outbound action {key!r}")
        return self.outbound[key]

    def artifact(self, key: str) -> dict[str, Any]:
        return self.rec.artifacts.get(key, {})

    def _raw(self, name: str) -> str:
        p = self.outputs_dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def read_output(self, key_or_name: str) -> str:
        """The file's content without the header that write_output puts on top."""
        name = self.rec.artifacts.get(key_or_name, {}).get("output", key_or_name)
        return strip_header(self._raw(name))

    def _model_for(self, agents: str) -> str | None:
        """The model behind a file: the model of the latest log entry by one of the agents that write it."""
        wanted = {a.strip() for a in agents.split(",")} - {"gate", "founder", ""}
        for e in reversed(self.log.entries()):
            if e.get("agent") in wanted:
                return e.get("model")
        return None

    def write_output(self, name: str, content: str) -> Path:
        """Write an output file. Code puts a short header on top: phase and step, agent, model, run timestamp."""
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"bad output name {name!r}")
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        p = self.outputs_dir / name
        agents = file_agents().get(name, "code")
        p.write_text(with_header(name, content, agents, self._model_for(agents), self.rec.created_at,
                                 tuple(waiver_lines(self.rec))), encoding="utf-8")
        return p

    def append_output(self, name: str, content: str) -> None:
        self.write_output(name, self._raw(name) + content)

    def set_decision(self, name: str, line: str | None = None, add: str = "") -> None:
        """Replace the file's Decision line, or add a sentence to it (the founder's choice)."""
        lines = self._raw(name).split("\n")
        for i, text in enumerate(lines[:6]):
            if text.startswith(gates.DECISION_PREFIX):
                lines[i] = line if line is not None else (text if add in text else f"{text} {add}")
                self.write_output(name, "\n".join(lines))
                return

    def prompt(self, agent: str) -> str:
        p = settings_mod.PROMPTS_DIR / f"{agent}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


_URL = re.compile(r"https?://[^\s)\]\"'<>|]+")

# ---------------------------------------------------------------- the header on every output file

_FILE_NAME = re.compile(r"^phase(\d+)_(?:step(\d+)|gate)_(.+?)\.\w+$")
_HEADER = re.compile(r"^(?:> |<!-- )?[^|\n]+ \| Agent: [^|\n]+ \| Model: [^|\n]+ \| Run started: \S+"
                     r"(?: \| Continued against [^|\n]+)*(?: -->)?$")
HEADER_KEY = "_header"
NO_MODEL = "none (written by code)"


@lru_cache(maxsize=1)
def file_agents() -> dict[str, str]:
    """Output file name -> the agent or agents that write it."""
    return {row["output"]: row["agent"] for row in catalog()}


def file_place(name: str) -> str:
    """Phase and step of an output file, from its name."""
    m = _FILE_NAME.match(name)
    if not m:
        return "Phase 5: payment, delivery and refund test" if name.startswith("payment_test_log") else name
    n, step, short = m.group(1), m.group(2), m.group(3).replace("_", " ")
    return f"Phase {n}, step {step}: {short}" if step else f"Phase {n}, gate: {short}"


def header_fields(name: str, agents: str, model: str | None, started: str) -> dict[str, str]:
    return {"file": file_place(name), "agent": agents, "model": model or NO_MODEL, "run_started": started or "unknown"}


def has_header(text: str) -> bool:
    return bool(_HEADER.match(text.split("\n", 1)[0]))


def strip_header(text: str) -> str:
    """The content below the header line (Markdown and HTML). A JSON file keeps its header key."""
    if not has_header(text):
        return text
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    return rest[1:] if rest.startswith("\n") else rest


def with_header(name: str, content: str, agents: str, model: str | None, started: str, waivers: tuple[str, ...] = ()) -> str:
    """The content with a one-line header on top, written by code. A file that has one is left as it is.
    After the founder signed a waiver, the header of every later file carries the waiver line."""
    f = header_fields(name, agents, model, started)
    if waivers:
        f["waivers"] = "; ".join(waivers)
    line = f"{f['file']} | Agent: {f['agent']} | Model: {f['model']} | Run started: {f['run_started']}"
    line += "".join(f" | {w}" for w in waivers)
    if name.endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError:
            return content
        if not isinstance(data, dict) or HEADER_KEY in data:
            return content
        return json.dumps({HEADER_KEY: f, **data}, indent=2, ensure_ascii=False) + "\n"
    if has_header(content):
        return content
    if name.endswith(".html"):
        return f"<!-- {line} -->\n{content}"
    return f"> {line}\n\n{content}"


def scrub_unregistered_urls(text: str, registry: SourceRegistry) -> str:
    """Replace any URL this run did not fetch, so a model cannot cite from memory."""
    return _URL.sub(lambda m: m.group(0) if registry.is_registered(m.group(0).rstrip(".,;:")) else "[unverified link removed: ASSUMPTION]", text)


def call_agent(ctx: RunContext, step: Step) -> StepResult:
    """Run one agent function: citation rule, output check (one regeneration), decision log entry."""
    fn = step.fn()
    if getattr(fn, "__status__", "") == "STUBBED":
        raise StepStubbed(f"{step.key} ({step.module}.{step.func}) is STUBBED")
    base = ctx.loop_feedback
    ctx.feedback = base
    ctx.registry.begin_step()  # MAX_SOURCES counts new pages per agent step
    calls_before, cost_before = ctx.llm.usage.calls, ctx.llm.usage.cost
    for attempt in (1, 2):
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
        hint = "Your previous draft was blocked. Remove: " + "; ".join(check.hits)
        ctx.feedback = f"{base}\n{hint}" if base else hint
    used_model = ctx.llm.usage.calls > calls_before
    sources = [u for u in dict.fromkeys(result.sources + [c.url for c in claims if c.url]) if ctx.registry.is_registered(u)]
    action = f"mocked_{step.func}" if step.mocked and not used_model else step.func
    ctx.log.record(step.agent, action, sources=sources,
                   model=ctx.llm.model_label if used_model else None,
                   cost=ctx.llm.usage.cost - cost_before)
    ctx.feedback = None
    return StepResult(content, claims, sources, result.data)


def run_step(ctx: RunContext, step: Step) -> StepResult:
    """A maker step: call the agent and write its output file."""
    res = call_agent(ctx, step)
    ctx.write_output(step.output, res.content)
    ctx.rec.artifacts[step.key] = {"output": step.output, "claims": [c.to_dict() for c in res.claims],
                                   "sources": res.sources, "data": res.data}
    return res


# ================================================================ helpers

def _budget(rec: RunRecord, key: str) -> dict[str, int]:
    b = rec.budget.setdefault(key, {})
    for k in BUDGET_KEYS:
        b.setdefault(k, 0)
    return b


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_say(kind: str) -> bool:
    """SAY: someone describes the problem."""
    return str(kind).upper() in ("SAY", "PROBLEM")


def _is_pay(kind: str) -> bool:
    """PAY: someone already pays for a comparable solution."""
    return str(kind).upper() in ("PAY", "PAYING")


def _can_pivot(rec: RunRecord, state: State) -> bool:
    return Outcome.PIVOT in TRANSITIONS.get(state, {}) and rec.pivots < gates.MAX_PIVOTS_PER_RUN


def _history(rec: RunRecord, state: State, outcome: str, reasons: list[str], nxt: str) -> None:
    rec.history.append({"state": state.value, "stage": rec.stage, "outcome": outcome, "reasons": reasons,
                        "next": nxt, "at": decision_log.utc_now()})


def _move(ctx: RunContext, state: State, outcome: str, reasons: list[str], nxt: State) -> bool:
    """Leave `state` for `nxt` in the outer machine. Returns False when the loop must stop."""
    rec = ctx.rec
    _history(rec, state, outcome, reasons, nxt.value)
    rec.state = nxt.value
    rec.stage = None
    rec.pending = None
    if nxt is not state:
        rec.phase_data = {}
        rec.budget.pop(nxt.value, None)  # arriving fresh resets that state's loop budget
    if nxt is State.STOPPED:
        rec.status, rec.stop_reasons = RunStatus.STOPPED.value, reasons
        return False
    if nxt is State.DONE:
        rec.status = RunStatus.DONE.value
        return False
    return True


def _ask(ctx: RunContext, action_id: str, summary: str, choices: list[str], **extra: Any) -> bool:
    """Block until the founder chooses. Returns False (the loop pauses)."""
    ctx.approvals.request(action_id, summary, choices=choices)
    ctx.rec.pending = {"kind": "choice", "action_id": action_id, "summary": summary, "choices": choices, **extra}
    ctx.rec.status = RunStatus.AWAITING_APPROVAL.value
    return False


def _answer(ctx: RunContext) -> tuple[str, str] | None:
    """The founder's choice and note, or None while there is none. A note, a revised idea or a pivot that is
    out of scope stops the run here with the fixed reply, before any agent sees it."""
    rec = ctx.approvals.record(ctx.rec.pending["action_id"]) or {}
    if rec.get("choice") is None:
        ctx.rec.status = RunStatus.AWAITING_APPROVAL.value
        return None
    note = rec.get("note", "")
    revised = " ".join(str(v) for v in (ctx.rec.pending.get("revised") or {}).values() if isinstance(v, (str, list)))
    if (note or revised).strip() and not guardrails.screen_note(f"{note} {revised}", ctx.log).in_scope:
        _move(ctx, State(ctx.rec.state), "stop", [guardrails.SCOPE_REPLY], State.STOPPED)
        return None
    return rec["choice"], note


def _founder_section(title: str, recommendation: str, offered: list[str], choice: str, note: str,
                     outbound: dict[str, str] | None = None, approvals: ApprovalStore | None = None) -> str:
    lines = ["", f"## Founder decision: {title}", "",
             f"- Recommended by the system: {recommendation}",
             f"- Choices offered: {', '.join(offered)}",
             f"- Founder chose: **{choice}**",
             f"- Notes: {note or '-'}"]
    for key, aid in (outbound or {}).items():
        decision = approvals.decision(aid) if approvals else None
        lines.append(f"- Outbound `{key}`: {decision or 'not decided'} (action id `{aid}`)")
    return "\n".join(lines) + "\n"


# ================================================================ once per run: screen, feasibility, roadmap

def scope_classifier(llm: LLMClient) -> Callable[[str], str]:
    """Model classifier for the scope screen, asked only when the rules are unsure. Any error means out of scope."""
    def classify(text: str) -> str:
        msgs = [{"role": "system", "content": (
                    "You decide whether a product idea is in scope. Out of scope: health or medical products, services "
                    "or advice; financial or investment products or advice, including crypto; legal services or legal "
                    "advice; physical goods. Everything else is in scope. Reply with one JSON object.")},
                {"role": "user", "content": text}]
        verdict, _ = llm.structured(msgs, _ScopeVerdict, agent="guardrails", temperature=0.0)
        return verdict.verdict
    return classify


def _tick_screen(ctx: RunContext) -> bool:
    flag = ctx.rec.artifacts.pop("scope_flag", None)  # set when the assessor answered that something physical is shipped
    if flag:
        ctx.log.record("guardrails", "screen_brief", outcome="stop", reason=guardrails.OUT_OF_SCOPE, category=flag["category"])
        screen = guardrails.ScreenResult(False, [guardrails.SCOPE_REPLY], flag["category"], out_of_scope=True)
    else:
        screen = guardrails.screen_brief(ctx.brief, ctx.log, classifier=scope_classifier(ctx.llm))
    gate = gates.brief_screen_gate(screen.allowed, screen.reasons)
    if screen.out_of_scope:
        gate.checks.append(gates.Check("in scope", False, f"category: {screen.category}"))
    ctx.write_output(SCREEN_FILE, gate.to_markdown("Phase 1 gate: scope screen"))
    ctx.log.record("gate", "brief_screen_gate", outcome=gate.outcome.value,
                   **({"reason": guardrails.OUT_OF_SCOPE, "category": screen.category} if screen.out_of_scope else {}))
    nxt, note = next_state(State.SCREEN_BRIEF, gate.outcome, 0, ctx.rec.pivots)
    return _move(ctx, State.SCREEN_BRIEF, gate.outcome.value, gate.reasons + ([note] if note else []), nxt)


REVISABLE = ("idea", "target_market", "skills", "budget_usd", "hours_per_week", "values",
             "timeframe_weeks", "delegated_hours_per_week")


def feasibility_meta(rec: RunRecord) -> dict[str, Any]:
    """What later states need to know about the feasibility check: revisions used, and how the founder went on."""
    return rec.artifacts.setdefault("feasibility_meta", {"revisions": 0, "majors": 0, "continued": False, "ids": []})


def waiver_lines(rec: RunRecord) -> list[str]:
    """One line per waiver the founder signed in this run, carried by every later file header and review."""
    return [gates.waiver_line(w["recommendation"], w["milestone"], w["criteria"])
            for w in (rec.artifacts.get("waivers") or {}).get("items", [])]


def carried_lines(rec: RunRecord) -> str:
    """What every later review must show: major feasibility warnings the founder continued past, and waivers."""
    return "\n\n".join([x for x in [feasibility_line(rec)] if x] + [w + "." for w in waiver_lines(rec)])


def feasibility_line(rec: RunRecord) -> str:
    """The line the roadmap and every later milestone review carry after the founder continued past major warnings."""
    meta = rec.artifacts.get("feasibility_meta") or {}
    if not (meta.get("continued") and meta.get("majors")):
        return ""
    return f"{gates.continued_line(meta['majors'])}: {', '.join(meta.get('ids') or [])}."


def _revised_brief(rec: RunRecord, note: str) -> dict[str, Any]:
    """The brief after a revise: the fields the founder changed, or the note as the new idea when none were sent."""
    changes = {k: v for k, v in (rec.pending.get("revised") or {}).items() if k in REVISABLE}
    if not changes and note.strip():
        changes = {"idea": note.strip()}
    return guardrails.validate_brief(dict(rec.brief, **changes)), changes


# The feasibility check only warns. The run leaves FEASIBILITY for the roadmap, the scope screen or
# STOPPED on the founder's recorded choice, and for no other reason.
def _tick_feasibility(ctx: RunContext) -> bool:
    rec, state = ctx.rec, State.FEASIBILITY
    meta = feasibility_meta(rec)
    if rec.pending and "warnings" not in rec.pending:  # a question from an earlier version of this check: ask again
        rec.pending = None
    if rec.pending:
        ans = _answer(ctx)
        if ans is None:
            return False
        choice, note = ans
        p = rec.pending
        majors, ids = int(p.get("majors", 0)), list(p.get("major_ids") or [])
        section = _founder_section("feasibility", p.get("advice", ""), p["choices"], choice, note)
        if choice == "revise":
            try:
                brief, changes = _revised_brief(rec, note)
            except guardrails.BriefError as exc:  # asked again; nothing moves without a valid answer
                rec.pending = None
                rec.phase_data = {"note": f"the revised inputs were not valid ({exc}); the check was run again"}
                return True
            section += "".join(f"- Revised {k.replace('_', ' ')}: {v}\n" for k, v in changes.items()) or "- No input was changed\n"
            ctx.append_output(FEASIBILITY_FILE, section)
            ctx.set_decision(FEASIBILITY_FILE, line=gates.decision_line("repeat", ["the founder revised the idea or the inputs"]))
            meta["revisions"] = int(meta.get("revisions", 0)) + 1
            rec.brief = brief
            return _move(ctx, state, "repeat", ["the founder revised the idea or the inputs: " + (", ".join(changes) or "nothing changed")],
                         State.SCREEN_BRIEF)
        ctx.append_output(FEASIBILITY_FILE, section)
        if choice == gates.CONTINUE:
            meta.update(majors=majors, continued=True, ids=ids)
            why = f"the founder chose to continue with {majors} major feasibility warning{'' if majors == 1 else 's'}"
            ctx.set_decision(FEASIBILITY_FILE, line=gates.decision_line("go", [why]))
            return _move(ctx, state, "go", [why], State.ROADMAP)
        why = ["the founder stopped the run at the feasibility check"] + [f"{w['id']} ({w['level']}): {w['fact']}"
                                                                         for w in p.get("warnings", []) if w["level"] != gates.NONE]
        ctx.set_decision(FEASIBILITY_FILE, line=gates.decision_line("stop", why))
        return _move(ctx, state, "stop", why, State.STOPPED)

    meta.update(continued=False, majors=0, ids=[])
    res = call_agent(ctx, ROLES["assess"])
    d = res.data or {}
    if str(d.get("physical_shipped", "no")).lower() == "yes" and not gates.product_family(ctx.brief["idea"])[1]:
        # A physical item is the scope screen's case, and only that screen stops a run by itself.
        rec.artifacts["scope_flag"] = {"category": "physical goods"}
        return _move(ctx, state, "repeat", ["the assessor answered that something physical is shipped to the customer; "
                                            "the idea goes back to the scope screen"], State.SCREEN_BRIEF)
    inp = gates.FeasibilityInput(
        customer=str(d.get("customer", "")), problem=str(d.get("problem", "")),
        idea=str(ctx.brief["idea"]), family=str(d.get("family", "")),
        physical_shipped=str(d.get("physical_shipped", "no")).lower() == "yes",
        problem_source_urls=[c.url for c in res.claims if c.label == "SOURCED" and _is_say(c.kind) and c.url],
        founder_skills=list(ctx.brief["skills"]), needed_skills=[str(x) for x in d.get("needed_skills") or []],
        budget_usd=float(ctx.brief["budget_usd"]), mvp_cost_usd=_num(d.get("mvp_cost_usd")),
        hours_per_week=float(ctx.brief["hours_per_week"]), effort_hours=_num(d.get("effort_hours")),
        founder_values=list(ctx.brief["values"]), values_conflict=str(d.get("values_conflict") or ""),
        skill_kinds={str(k): str(v) for k, v in (d.get("skill_kinds") or {}).items()},
        skill_cover={str(k): str(v) for k, v in (d.get("skill_cover") or {}).items()},
        timeframe_weeks=_num(ctx.brief.get("timeframe_weeks")),
        delegated_hours_per_week=_num(ctx.brief.get("delegated_hours_per_week")) or 0.0,
    )
    warnings = gates.feasibility_warnings(inp)
    majors, minors = gates.warning_counts(warnings)
    advice = gates.feasibility_recommendation(warnings)
    report = gates.feasibility_markdown(warnings, ctx.brief["idea"], inp)
    if meta.get("revisions"):  # the newest check comes first; the earlier one follows with its decision marked as earlier
        earlier = ctx.read_output(FEASIBILITY_FILE).replace(gates.DECISION_PREFIX, "Earlier decision: ", 1)
        report = report + "\n---\n\n" + earlier
    ctx.write_output(FEASIBILITY_FILE, report)
    ctx.log.record("gate", "feasibility_check", sources=[w.evidence for w in warnings if w.evidence.startswith("http")],
                   outcome="awaiting_founder")
    rec.artifacts["feasibility"] = {"output": FEASIBILITY_FILE, "claims": [c.to_dict() for c in res.claims],
                                    "sources": res.sources, "data": d}  # later agents reuse customer, problem, search terms
    choices = [gates.CONTINUE] + (["revise"] if int(meta.get("revisions", 0)) < gates.FEASIBILITY_MAX_REPEATS else []) + ["stop"]
    summary = (f"Feasibility check: {majors} major and {minors} minor warning{'' if minors == 1 else 's'}. "
               f"The system's recommendation, which is not enforced: {advice}. You decide.")
    action_id = make_action_id("feasibility", rec.run_id, report + str(meta.get("revisions", 0)))
    return _ask(ctx, action_id, summary, choices, gate="feasibility", advice=advice, majors=majors, minors=minors,
                major_ids=[w.id for w in warnings if w.level == gates.MAJOR],
                warnings=[w.to_dict() for w in sorted(warnings, key=lambda w: {"major": 0, "minor": 1}.get(w.level, 2))],
                note=rec.phase_data.get("note", ""), asks_timeframe=not ctx.brief.get("timeframe_weeks"))


def _tick_roadmap(ctx: RunContext) -> bool:
    rec, state = ctx.rec, State.ROADMAP
    b = _budget(rec, state.value)
    if rec.pending:
        ans = _answer(ctx)
        if ans is None:
            return False
        choice, note = ans
        ctx.append_output(ROADMAP_FILE, _founder_section("roadmap", "approve", rec.pending["choices"], choice, note))
        outcome, why = {"approve": ("go", "the founder approved the roadmap"),
                        "revise": ("repeat", "the founder asked for a revision")}.get(
                            choice, ("stop", "the founder stopped the run at the roadmap"))
        ctx.set_decision(ROADMAP_FILE, line=gates.decision_line(outcome, [why]))
        if choice == "approve":
            rec.roadmap = rec.pending["milestones"]
            rec.roadmap_action_id = rec.pending["action_id"]
            return _move(ctx, state, "go", ["founder approved the roadmap"], State.P1_DISCOVER)
        rec.pending = None
        if choice == "revise":
            b["revise"] += 1
            rec.phase_data = {"feedback": f"Founder's revision notes: {note or 'revise the roadmap'}"}
            _history(rec, state, "repeat", [f"founder asked for a revision: {note or '-'}"], state.value)
            return True
        return _move(ctx, state, "stop", ["founder stopped the run at the roadmap"], State.STOPPED)

    ctx.loop_feedback = rec.phase_data.get("feedback")
    n = ctx.settings.n_architects
    proposals = []
    for i in range(n):
        ctx.architect = i
        proposals.append(call_agent(ctx, ROLES["roadmap"]).data.get("milestones"))
    if n > 1:
        ctx.proposals = proposals
        milestones = call_agent(ctx, ROLES["reconcile_roadmap"]).data.get("milestones")
    else:
        milestones = proposals[0]
    normalised, problems = gates.validate_roadmap(milestones)
    md = gates.roadmap_markdown(normalised) if normalised else "# Milestone roadmap\n\nNo valid roadmap.\n"
    check = guardrails.check_output(ROADMAP_FILE, md, ctx.log)
    problems += check.hits
    if problems:
        if not rec.phase_data.get("regenerated"):
            rec.phase_data = {"regenerated": True, "feedback": "Fix the roadmap: " + "; ".join(problems)}
            ctx.log.record("gate", "roadmap_check", outcome="revise")
            return True
        ctx.write_output(ROADMAP_FILE, gates.decision_line("stop", ["the roadmap failed the code checks twice"] + problems)
                         + "\n\n" + md + "\n## Problems\n\n" + "\n".join(f"- {p}" for p in problems) + "\n")
        ctx.log.record("gate", "roadmap_check", outcome="stop")
        return _move(ctx, state, "stop", ["the roadmap failed the code checks twice"] + problems, State.STOPPED)
    how = (f"{n} planners proposed roadmaps; the checker reconciled them." if n > 1
           else "One planner wrote the roadmap; code validated it.")
    carried = carried_lines(rec)
    ctx.write_output(ROADMAP_FILE, gates.decision_line("pending", ["the roadmap passed the code checks and waits for the founder"])
                     + "\n\n" + md + f"\n{how} Gates and order are enforced by code.\n" + (f"\n{carried}\n" if carried else ""))
    ctx.log.record("gate", "roadmap_check", outcome="awaiting_founder")
    choices = ["approve"] + (["revise"] if b["revise"] < gates.ROADMAP_MAX_REVISIONS else []) + ["stop"]
    action_id = make_action_id("roadmap", rec.run_id, md + str(b["revise"]))
    return _ask(ctx, action_id, "Approve the six-milestone roadmap before any work starts", choices,
                milestones=normalised, gate="roadmap")


def roadmap_approved(ctx: RunContext) -> bool:
    aid = ctx.rec.roadmap_action_id
    return bool(aid) and ctx.approvals.choice(aid) == "approve"


# ================================================================ sign-off gates per phase (code only)

def _claims(ctx: RunContext, *keys: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in keys:
        out += ctx.artifact(k).get("claims", [])
    return out


def _signoff_p1(ctx: RunContext, attempt: int) -> GateResult:
    scoring = ctx.artifact("phase1_step2").get("data", {})
    pick = str(scoring.get("pick") or "")
    # The gate looks at the chosen opportunity's claims (plus the market claims), not at every idea considered.
    claims = [c for c in _claims(ctx, "phase1_step1") if not pick or c.get("group", "") in ("", pick)]
    claims += _claims(ctx, "phase1_step3")
    ev = gates.Phase1Evidence(
        problem_source_urls=[c["url"] for c in claims if c["label"] == "SOURCED" and _is_say(c["kind"])],
        paying_source_urls=[c["url"] for c in claims if c["label"] == "SOURCED" and _is_pay(c["kind"])],
        budget_usd=float(ctx.brief["budget_usd"]),
        hours_per_week=float(ctx.brief["hours_per_week"]),
        mvp_cost_usd=_num(scoring.get("mvp_cost_usd")),
        mvp_hours_per_week=_num(scoring.get("mvp_hours_per_week")),
        total_claims=len(claims),
        assumption_claims=sum(1 for c in claims if c["label"] != "SOURCED"),
        search_notes=[f"{name}: {why}" for name, why in sorted(sources_mod.SEARCH_REPORT.items())],
        core_skill_missing=_core_skill_missing(ctx),
    )
    return gates.phase1_gate(ev, attempt)


def _core_skill_missing(ctx: RunContext) -> bool:
    """True when the feasibility check found a skill needed to make the product that the founder lacks."""
    d = ctx.artifact("feasibility").get("data", {})
    kinds = d.get("skill_kinds") or {}
    return any(kinds.get(s, "core") != "supporting" and not gates._skill_covered(s, list(ctx.brief["skills"]))
               for s in d.get("needed_skills") or [])


def _signoff_p2(ctx: RunContext, attempt: int) -> GateResult:
    claims = _claims(ctx, "phase2_step1")

    def sourced(box: str) -> int:
        return sum(1 for c in claims if c["label"] == "SOURCED" and c.get("group") == box)

    page = ctx.read_output("phase2_step2").lower()
    test = ctx.artifact("phase2_step3").get("data", {})
    return gates.phase2_gate(sourced("problem"), sourced("customer_segments"), "<form" in page and 'type="email"' in page,
                             str(test.get("status", "not_measured")), str(test.get("detail", "no demand test was written")),
                             attempt)


def _signoff_p3(ctx: RunContext, attempt: int) -> GateResult:
    tests = ctx.artifact("phase3_step2").get("data", {}).get("tests", {})
    gate = gates.tests_gate({str(k): bool(v) for k, v in tests.items()}, attempt)
    ctx.write_output(TESTS_FILE, gate.to_markdown("Phase 3 gate: product tests"))
    return gate


def _signoff_p4(ctx: RunContext, attempt: int) -> GateResult:
    d = ctx.artifact("phase4_step1").get("data", {})
    return gates.phase4_gate(_num(d.get("price_usd")), str(d.get("product_name", "")), ctx.read_output("phase4_step2"), attempt)


def _signoff_p5(ctx: RunContext, attempt: int) -> GateResult:
    posts = ctx.artifact("phase5_step2").get("data", {}).get("posts", [])
    return gates.phase5_gate(ctx.read_output("phase5_step1"), posts if isinstance(posts, list) else [], attempt)


def _signoff_p6(ctx: RunContext, attempt: int) -> GateResult:
    return gates.phase6_gate(ctx.artifact("phase6_step2").get("data", {}).get("metrics", []))


SIGN_OFF_GATES: dict[State, Callable[[RunContext, int], GateResult]] = {
    State.P1_DISCOVER: _signoff_p1, State.P2_VALIDATE: _signoff_p2, State.P3_BUILD: _signoff_p3,
    State.P4_COMMERCIAL: _signoff_p4, State.P5_LAUNCH: _signoff_p5, State.P6_OPERATE: _signoff_p6,
}


# ================================================================ phase files

def _budget_md(b: dict[str, int]) -> str:
    lines = ["## Loop budget", "", "Limit of 2 per phase: ASSUMPTION.", "", "| Back-edge | Used | Limit |", "|---|---|---|"]
    lines += [f"| {k} | {b.get(k, 0)} | {gates.MAX_BACK_EDGES} |" for k in BUDGET_KEYS]
    return "\n".join(lines) + "\n"


def _plan_review_md(n: int, plans: list[dict], chosen: int, signal: Signal, why: str, notes: str,
                    problems: list[str], b: dict[str, int]) -> str:
    plan = plans[chosen] if plans else {"criteria": []}
    outcome = {"approved": "go", "revise": "repeat"}.get(signal.value, "stop")
    lines = [gates.decision_line(outcome, problems or ["every criterion has an id, a check, a threshold and a method"]), "",
             f"# Phase {n} plan review: {gates.PHASE_NAMES[n]}", "",
             f"Plans proposed: {len(plans)}. Plan chosen: architect {plan.get('architect', 0) + 1}.",
             f"Why: {why or '-'}", "", "## Criteria (written before any maker ran)", "",
             "| Id | Check | Threshold | Method |", "|---|---|---|---|"]
    for c in plan.get("criteria") or []:
        c = c if isinstance(c, dict) else {}
        lines.append(f"| {c.get('id', '')} | {c.get('check', '')} | {c.get('threshold', '')} | {c.get('method', '')} |")
    lines += ["", f"**Verdict: {signal.value.upper()}**", ""]
    lines += [f"- code check: {p}" for p in problems] + ([f"- checker notes: {notes}"] if notes else [])
    return "\n".join(lines) + "\n\n" + _budget_md(b)


def _decision_md(n: int, criteria: list[dict], rows: list[dict], gate: GateResult, b: dict[str, int], carried: str = "") -> str:
    by_id = {str(c.get("id")): c for c in criteria if isinstance(c, dict)}
    lines = ["## Verification report (checker)", "",
             "| Id | Check | Threshold | Result | Evidence |", "|---|---|---|---|---|"]
    for r in rows:
        c = by_id.get(r["id"], {})
        lines.append(f"| {r['id']} | {c.get('check', '')} | {c.get('threshold', '')} | {r['result']} | {r['evidence']} |")
    top = [gates.decision_line(gate.outcome, gate.reasons), "", f"# Phase {n} gate decision: {gates.PHASE_NAMES[n]}", ""]
    top += ([carried, ""] if carried else []) + [gate.body_markdown(f"Sign-off (gates.py): {gate.gate}", level=2)]
    return "\n".join(top) + "\n" + "\n".join(lines) + "\n\n" + _budget_md(b)


def _valid_evidence(ctx: RunContext, evidence: str) -> bool:
    if evidence == "ASSUMPTION" or ctx.registry.is_registered(evidence):
        return True
    names = {p.name for p in ctx.outputs_dir.glob("*")} if ctx.outputs_dir.exists() else set()
    tests = set()
    for a in ctx.rec.artifacts.values():
        t = a.get("data", {}).get("tests")
        if isinstance(t, dict):
            tests |= {str(k) for k in t}
    return evidence in names or evidence in tests


_SOURCE_WORDS = re.compile(r"\b(?:sources?|sourced|urls?|links?|citations?|cited|cites?)\b", re.IGNORECASE)


def _about_sources(criterion: Any) -> bool:
    """A criterion with method "source", or one that mentions a source, URL, link or citation."""
    if not isinstance(criterion, dict):
        return False
    text = f"{criterion.get('check', '')} {criterion.get('threshold', '')}"
    return str(criterion.get("method", "")).lower() == "source" or bool(_SOURCE_WORDS.search(text))


def _normalise_rows(ctx: RunContext, rows: Any, criteria: list[dict]) -> list[dict]:
    """The checker reports; code decides what counts as evidence."""
    given = {str(r.get("id")): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
    out = []
    for c in criteria:
        cid = str(c.get("id", "")) if isinstance(c, dict) else ""
        r = given.get(cid, {})
        result = str(r.get("result", "unverified")).lower()
        result = result if result in ("pass", "fail", "unverified") else "unverified"
        evidence = str(r.get("evidence") or "ASSUMPTION").strip()
        if not _valid_evidence(ctx, evidence):
            evidence = "ASSUMPTION"
        if _about_sources(c) and result == "pass" and not ctx.registry.is_registered(evidence):
            # A criterion about a cited source is decided against the registry: a pass must name a page this
            # run fetched. A file name is not evidence for it, and with nothing fetched it cannot pass.
            result, evidence = "unverified", "no fetched source was named for this criterion"
        if result == "pass" and evidence == "ASSUMPTION":
            result = "unverified"
        out.append({"id": cid, "result": result, "evidence": evidence})
    return out


# ================================================================ inner loop tick

def _inner(ctx: RunContext, state: State, stage: Stage, signal: Signal, reasons: list[str],
           feedback: str | None = None) -> bool:
    rec = ctx.rec
    b = _budget(rec, state.value)
    target, eff, spent, note = inner_next(stage, signal, b)
    if spent:
        b[spent] += 1
    reasons = reasons + ([note] if note else [])
    rec.phase_data["feedback"] = feedback if spent else None
    if target is Exit.STOPPED:
        return _move(ctx, state, eff.value, reasons, State.STOPPED)
    if isinstance(target, Exit):  # NEXT_PHASE and PIVOT_PHASE are handled by the milestone review
        raise RuntimeError(f"unexpected exit {target} from {stage}")
    if target is Stage.MILESTONE_REVIEW:
        rec.phase_data["recommendation"] = eff.value
    _history(rec, state, eff.value, reasons, f"{state.value}.{target.value}")
    rec.stage = target.value
    return True


def _tick_phase(ctx: RunContext, state: State) -> bool:
    rec = ctx.rec
    n = PHASE_OF[state]
    ctx.phase = n
    pd = rec.phase_data
    b = _budget(rec, state.value)
    if rec.stage is None:
        if not roadmap_approved(ctx):
            return _move(ctx, state, "stop", ["no founder approval of the roadmap is on record; no phase may start"],
                         State.STOPPED)
        rec.stage = Stage.PLAN.value
    stage = Stage(rec.stage)
    ctx.loop_feedback = pd.get("feedback")
    ctx.attempt = b["defects"] + b["repeat"] + 1  # which pass through the makers this is
    ctx.repeats = b["repeat"]
    ctx.plan_criteria = (pd.get("criteria") or []) if stage is Stage.MAKE else []

    if stage is Stage.PLAN:
        k = ctx.settings.n_architects if n == 1 else 1
        plans = []
        for i in range(k):
            ctx.architect = i
            try:
                r = call_agent(ctx, _role("plan", n))
                # The gate's own checks go into the plan by code; the planner adds criteria for the files' quality.
                criteria = gates.gate_criteria(n) + [c for c in (r.data.get("criteria") or []) if isinstance(c, dict)]
                plans.append({"architect": i, "criteria": criteria, "text": r.content, "error": ""})
            except (ModelOutputInvalid, OutputBlocked) as exc:  # the plan review sends it back
                plans.append({"architect": i, "criteria": [], "text": "", "error": str(exc)})
        pd["plans"] = plans
        return _inner(ctx, state, stage, Signal.DONE, [f"{k} plan(s) written"])

    if stage is Stage.PLAN_REVIEW:
        # Plan review is code: every criterion needs an id, a check, a threshold and a method.
        plans = pd.get("plans") or []
        checked = [gates.check_plan(p["criteria"]) + ([p["error"]] if p.get("error") else []) for p in plans]
        chosen = next((i for i, probs in enumerate(checked) if not probs), 0)
        problems = checked[chosen] if checked else ["no plan was written"]
        signal = Signal.REVISE if problems else Signal.APPROVED
        if len(plans) > 1:
            why = (f"plan {chosen + 1} is the first whose criteria all pass the code check" if not problems
                   else "no plan passed the code check")
        else:
            why = "one planner" + ("" if not problems else "; its plan failed the code check")
        pd["criteria"] = plans[chosen]["criteria"] if plans else []
        ctx.write_output(plan_review_file(n), _plan_review_md(n, plans, chosen, signal, why, "", problems, b))
        ctx.log.record("gate", f"phase{n}_plan_review", outcome=signal.value)
        feedback = "Fix the plan: " + "; ".join(problems) if problems else None
        return _inner(ctx, state, stage, signal, problems or ["plan review: approved"], feedback)

    if stage is Stage.MAKE:
        blocked: list[dict[str, str]] = []
        for step in STEPS[state]:
            try:
                run_step(ctx, step)
            except (OutputBlocked, ModelOutputInvalid) as exc:  # blocks only this file; the run goes on
                blocked.append({"output": step.output, "reason": str(exc)})
                ctx.log.record(step.agent, f"{step.func}_not_written",
                               outcome="model_output_invalid" if isinstance(exc, ModelOutputInvalid) else "blocked")
        pd["blocked"] = blocked
        return _inner(ctx, state, stage, Signal.DONE,
                      [f"not written: {', '.join(x['output'] for x in blocked)}"] if blocked else ["artefacts produced"])

    if stage is Stage.VERIFY:
        criteria = pd.get("criteria") or []
        # Gate criteria are measured by code, with the same checks the sign-off will run. A miss there is the
        # gate's to act on (repeat, then stop), so it is shown in the report but is not a checker defect.
        measured = {c.name: c for c in SIGN_OFF_GATES[state](ctx, ctx.attempt).checks}
        rows = []
        for c in criteria:
            if c.get("by") == "gate":
                m = measured.get(c["check"])
                result = "unverified" if not m or not m.measured else ("pass" if m.passed else "fail")
                rows.append({"id": c["id"], "result": result, "code": True,
                             "evidence": "code: " + (m.detail if m else "not measured")})
        ctx.criteria = [c for c in criteria if c.get("by") != "gate"]
        given = []
        if ctx.criteria:
            try:
                given = (call_agent(ctx, _role("verify", n)).data or {}).get("rows")
            except (ModelOutputInvalid, OutputBlocked) as exc:
                pd["verify_error"] = str(exc)
        rows += _normalise_rows(ctx, given, ctx.criteria)
        rows += [{"id": f"blocked:{x['output']}", "result": "fail", "evidence": x["reason"][:160]}
                 for x in pd.get("blocked", [])]
        pd["verification"] = rows
        defects = [x["id"] for x in rows if x["result"] == "fail" and not x.get("code")]
        signal = Signal.DEFECTS if defects else Signal.VERIFIED
        ctx.log.record("gate", f"phase{n}_verification", outcome=signal.value)
        notes = [f"{x['id']}: {x['evidence']}" for x in rows if x["result"] == "fail" and x["id"].startswith("blocked:")]
        feedback = ("Fix these failed criteria: " + ", ".join(defects) + (". " + "; ".join(notes) if notes else "")
                    if defects else None)
        return _inner(ctx, state, stage, signal, [f"defects: {', '.join(defects)}"] if defects else ["verified"], feedback)

    if stage is Stage.SIGN_OFF:
        ctx.attempt = b["repeat"] + 1
        gate = SIGN_OFF_GATES[state](ctx, ctx.attempt)
        open_defects = [x["id"] for x in pd.get("verification", []) if x["result"] == "fail" and not x.get("code")]
        gate = gates.with_open_defects(gate, open_defects, _can_pivot(rec, state))
        if gate.outcome is Outcome.REPEAT and b["repeat"] >= gates.MAX_BACK_EDGES:
            gate = GateResult(gate.gate, Outcome.STOP, gate.reasons + ["repeat budget used up: recommend STOP"], gate.checks)
        pd["signoff"] = {"gate": gate.gate, "outcome": gate.outcome.value, "reasons": gate.reasons}
        pd["signoff"]["failed"] = ([c.name for c in gate.checks if c.measured and not c.passed]
                                   or (["no evidence was collected"] if any(not c.measured for c in gate.checks) else [])
                                   or [r for r in gate.reasons][:2])
        ctx.write_output(decision_file(n), _decision_md(n, pd.get("criteria") or [], pd.get("verification", []), gate, b,
                                                        carried_lines(rec)))
        ctx.log.record("gate", f"{gate.gate}_gate", outcome=gate.outcome.value)
        return _inner(ctx, state, stage, Signal(gate.outcome.value), gate.reasons, "; ".join(gate.reasons))

    return _tick_milestone(ctx, state)


# Requirement R-FOUNDER-GATE: a run moves on to the next phase only when the founder's recorded
# choice at this phase's milestone review is "continue"; with no choice it stays awaiting_approval.
# Verified by: tests/test_inner_loop.py::test_no_phase_advances_without_a_founder_decision
def _tick_milestone(ctx: RunContext, state: State) -> bool:
    rec = ctx.rec
    n = PHASE_OF[state]
    pd = rec.phase_data
    b = _budget(rec, state.value)
    recommendation = Outcome(pd.get("recommendation", "stop"))

    if not rec.pending:
        choices = gates.milestone_choices(recommendation, b["repeat"] < gates.MAX_BACK_EDGES, _can_pivot(rec, state),
                                          waivable=n in gates.EVIDENCE_GATE_PHASES)
        outbound: dict[str, str] = {}
        if recommendation is Outcome.GO:
            for ob in OUTBOUND.get(state, []):
                payload = "".join(ctx.read_output(k) for k in ob.payload_keys)
                oid = make_action_id(ob.key, rec.run_id, payload)
                ctx.approvals.request(oid, ob.summary)
                outbound[ob.key] = oid
        pd["outbound"] = outbound
        summary = (f"Milestone M{n} ({gates.PHASE_NAMES[n]}): the gate recommends {recommendation.value.upper()}. "
                   f"Review {decision_file(n)} and decide.")
        action_id = make_action_id(f"milestone_m{n}", rec.run_id, ctx.read_output(decision_file(n)) + str(len(rec.history)))
        ctx.log.record("gate", f"milestone_review_m{n}", outcome="awaiting_founder")
        return _ask(ctx, action_id, summary, choices, gate=f"milestone_m{n}", milestone=f"M{n}",
                    recommendation=recommendation.value, outbound=outbound)

    ans = _answer(ctx)
    if ans is None:
        return False
    choice, note = ans
    outbound = pd.get("outbound", {})
    if choice == gates.CONTINUE:
        undecided = [k for k, aid in outbound.items() if ctx.approvals.decision(aid) is None]
        if undecided:  # every outbound action needs its own approve or reject before the phase closes
            rec.pending["waiting_for"] = undecided
            rec.status = RunStatus.AWAITING_APPROVAL.value
            return False
    if not pd.get("decision_written"):
        ctx.append_output(decision_file(n), _founder_section(f"milestone M{n}", recommendation.value,
                                                             rec.pending["choices"], choice, note, outbound, ctx.approvals))
        ctx.set_decision(decision_file(n), add=f"Founder chose: {choice}.")
        pd["decision_written"] = True

    if choice == gates.CONTINUE:
        ctx.outbound = outbound
        done = pd.setdefault("after_done", [])
        try:
            for step in AFTER_APPROVAL.get(state, []):
                if step.key in done:
                    continue
                if any(ctx.approvals.decision(outbound.get(k, "")) == "approved" for k in step.needs):
                    try:
                        run_step(ctx, step)
                    except (OutputBlocked, ModelOutputInvalid) as exc:  # blocks only this file
                        ctx.log.record(step.agent, f"{step.func}_not_written", outcome="blocked")
                        pd.setdefault("after_blocked", []).append({"output": step.output, "reason": str(exc)})
                else:
                    ctx.log.record(step.agent, f"skipped_{step.func}", outcome="rejected")
                done.append(step.key)
        except (StepStubbed, LLMError) as exc:
            rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, str(exc)
            return False
        rec.passed.append(state.value)
        return _move(ctx, state, "continue", [f"founder chose continue at M{n}"], TRANSITIONS[state][Outcome.GO])

    if choice == gates.WAIVER:
        # The founder goes on against the recommendation. The gate's recommendation and its failed criteria
        # stay in the decision file as they were; the waiver is added to the record and carried forward.
        if n not in gates.EVIDENCE_GATE_PHASES or recommendation is Outcome.GO or not note.strip():
            return _move(ctx, state, "stop", [f"a waiver was not possible at M{n}"], State.STOPPED)
        failed = list((pd.get("signoff") or {}).get("failed") or [])
        ctx.log.record("founder", f"waiver_m{n}", approved_by="founder", outcome="waiver", reason=note.strip()[:400],
                       category="; ".join(failed)[:400] or "no criterion was named")
        rec.artifacts.setdefault("waivers", {"items": []})["items"].append(
            {"milestone": n, "recommendation": recommendation.value, "criteria": failed, "reason": note.strip()})
        rec.passed.append(state.value)
        return _move(ctx, state, "waiver", [gates.waiver_line(recommendation.value, n, failed), f"founder's reason: {note.strip()}"],
                     TRANSITIONS[state][Outcome.GO])

    rec.pending = None
    pd["decision_written"] = False
    if choice == gates.REQUEST_CHANGES:
        return _inner(ctx, state, Stage.MILESTONE_REVIEW, Signal.REQUEST_CHANGES,
                      [f"founder asked for changes at M{n}: {note or '-'}"], note or "the founder asked for changes")
    if choice == gates.PIVOT:
        nxt, why = next_state(state, Outcome.PIVOT, 0, rec.pivots)
        if nxt is not State.STOPPED:
            rec.pivots += 1
        if nxt is State.SCREEN_BRIEF and note.strip():
            rec.brief = guardrails.validate_brief(dict(rec.brief, idea=note.strip()))
        return _move(ctx, state, "pivot", [f"founder chose pivot at M{n}"] + ([why] if why else []), nxt)
    return _move(ctx, state, "stop", [f"founder chose stop at M{n}"], State.STOPPED)


# ================================================================ run loop

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


class _Verdict(BaseModel):
    verdict: Literal["allow", "refuse"]
    reason: ShortText


class _ScopeVerdict(BaseModel):
    verdict: Literal["in_scope", "out_of_scope"]
    reason: ShortText


def request_classifier(llm: LLMClient) -> Callable[[str], str]:
    """Model classifier for ambiguous founder instructions only. guardrails refuses on any error (fail closed)."""
    def classify(text: str) -> str:
        msgs = [{"role": "system", "content": settings_mod.PROMPTS_DIR.joinpath("guardrails.md").read_text(encoding="utf-8")},
                {"role": "user", "content": text}]
        verdict, _ = llm.structured(msgs, _Verdict, agent="guardrails", temperature=0.0)
        return verdict.verdict
    return classify


def revise_inputs(run_id: str, action_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Store the inputs the founder changed on the feasibility screen, next to the pending decision.
    The brief itself changes only when the founder's "revise" choice is processed."""
    with _lock_for(run_id):
        rec = load(run_id)
        if not rec.pending or rec.pending.get("gate") != "feasibility" or rec.pending.get("action_id") != action_id:
            raise ValueError("no feasibility decision is waiting with that id")
        changes = {k: v for k, v in changes.items() if k in REVISABLE}
        guardrails.validate_brief(dict(rec.brief, **changes))  # raises BriefError on a bad value
        rec.pending["revised"] = changes
        save(rec)
        return changes


def instruct(run_id: str, text: str, s: settings_mod.Settings | None = None,
             classifier: Callable[[str], str] | None = None) -> guardrails.RequestScreen:
    """A founder instruction during a run. Screened before any agent sees it; secrets are never stored."""
    s = s or settings_mod.load()
    with _lock_for(run_id):
        rec = load(run_id)
        log = DecisionLog(run_dir(run_id) / "decision_log.json")
        screen = guardrails.screen_request(text, log, classifier=classifier or request_classifier(LLMClient(s)))
        if not screen.refused:
            rec.instructions.append(screen.redacted_text)
            save(rec)
        return screen


def advance(run_id: str, s: settings_mod.Settings | None = None, llm: LLMClient | None = None,
            registry: SourceRegistry | None = None, max_transitions: int = 200) -> RunRecord:
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
        calls_before = int((rec.usage or {}).get("calls", 0))
        seconds_before = float((rec.usage or {}).get("seconds", 0.0))
        started = time.monotonic()

        def call_guard() -> None:  # run budget: checked before every model call
            if calls_before + ctx.llm.usage.calls >= s.max_model_calls:
                raise CallBudgetExceeded(f"run budget: MAX_MODEL_CALLS={s.max_model_calls} reached")

        ctx.llm.before_call = call_guard
        try:
            for _ in range(max_transitions):
                if _stop_flag(run_id).exists():
                    _apply_stop(rec, ctx.log, _stop_flag(run_id).read_text(encoding="utf-8") or "founder pressed stop")
                    break
                if seconds_before + (time.monotonic() - started) >= s.max_run_seconds:
                    ctx.log.record("orchestrator", "run_budget", outcome="stop")
                    _move(ctx, State(rec.state), "stop", [f"run budget: MAX_RUN_SECONDS={s.max_run_seconds:g} reached"],
                          State.STOPPED)
                    break
                if not _tick(ctx):
                    break
            else:
                _move(ctx, State(rec.state), "stop", ["transition limit reached"], State.STOPPED)
        finally:
            ctx.llm.before_call = None
            rec.registered_urls = ctx.registry.urls()
            (ctx.dir / "pages.json").write_text(json.dumps(ctx.registry.dump(), ensure_ascii=False), encoding="utf-8")
            usage = ctx.llm.usage.as_dict()
            usage["seconds"] = round(time.monotonic() - started, 3)
            prev = rec.usage or {}
            rec.usage = {k: (prev.get(k, 0) + v) for k, v in usage.items()}
            ctx.llm.usage = type(ctx.llm.usage)()
            save(rec)
        return rec


def _stop_flag(run_id: str) -> Path:
    return run_dir(run_id) / "stop_requested"


def _apply_stop(rec: RunRecord, log: DecisionLog, reason: str) -> None:
    if State(rec.state) in TERMINAL:
        return
    log.record("founder", "stop_run", approved_by="founder", outcome="stop")
    rec.history.append({"state": rec.state, "stage": rec.stage, "outcome": "stop", "reasons": [reason],
                        "next": State.STOPPED.value, "at": decision_log.utc_now()})
    rec.state, rec.stage, rec.pending = State.STOPPED.value, None, None
    rec.status, rec.stop_reasons = RunStatus.STOPPED.value, [reason]


def request_stop(run_id: str, reason: str = "founder pressed stop") -> RunRecord:
    """The founder's stop button. Always available: a running run stops before its next step."""
    flag = _stop_flag(run_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(reason, encoding="utf-8")
    lock = _lock_for(run_id)
    if lock.acquire(blocking=False):  # not running right now: stop at once
        try:
            rec = load(run_id)
            _apply_stop(rec, DecisionLog(run_dir(run_id) / "decision_log.json"), reason)
            save(rec)
        finally:
            lock.release()
    return load(run_id)


def _tick(ctx: RunContext) -> bool:
    """One step of the machine. Returns False when the loop must pause or end."""
    rec = ctx.rec
    state = State(rec.state)
    if state in TERMINAL:
        rec.status = RunStatus.DONE.value if state is State.DONE else RunStatus.STOPPED.value
        return False
    try:
        if state is State.SCREEN_BRIEF:
            return _tick_screen(ctx)
        if state is State.FEASIBILITY:
            return _tick_feasibility(ctx)
        if state is State.ROADMAP:
            return _tick_roadmap(ctx)
        return _tick_phase(ctx, state)
    except StepStubbed as exc:  # nothing was spent; resume retries the same stage
        rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, str(exc)
        return False
    except CallBudgetExceeded as exc:
        ctx.log.record("orchestrator", "run_budget", outcome="stop")
        return _move(ctx, state, "stop", [str(exc)], State.STOPPED)
    except LLMError as exc:
        rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, f"model unavailable: {exc}"
        return False
    except (OutputBlocked, ModelOutputInvalid) as exc:  # outside a phase there is no loop to repeat in
        if state is State.FEASIBILITY:  # only the founder ends a run here: pause, so it can be resumed or stopped
            rec.status, rec.blocked_reason = RunStatus.BLOCKED.value, f"the feasibility check could not be written: {exc}"
            return False
        return _move(ctx, state, "stop", [str(exc)], State.STOPPED)


# ================================================================ milestone tracker

def milestones(rec: RunRecord) -> list[dict[str, Any]]:
    """The six milestones with their status: pending, in progress, awaiting founder, passed or stopped."""
    titles = {m["phase"]: m.get("title", "") for m in rec.roadmap}
    out = []
    for st in PHASE_STATES:
        n = PHASE_OF[st]
        if rec.state == st.value:
            if rec.status == RunStatus.STOPPED.value:
                status = "stopped"
            elif rec.status == RunStatus.AWAITING_APPROVAL.value:
                status = "awaiting founder"
            else:
                status = "in progress"
        elif rec.state == State.STOPPED.value and rec.history and rec.history[-1]["state"] == st.value:
            status = "stopped"
        elif st.value in rec.passed and PHASE_STATES.index(st) < _position(rec):
            status = "passed"
        else:
            status = "pending"
        out.append({"id": f"M{n}", "phase": n, "name": gates.PHASE_NAMES[n], "title": titles.get(n, ""),
                    "status": status, "stage": rec.stage if rec.state == st.value else None})
    return out


def _position(rec: RunRecord) -> int:
    """Index of the current phase (6 when done; 0 before Phase 1)."""
    if rec.state == State.DONE.value:
        return len(PHASE_STATES)
    if rec.state == State.STOPPED.value:
        last = rec.history[-1]["state"] if rec.history else ""
        return next((i for i, s in enumerate(PHASE_STATES) if s.value == last), len(PHASE_STATES))
    return next((i for i, s in enumerate(PHASE_STATES) if s.value == rec.state), 0)


# ================================================================ promote a real run

def labelled_name(name: str, label: str | None) -> str:
    """The committed file name for a run's output: unchanged for the main run, with the label added
    to the short name for any other run (phase1_gate_feasibility.md -> phase1_gate_feasibility_weak.md)."""
    if not label:
        return name
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_{label}{dot}{ext}" if dot else f"{name}_{label}"


def brief_file(brief: dict[str, Any]) -> Path | None:
    """The file under briefs/ that holds exactly this brief, if there is one."""
    folder = settings_mod.BRIEFS_DIR
    for p in sorted(folder.glob("*.json")) + sorted((folder / "promoted").glob("*.json")):
        try:
            if json.loads(p.read_text(encoding="utf-8")) == brief:
                return p
        except ValueError:
            continue
    return None


def keep_brief(rec: RunRecord, label: str | None = None) -> Path:
    """A committed run can be repeated only if its brief is in the repository. A brief typed into the
    form on the home page is saved under briefs/promoted/ when no brief file holds the same content."""
    found = brief_file(rec.brief)
    if found:
        return found
    name = label or re.sub(r"[^a-z0-9]+", "_", str(rec.brief.get("idea", "")).lower()).strip("_")[:40] or "brief"
    dest = settings_mod.BRIEFS_DIR / "promoted" / f"{name}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rec.brief, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


# Requirement R-REAL-OUTPUTS: a run is copied into outputs/ and the root decision_log.json only if
# it ran in live mode, is not running and has no mock-model log entry; otherwise PromoteError.
# Verified by: tests/test_promote.py::test_mock_and_unfinished_runs_are_refused
def promote(run_id: str, label: str | None = None, replace: bool = False) -> list[Path]:
    """Copy a finished live run into the committed outputs/ and decision_log.json.

    This is the only code path that writes to the repository's outputs/ or root decision_log.json.
    It refuses mock runs and runs still in progress. The main run keeps the standard file names;
    every other run is promoted with a label that is added to each file's short name, so several
    runs sit side by side in outputs/ and none overwrites another. decision_log.json holds the
    entries of all promoted runs in time order. `replace` first clears outputs/ and the log.
    The run's brief is kept under briefs/ if it is not there already, so the run can be repeated.
    """
    rec = load(run_id)
    entries = DecisionLog(run_dir(run_id) / "decision_log.json").entries()
    if rec.mode != "live" or any(e.get("model") == MOCK_MODEL_NAME for e in entries):
        raise PromoteError("mock-mode runs are never promoted to outputs/ or decision_log.json")
    if rec.status == RunStatus.RUNNING.value:
        raise PromoteError("run is still in progress")
    if label is not None and not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", label):
        raise PromoteError("label must be lowercase letters and digits, with single underscores between words")
    out_dir = settings_mod.OUTPUTS_DIR
    files = [f for f in sorted((run_dir(run_id) / "outputs").glob("*")) if f.is_file()]
    if replace:
        for p in out_dir.iterdir() if out_dir.exists() else []:
            if p.name == ".gitkeep":
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        decision_log.reset(settings_mod.ROOT_LOG)
    existing = decision_log.read(settings_mod.ROOT_LOG)
    if entries and all(e in existing for e in entries):
        raise PromoteError(f"run {run_id} is already in decision_log.json")
    taken = [labelled_name(f.name, label) for f in files if (out_dir / labelled_name(f.name, label)).exists()]
    if taken:
        raise PromoteError(f"outputs/ already holds {', '.join(taken[:3])}; promote this run with a label "
                           "(for example --label weak), or use --replace to start outputs/ and the log over")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for f in files:
        dest = out_dir / labelled_name(f.name, label)
        shutil.copy2(f, dest)
        written.append(dest)
    merged = sorted(existing + entries, key=lambda e: e["timestamp"])  # stable: order inside a run is kept
    decision_log.reset(settings_mod.ROOT_LOG)
    for e in merged:
        decision_log.append(e, settings_mod.ROOT_LOG)
    keep_brief(rec, label)
    return written

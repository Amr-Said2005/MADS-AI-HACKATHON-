"""Web UI: start runs, follow the milestone tracker, make founder decisions, approve or reject
each outbound action, and send screened instructions.

Server-rendered HTML; no front-end build. Run it with `docker compose up` or
`uvicorn app.main:app --reload` from the repository root.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import delivery
import demand
import gates
import guardrails
import orchestrator as orch
import payments
import reconcile
import refunds
import settings as settings_mod
from agents import launcher, operator
from app import command, plain, render
from approval import ApprovalStore
from decision_log import DecisionLog
from guardrails import BriefError
from llm import LLMClient

INTERRUPTED = "the app restarted while this run was working; press Resume to continue from the last finished step"


def pause_interrupted_runs() -> list[str]:
    """Runs are worked on by threads of this process. A run still marked as working when the app starts
    has no worker any more, so it is paused; the run page then offers Resume, which continues from its saved state."""
    paused = []
    for rec in orch.list_runs():
        if rec.status == orch.RunStatus.RUNNING.value:
            rec.status, rec.blocked_reason = orch.RunStatus.BLOCKED.value, INTERRUPTED
            orch.save(rec)
            paused.append(rec.run_id)
    return paused


def answered_runs() -> list[str]:
    """Runs that wait for the founder although the founder has already answered: the choice is recorded and
    no outbound action is left undecided. A restart between the answer and the next step leaves a run so."""
    out = []
    for rec in orch.list_runs():
        p = rec.pending or {}
        if rec.status != orch.RunStatus.AWAITING_APPROVAL.value or not p.get("action_id"):
            continue
        store = _store(rec.run_id)
        if store.choice(p["action_id"]) is None:
            continue
        if any(store.decision(aid) is None for aid in (p.get("outbound") or {}).values()):
            continue
        out.append(rec.run_id)
    return out


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pause_interrupted_runs()
    for run_id in answered_runs():  # the founder decided before the restart: carry on from there
        _start_in_background(run_id)
    yield


app = FastAPI(title="Agentic Venture Builder", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CHOICE_LABELS = {"continue": "Continue to the next phase", "request_changes": "Request changes",
                 "pivot": "Pivot", "stop": "Stop the run", "approve": "Approve", "revise": "Revise (add notes)",
                 "retry": "Try again", "extend": "Allow more time and model calls"}
FEASIBILITY_LABELS = {"continue": "Continue to the roadmap", "revise": "Revise and check again", "stop": "Stop the run"}

# Plain words for the names the state machine uses, so the pages read as sentences.
STATE_LABELS = {"SCREEN_BRIEF": "Before Phase 1: scope screen", "FEASIBILITY": "Before Phase 1: feasibility check",
                "ROADMAP": "Before Phase 1: milestone roadmap", "DONE": "Finished", "STOPPED": "Stopped"}
STATE_LABELS.update({st.value: f"Phase {n}: {gates.PHASE_NAMES[n]}" for st, n in orch.PHASE_OF.items()})
STAGE_LABELS = {"PLAN": "the planner is writing the pass criteria", "PLAN_REVIEW": "code is checking the plan",
                "MAKE": "the agents are producing the files", "VERIFY": "the files are being checked against the plan",
                "SIGN_OFF": "the gate is signing off", "MILESTONE_REVIEW": "milestone review"}
STATUS_LABELS = {"running": "Working", "awaiting_approval": "Waiting for your decision", "blocked": "Paused",
                 "stopped": "Stopped", "done": "Finished"}
MILESTONE_LABELS = {"awaiting founder": "waiting for you", "in progress": "in progress", "passed": "passed",
                    "stopped": "stopped", "pending": "not started"}
OUTCOME_LABELS = {"go": "go", "repeat": "repeat", "pivot": "pivot", "stop": "stop", "done": "done",
                  "approved": "approved", "revise": "revise", "verified": "verified", "defects": "defects found"}


def _file_labels() -> dict[str, str]:
    """Output file name -> what the file is, in words."""
    return {row["output"]: row["label"] for row in orch.catalog()}


def _phase_of_file(name: str) -> int:
    if name.startswith("payment_test_log"):
        return 5
    digits = name[len("phase"):].split("_", 1)[0] if name.startswith("phase") else ""
    return int(digits) if digits.isdigit() else 0


def _situation(rec: orch.RunRecord) -> dict:
    """One sentence saying what the run is doing now and whether the founder has to act."""
    where = STATE_LABELS.get(rec.state, rec.state)
    if rec.status == "running":
        doing = STAGE_LABELS.get(rec.stage or "", "the agents are working")
        return {"tone": "work", "headline": f"{where}: {doing}.",
                "detail": "This page refreshes by itself every few seconds. Nothing is needed from you yet."}
    if rec.status == "awaiting_approval":
        return {"tone": "act", "headline": f"{where}: waiting for your decision.",
                "detail": "The run is paused until you decide. Your choices are in the box below."}
    if rec.status == "blocked":
        return {"tone": "warn", "headline": f"{where}: paused.", "detail": rec.blocked_reason or "Press Resume to continue."}
    if rec.status == "done":
        return {"tone": "ok", "headline": "The run is finished.", "detail": "Every milestone was reviewed. The files are listed below."}
    last = rec.history[-1]["state"] if rec.history else rec.state
    return {"tone": "stop", "headline": f"The run stopped at {STATE_LABELS.get(last, last).lower()}.",
            "detail": "The reasons are listed below. A stop is a result of the run, not an error."}


def _grouped_outputs(outputs: list[str]) -> list[dict]:
    labels = _file_labels()
    groups: dict[int, list[dict]] = {}
    for name in outputs:
        groups.setdefault(_phase_of_file(name), []).append({"name": name, "label": labels.get(name, "")})
    return [{"phase": n, "title": f"Phase {n}: {gates.PHASE_NAMES[n]}" if n in gates.PHASE_NAMES else "Other files",
             "files": files} for n, files in sorted(groups.items())]


def _brief_ideas() -> list[dict]:
    out = []
    for path in sorted(settings_mod.BRIEFS_DIR.glob("*.json")):
        try:
            idea = str(json.loads(path.read_text(encoding="utf-8")).get("idea", ""))
        except (ValueError, AttributeError):
            idea = ""
        out.append({"name": path.name, "idea": idea})
    return out


# Requirement R-MOCK-LABEL: the modules whose MOCKED flag is true are collected here, and the home
# page lists them by name in a banner that starts with the word MOCKED.
# Verified by: tests/test_app.py::test_home_page_lists_steps_and_mocks
MOCKED_MODULES = {m.__name__: m for m in (payments, delivery, refunds, reconcile, launcher, demand, operator)
                  if getattr(m, "MOCKED", False)}


def _settings() -> settings_mod.Settings:
    return settings_mod.load()


def _steps_view() -> list[dict]:
    return orch.catalog()


def _start_in_background(run_id: str) -> None:
    def work() -> None:
        try:
            orch.advance(run_id)
        except Exception as exc:  # keep the UI alive; the reason shows on the run page
            rec = orch.load(run_id)
            rec.status, rec.blocked_reason = orch.RunStatus.BLOCKED.value, f"error: {type(exc).__name__}: {exc}"
            orch.save(rec)

    threading.Thread(target=work, daemon=True).start()


def _ctx(request: Request, **kw) -> dict:
    s = _settings()
    return {"request": request, "llm_mocked": s.mock, "mocked_modules": sorted(MOCKED_MODULES),
            "state_labels": STATE_LABELS, "status_labels": STATUS_LABELS, **kw}


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = ""):
    s = _settings()
    health = LLMClient(s).health()
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, health=health, settings=s, steps=_steps_view(), runs=orch.list_runs()[:20], briefs=_brief_ideas(),
        msg=msg, limits=CUSTOM_LIMITS))


@app.post("/runs")
def start_run(brief_name: str = Form(...)):
    path = (settings_mod.BRIEFS_DIR / brief_name).resolve()
    if path.parent != settings_mod.BRIEFS_DIR.resolve() or not path.exists():
        raise HTTPException(404, "brief not found")
    try:
        rec = orch.new_run(orch.load_brief(path), _settings())
    except BriefError as exc:
        raise HTTPException(400, str(exc)) from exc
    _start_in_background(rec.run_id)
    return RedirectResponse(f"/runs/{rec.run_id}", status_code=303)


# A brief typed into the form on the home page. It becomes the same six keys a brief file has and
# then takes the same path: validate_brief (shape, secrets removed), then the brief screen.
CUSTOM_LIMITS = {"idea": 600, "target_market": 300, "item": 80, "items": 12}


def _items(text: str) -> list[str]:
    """A comma, semicolon or line separated field as a list of short strings."""
    parts = [x.strip() for x in re.split(r"[,;\n]", text) if x.strip()]
    return [x[:CUSTOM_LIMITS["item"]] for x in parts][:CUSTOM_LIMITS["items"]]


def _amount(text: str, name: str) -> float:
    try:
        value = float(text.strip().replace(",", "") or "0")
    except ValueError as exc:
        raise BriefError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise BriefError(f"{name} must be zero or more")
    return value


@app.post("/runs/custom")
def start_custom_run(idea: str = Form(""), target_market: str = Form(""), skills: str = Form(""),
                     budget_usd: str = Form("0"), hours_per_week: str = Form("0"), values: str = Form(""),
                     timeframe_weeks: str = Form(""), delegated_hours_per_week: str = Form("")):
    """Start a run from an idea typed by the founder instead of a brief file."""
    try:
        brief = {"idea": " ".join(idea.split())[:CUSTOM_LIMITS["idea"]],
                 "target_market": " ".join(target_market.split())[:CUSTOM_LIMITS["target_market"]],
                 "skills": _items(skills), "budget_usd": _amount(budget_usd, "budget"),
                 "hours_per_week": _amount(hours_per_week, "hours per week"), "values": _items(values)}
        if timeframe_weeks.strip():  # optional: left blank, the feasibility screen asks and 4 weeks are assumed
            brief["timeframe_weeks"] = _amount(timeframe_weeks, "time frame")
        if delegated_hours_per_week.strip():
            brief["delegated_hours_per_week"] = _amount(delegated_hours_per_week, "delegated hours")
        rec = orch.new_run(brief, _settings())
    except BriefError as exc:
        return RedirectResponse(f"/?msg={quote('The idea was not started: ' + str(exc))}#own-idea", status_code=303)
    _start_in_background(rec.run_id)
    return RedirectResponse(f"/runs/{rec.run_id}", status_code=303)


def _store(run_id: str) -> ApprovalStore:
    d = orch.run_dir(run_id)
    return ApprovalStore(d / "approvals.json", DecisionLog(d / "decision_log.json"))


def _load(run_id: str) -> orch.RunRecord:
    try:
        return orch.load(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "run not found") from exc


def _milestone_review(rec: orch.RunRecord, outputs: list[str]) -> dict | None:
    """What the founder is shown at a milestone: the phase's files, the verification report,
    the gate's reasons, the cost so far and the next milestone."""
    if not (rec.pending and rec.pending.get("milestone")):
        return None
    state = orch.State(rec.state)
    n = orch.PHASE_OF[state]
    names = [orch.plan_review_file(n)] + [s.output for s in orch.STEPS[state]]
    names += ([orch.TESTS_FILE] if state is orch.State.P3_BUILD else []) + [orch.decision_file(n)]
    signoff = rec.phase_data.get("signoff", {})
    criteria = {str(c.get("id")): c for c in rec.phase_data.get("criteria") or [] if isinstance(c, dict)}
    rows = [dict(r, check=criteria.get(str(r.get("id")), {}).get("check", ""),
                 threshold=criteria.get(str(r.get("id")), {}).get("threshold", ""))
            for r in rec.phase_data.get("verification", [])]
    return {
        "phase": n,
        "name": gates.PHASE_NAMES[n],
        "files": [f for f in names if f in outputs],
        "drafts": [f for f in names if guardrails.needs_draft_header(f)],
        "missing": [f for f in names if f not in outputs],
        "reasons": [plain.plain_reason(r) for r in signoff.get("reasons", [])],
        "headline": plain.headline(str(rec.pending.get("recommendation", "")), signoff.get("reasons", [])),
        "can_continue": "continue" in rec.pending.get("choices", []),
        "rows": rows,
        "next": next((m for m in rec.roadmap if m.get("phase") == n + 1), None),
    }


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: str, msg: str = ""):
    rec = _load(run_id)
    d = orch.run_dir(run_id)
    outputs = sorted(p.name for p in (d / "outputs").glob("*")) if (d / "outputs").exists() else []
    approvals = ApprovalStore(d / "approvals.json").all()
    log = _log(run_id)
    used_mocks = sorted({str(e.get("agent", "")) for e in log if str(e.get("action", "")).startswith("mocked_")})
    return templates.TemplateResponse(request, "run.html", _ctx(
        request, rec=rec, outputs=outputs, approvals=approvals, log=log[-40:], used_mocks=used_mocks,
        run_mocked=rec.mode == "mock", msg=msg, milestones=orch.milestones(rec), max_back_edges=gates.MAX_BACK_EDGES,
        choice_labels=CHOICE_LABELS, review=_milestone_review(rec, outputs), situation=_situation(rec),
        cc=command.view(rec, log, orch.milestones(rec), _milestone_review(rec, outputs)),
        groups=_grouped_outputs(outputs), file_labels=_file_labels(), stage_labels=STAGE_LABELS,
        feasibility_labels=FEASIBILITY_LABELS, carried=plain.carried([x for x in orch.carried_lines(rec).split("\n\n") if x]),
        plain_files=plain.file_label, warning_words=plain.WARNING_WORDS, step_titles=plain.STEP_TITLES,
        signature=_signature(run_id), progress=_progress(rec), step_cap=command.STEP_FRACTION_CAP,
        roadmap_rows=plain.roadmap_rows((rec.pending or {}).get("milestones") or [],
                                        {orch.PHASE_OF[st]: [s.output for s in orch.STEPS[st]] for st in orch.PHASE_STATES}),
        milestone_labels=MILESTONE_LABELS, outcome_labels=OUTCOME_LABELS, rebuttal_limit=REBUTTAL_LIMIT))


def _signature(run_id: str) -> str:
    """Changes whenever the run moves: its status, where it is, how many steps it took and what is decided."""
    rec = _load(run_id)
    decided = sum(1 for a in ApprovalStore(orch.run_dir(run_id) / "approvals.json").all().values() if a.get("decision"))
    parts = (rec.status, rec.state, rec.stage, len(rec.history), (rec.pending or {}).get("action_id", ""),
             decided, len(rec.artifacts), rec.blocked_reason)
    return hashlib.sha256("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()[:16]


@app.get("/runs/{run_id}/state")
def run_state(run_id: str):
    """What the run page asks every two seconds, to reload itself when the run has moved. Read-only."""
    return {"signature": _signature(run_id)}


def _progress(rec: orch.RunRecord) -> dict:
    """The decision log and the outputs folder of a run, as the "Working now" panel shows them."""
    d = orch.run_dir(rec.run_id)
    running = rec.status == orch.RunStatus.RUNNING.value
    try:
        files = sorted(p.name for p in (d / "outputs").glob("*")) if (d / "outputs").exists() else []
    except OSError:
        files = []
    try:
        phase_files = [s.output for s in orch.STEPS[orch.State(rec.state)]]
    except (KeyError, ValueError):
        phase_files = None  # before the first phase or after the run ended: no step count
    try:
        history = command.history_durations(settings_mod.RUNS_DIR) if running else None
    except Exception:  # the estimate is optional; the panel works without it
        history = None
    return command.progress(_log(rec.run_id), files, running, phase_files, history)


_last_log: dict[str, list] = {}  # the last readable decision log per run, for the moment a log is half-written


def _log(run_id: str) -> list[dict]:
    """The run's decision log for display. A log that is missing or caught half-written never breaks a page:
    what was last read is shown instead, or nothing."""
    try:
        log = DecisionLog(orch.run_dir(run_id) / "decision_log.json").entries()
        if not isinstance(log, list):
            raise ValueError("the log is not a list")
    except (OSError, ValueError):
        return _last_log.get(run_id, [])
    log = [e for e in log if isinstance(e, dict)]
    _last_log[run_id] = log
    while len(_last_log) > 32:
        _last_log.pop(next(iter(_last_log)))
    return log


@app.get("/runs/{run_id}/progress")
def run_progress(run_id: str):
    """What the agents did most recently, read from the run's decision log and outputs folder. Read-only.
    The run page asks for it every two seconds while the agents work, so the founder sees the work move."""
    return _progress(_load(run_id))


def _back(run_id: str, msg: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/runs/{run_id}?msg={quote(msg[:400])}{anchor}", status_code=303)


@app.post("/runs/{run_id}/rebuttal")
def rebuttal(run_id: str, action_id: str = Form(...), code: str = Form(...), text: str = Form("")):
    """The founder disagrees with one feasibility warning. The backend screens the text, asks the model once and
    may lower, remove or keep the warning; a refusal comes back as a plain message on the run page."""
    _load(run_id)
    text = " ".join(text.split())[:REBUTTAL_LIMIT]
    if not text:
        return _back(run_id, "The rebuttal was not sent: write why you disagree with the warning.")
    rebut = getattr(orch, "rebut_warning", None)
    if not callable(rebut):
        return _back(run_id, "The rebuttal was not sent: rebuttals are not available in this version.")
    try:
        res = rebut(run_id, action_id, code, text)
    except (KeyError, ValueError) as exc:
        return _back(run_id, f"The rebuttal was not sent: {exc}")
    return _back(run_id, f"Rebuttal answered: {command.rebuttal_result(res if isinstance(res, dict) else {})}. "
                         "The system may keep a warning; the decision stays yours.")


def _count(text: str, name: str) -> int | None:
    if not text.strip():
        return None
    value = _amount(text, name)
    if value != int(value):
        raise BriefError(f"{name} must be a whole number")
    return int(value)


@app.post("/runs/{run_id}/evidence")
def evidence(run_id: str, kind: str = Form(...), url: str = Form(""), statement: str = Form(""),
             visitors: str = Form(""), signups: str = Form(""), days: str = Form("")):
    """Evidence the founder adds: a page, a measured result, or a statement without a link. The backend fetches
    and checks a page itself; a statement is recorded as an ASSUMPTION and never counts as a source."""
    _load(run_id)
    if kind not in command.EVIDENCE_KINDS:
        raise HTTPException(400, "the kind of evidence must be problem, pay, demand or traction")
    add = getattr(orch, "add_evidence", None)
    if not callable(add):
        return _back(run_id, "The evidence was not added: adding evidence is not available in this version.", "#evidence")
    try:
        item = add(run_id, kind, url=url.strip()[:EVIDENCE_LIMITS["url"]],
                   statement=" ".join(statement.split())[:EVIDENCE_LIMITS["statement"]],
                   visitors=_count(visitors, "visitors"), signups=_count(signups, "sign-ups"), days=_count(days, "days"))
    except (KeyError, ValueError) as exc:  # BriefError is a ValueError
        return _back(run_id, f"The evidence was not added: {exc}", "#evidence")
    status = command.EVIDENCE_STATUS_WORDS.get(str((item or {}).get("status", "")), "recorded")
    return _back(run_id, f"Evidence added: {status}. Choose Request changes so this phase is checked again with it.", "#evidence")


REBUTTAL_LIMIT = 600
EVIDENCE_LIMITS = {"url": 500, "statement": 600}
CHANGES_NEEDED = "Write what should change before you send Request changes. Nothing was recorded."


@app.get("/runs/{run_id}/log", response_class=HTMLResponse)
def log_view(request: Request, run_id: str):
    """Everything the system and the founder did in this run, in order. Read-only."""
    rec = _load(run_id)
    log = DecisionLog(orch.run_dir(run_id) / "decision_log.json").entries()
    return templates.TemplateResponse(request, "log.html", _ctx(request, rec=rec, rows=command.log_rows(log)))


@app.post("/runs/{run_id}/approvals")
def decide(run_id: str, action_id: str = Form(...), decision: str = Form(...)):
    """Approve or reject one outbound action (publish, send, payment test)."""
    _load(run_id)
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    try:
        _store(run_id).decide(action_id, decision == "approve")
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _start_in_background(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


def _changed_inputs(brief: dict, idea: str, skills: str, budget_usd: str, hours_per_week: str,
                    timeframe_weeks: str = "", delegated_hours_per_week: str = "") -> dict:
    """The fields the founder changed on the feasibility screen; an empty or unchanged field is left out."""
    changes: dict = {}
    idea = " ".join(idea.split())[:CUSTOM_LIMITS["idea"]]
    if idea and idea != brief["idea"]:
        changes["idea"] = idea
    if _items(skills) and _items(skills) != list(brief["skills"]):
        changes["skills"] = _items(skills)
    for key, text, name in (("budget_usd", budget_usd, "budget"), ("hours_per_week", hours_per_week, "hours per week")):
        if text.strip() and _amount(text, name) != float(brief[key]):
            changes[key] = _amount(text, name)
    for key, text, name in (("timeframe_weeks", timeframe_weeks, "time frame"),
                            ("delegated_hours_per_week", delegated_hours_per_week, "delegated hours")):
        if text.strip() and _amount(text, name) != brief.get(key):
            changes[key] = _amount(text, name)
    return changes


@app.post("/runs/{run_id}/choice")
def choose(run_id: str, action_id: str = Form(...), choice: str = Form(...), note: str = Form(""),
           idea: str = Form(""), skills: str = Form(""), budget_usd: str = Form(""), hours_per_week: str = Form(""),
           timeframe_weeks: str = Form(""), delegated_hours_per_week: str = Form("")):
    """A founder decision: feasibility, roadmap or milestone review. Only offered choices are accepted.
    On the feasibility screen a "revise" carries the inputs the founder changed."""
    rec = _load(run_id)
    if choice == "request_changes" and not note.strip():
        raise HTTPException(400, CHANGES_NEEDED)
    try:
        if choice == "revise" and (rec.pending or {}).get("gate") == "feasibility":
            orch.revise_inputs(run_id, action_id, _changed_inputs(rec.brief, idea, skills, budget_usd, hours_per_week,
                                                                  timeframe_weeks, delegated_hours_per_week))
        _store(run_id).choose(action_id, choice, note=note.strip())
    except (KeyError, ValueError) as exc:  # BriefError is a ValueError: a bad revised value changes nothing
        raise HTTPException(400, str(exc)) from exc
    _start_in_background(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/recheck")
def recheck(run_id: str, action_id: str = Form(...)):
    """Check the phase again with the evidence the founder added. Nothing is regenerated: the files are
    kept and only the gate runs again. Offered only once the founder has added evidence."""
    rec = _load(run_id)
    if not command.founder_evidence(rec):
        raise HTTPException(400, "Add your evidence first. Nothing was recorded.")
    try:
        _store(run_id).choose(action_id, "request_changes", note="")
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _start_in_background(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/stop")
def stop(run_id: str):
    """The founder's stop button: always available; a running run stops before its next step."""
    _load(run_id)
    orch.request_stop(run_id)
    return RedirectResponse(f"/runs/{run_id}?msg={quote('stop requested; the run ends before its next step')}",
                            status_code=303)


@app.post("/runs/{run_id}/resume")
def resume(run_id: str):
    _load(run_id)
    _start_in_background(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/instruct")
def instruct(run_id: str, text: str = Form(...)):
    _load(run_id)
    res = orch.instruct(run_id, text)
    msg = res.reason if res.refused else "instruction accepted; agents will see it in their next step"
    return RedirectResponse(f"/runs/{run_id}?msg={quote(msg)}", status_code=303)


@app.get("/runs/{run_id}/outputs/{name}", response_class=HTMLResponse)
def output_view(request: Request, run_id: str, name: str):
    _load(run_id)
    p = (orch.run_dir(run_id) / "outputs" / name).resolve()
    if p.parent != (orch.run_dir(run_id) / "outputs").resolve() or not p.exists():
        raise HTTPException(404, "output not found")
    rec = orch.load(run_id)
    text = p.read_text(encoding="utf-8")
    kind = {".md": "markdown", ".json": "json", ".html": "page"}.get(p.suffix, "text")
    body = orch.strip_header(text)  # the header written by code goes into the technical details, not the page
    rendered = {"markdown": render.markdown_to_html, "json": render.json_to_html}.get(kind, lambda _t: "")(body)
    scoring = None
    if name.startswith("phase1_step2_scoring"):
        scoring = plain.scoring_view(text, rec.artifacts.get("phase1_step1", {}).get("data", {}).get("opportunities", []))
    details = {"File": name, "Written by": orch.file_agents().get(name, "code"), "Run started": rec.created_at}
    first = text.split("\n", 1)[0]
    if orch.has_header(text) and "| Model: " in first:
        details["Model"] = first.split("| Model: ", 1)[1].split(" | ", 1)[0].replace(" -->", "")
    elif kind == "json" and scoring is not None:
        details["Model"] = str((json.loads(text).get(orch.HEADER_KEY) or {}).get("model", rec.model))
    decision = next((l for l in body.split("\n")[:3] if l.startswith("Decision: ")), "")
    return templates.TemplateResponse(request, "output.html", _ctx(
        request, rec=rec, name=name, text=text, run_mocked=rec.mode == "mock", kind=kind, rendered=rendered,
        label=plain.file_label(name), file_mocked="MOCKED" in body[:400], scoring=scoring, details=details,
        headline=plain.plain_reason(decision.replace("Decision: ", "Decision: ", 1)) if decision else ""))

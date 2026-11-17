"""Web UI: start runs, watch them, approve or reject founder gates, send screened instructions.

Server-rendered HTML; no front-end build. Run it with `docker compose up` or
`uvicorn app.main:app --reload` from the repository root.
"""

from __future__ import annotations

from urllib.parse import quote
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import delivery
import orchestrator as orch
import payments
import reconcile
import refunds
import settings as settings_mod
from agents import launcher
from approval import ApprovalStore
from decision_log import DecisionLog
from guardrails import BriefError
from llm import LLMClient

app = FastAPI(title="Agentic Venture Builder")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MOCKED_MODULES = {m.__name__: m for m in (payments, delivery, refunds, reconcile, launcher) if getattr(m, "MOCKED", False)}


def _settings() -> settings_mod.Settings:
    return settings_mod.load()


def _steps_view() -> list[dict]:
    rows = [{"phase": 1, "key": "phase1_gate", "label": "Founder brief screen (guardrails)",
             "agent": "guardrails", "output": orch.GATE_FILES[orch.State.SCREEN_BRIEF], "status": "Implemented",
             "mocked": ""}]
    for st in orch.STEPS:
        for step in orch.STEPS[st] + orch.AFTER_APPROVAL.get(st, []):
            rows.append({"phase": step.phase, "key": step.key, "label": step.label, "agent": step.agent,
                         "output": step.output, "status": orch.step_status(step), "mocked": step.mocked})
    return rows


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
    return {"request": request, "llm_mocked": s.mock, "mocked_modules": sorted(MOCKED_MODULES), **kw}


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    s = _settings()
    health = LLMClient(s).health()
    briefs = sorted(p.name for p in settings_mod.BRIEFS_DIR.glob("*.json"))
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, health=health, settings=s, steps=_steps_view(), runs=orch.list_runs()[:20], briefs=briefs))


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


def _load(run_id: str) -> orch.RunRecord:
    try:
        return orch.load(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "run not found") from exc


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: str, msg: str = ""):
    rec = _load(run_id)
    d = orch.run_dir(run_id)
    outputs = sorted(p.name for p in (d / "outputs").glob("*")) if (d / "outputs").exists() else []
    approvals = ApprovalStore(d / "approvals.json").all()
    log = DecisionLog(d / "decision_log.json").entries()
    used_mocks = sorted({e["agent"] for e in log if e["action"].startswith("mocked_")})
    return templates.TemplateResponse(request, "run.html", _ctx(
        request, rec=rec, outputs=outputs, approvals=approvals, log=log[-40:], used_mocks=used_mocks,
        run_mocked=rec.mode == "mock", msg=msg))


@app.post("/runs/{run_id}/approvals")
def decide(run_id: str, action_id: str = Form(...), decision: str = Form(...), on_reject: str = Form("stop")):
    _load(run_id)
    d = orch.run_dir(run_id)
    store = ApprovalStore(d / "approvals.json", DecisionLog(d / "decision_log.json"))
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    try:
        store.decide(action_id, decision == "approve", on_reject=on_reject if on_reject in ("repeat", "stop") else "stop")
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _start_in_background(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


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
    return templates.TemplateResponse(request, "output.html", _ctx(
        request, rec=rec, name=name, text=p.read_text(encoding="utf-8"), run_mocked=rec.mode == "mock",
        file_mocked="MOCKED" in p.read_text(encoding="utf-8")[:400]))

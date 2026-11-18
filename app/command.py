"""The command center view of a run.

Everything shown here is read from the run record and the run's decision log. A light, a counter,
an agent line or a feed line exists only when the record or the log holds something behind it;
with an empty log the agents and activity panels are empty. Nothing in this module changes a run.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import gates
import orchestrator as orch
from app import plain

RUN_STATES = {"running": ("RUNNING", "live"), "awaiting_approval": ("AWAITING YOUR COMMAND", "wait"),
              "blocked": ("PAUSED", "wait"), "stopped": ("STOPPED", "bad"), "done": ("COMPLETE", "ok")}
AGENT_NAMES = {"guardrails": "Safeguards", "assessor": "Assessor", "planner": "Planner", "researcher": "Researcher",
               "scorer": "Scorer", "interviewer": "Interviewer", "checker": "Checker", "strategist": "Strategist",
               "builder": "Builder", "commercial": "Commercial", "launcher": "Launcher", "delivery": "Delivery (MOCKED)",
               "operator": "Operator", "gate": "Gate (code)", "sources": "Sources (code)", "orchestrator": "Run control",
               "founder": "You"}
# What a log action means, in a sentence. An action that is not listed is shown with its own words.
ACTION_WORDS = {
    "screen_brief": "checked that the idea is in scope", "brief_screen_gate": "scope check finished",
    "assess": "read the brief and estimated skills, cost and effort", "feasibility_check": "listed the feasibility warnings",
    "roadmap": "proposed the six-step plan", "roadmap_check": "checked the plan's shape", "plan": "wrote the pass criteria",
    "find_opportunities": "searched for people describing the problem", "score": "scored the opportunities",
    "market_research": "looked for comparable products and prices", "interview_guide": "wrote the customer interview guide",
    "verify": "checked the work against the plan", "lean_canvas": "drafted the one-page business plan",
    "landing_page": "drafted the landing page", "mocked_demand_test": "set the sign-up target (results MOCKED)",
    "demand_test": "set the sign-up target (results MOCKED)", "mvp_spec": "specified the first version",
    "build_product": "wrote the product draft", "user_docs": "wrote the user guide", "pricing": "suggested a price",
    "policies": "drafted the policies (not legal advice)", "store_page": "drafted the store page",
    "launch_posts": "drafted the launch posts", "support_faq": "drafted the support FAQ",
    "pivot_memo": "wrote the carry on or change direction memo", "pay_tag_downgraded": "a page without a price was not counted as payment evidence",
    "reference_page_not_evidence": "a reference page was kept as background only", "competitor_dropped": "a competitor entry failed its check and was dropped",
    "run_budget": "the run budget was reached", "stop_run": "pressed stop", "screen_request": "screened an instruction",
}
OUTCOME_WORDS = {"go": "recommended go", "repeat": "asked for another try", "pivot": "recommended a change of direction",
                 "stop": "recommended stop", "approved": "approved", "revise": "sent back for revision", "verified": "found no defect",
                 "defects": "found defects", "awaiting_founder": "is waiting for you", "pass": "passed", "blocked": "was blocked",
                 "refused": "was refused", "allowed": "was allowed", "gate_criteria_only": "went on with the gate's own checks only",
                 "waiver": "signed a waiver", "continue": "chose to continue", "approve": "approved", "request_changes": "asked for changes",
                 "extend": "allowed more time and model calls", "retry": "asked for another try"}
COUNTS_AS = {"SAY": "Describes the problem", "PROBLEM": "Describes the problem", "PAY": "Shows a price",
             "MARKET": "About the market", "OTHER": "Background only"}


def site(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def clock(timestamp: str) -> str:
    return str(timestamp)[11:19]


def run_state(rec: orch.RunRecord) -> dict:
    word, light = RUN_STATES.get(rec.status, (rec.status.upper(), ""))
    return {"word": word, "light": light}


def telemetry(rec: orch.RunRecord) -> dict:
    u = rec.usage or {}
    seconds = int(u.get("seconds", 0) or 0)
    return {"model": rec.model, "calls": f"{int(u.get('calls', 0) or 0):,}", "tokens": f"{int(u.get('total_tokens', 0) or 0):,}",
            "cost": f"${float(u.get('cost_usd', 0) or 0):,.0f}" if not float(u.get("cost_usd", 0) or 0) % 1 else f"${float(u.get('cost_usd', 0)):,.2f}",
            "elapsed": f"{seconds // 60:02d}:{seconds % 60:02d}"}


def sentence(entry: dict) -> str:
    """One log entry as a plain sentence."""
    agent, action, outcome = entry.get("agent", ""), str(entry.get("action", "")), entry.get("outcome")
    who = AGENT_NAMES.get(agent, agent.capitalize())
    if agent == "founder":
        if action.startswith("waiver_"):
            return f"You signed a waiver at milestone {action.rsplit('m', 1)[-1]}"
        return "You " + OUTCOME_WORDS.get(str(outcome), str(outcome or "decided")).replace("is waiting for you", "decided")
    if action.startswith("check_output:"):
        return f"Safeguards checked {plain.file_label(action.split(':', 1)[1])}: {OUTCOME_WORDS.get(str(outcome), str(outcome))}"
    if action.startswith("milestone_review_m"):
        return f"Milestone {action[-1]} is ready for your review"
    if action.endswith("_plan_review"):
        return f"The plan for step {action[5]} {OUTCOME_WORDS.get(str(outcome), 'was checked')}"
    if action.endswith("_verification"):
        return f"The checker {OUTCOME_WORDS.get(str(outcome), 'finished')} in step {action[5]}"
    if action.endswith("_gate") and agent == "gate":
        return f"The gate {OUTCOME_WORDS.get(str(outcome), 'finished')}"
    if action.startswith("mocked_"):
        base = action[len("mocked_"):].split(":")[0].replace("_", " ")
        return f"{who}: {base} (MOCKED, nothing left this machine)"
    words = ACTION_WORDS.get(action.split(":")[0])
    if words:
        return f"{who} {words}" if agent not in ("sources", "orchestrator") else words[:1].upper() + words[1:]
    return f"{who}: {action.replace('_', ' ')}" + (f" ({OUTCOME_WORDS.get(str(outcome), outcome)})" if outcome else "")


def activity(log: list[dict], limit: int = 12) -> list[dict]:
    """The latest log entries, newest first. Each line is one log entry."""
    return [{"time": clock(e.get("timestamp", "")), "text": sentence(e), "you": e.get("agent") == "founder"}
            for e in reversed(log[-limit:])]


def agents(rec: orch.RunRecord, log: list[dict]) -> list[dict]:
    """Every agent that has an entry in the log, with its last action, and the founder when a decision is open."""
    last: dict[str, dict] = {}
    for e in log:
        if e.get("agent") not in ("founder", "gate", "sources", "orchestrator", "guardrails"):
            last[e["agent"]] = e
    out = [{"name": AGENT_NAMES.get(a, a.capitalize()), "did": sentence(e).split(" ", 1)[-1].capitalize() if AGENT_NAMES.get(a) else sentence(e),
            "state": "Done", "light": "ok"} for a, e in last.items()]
    if rec.status == "running" and out:
        out[-1].update(state="Latest", light="live")
    if rec.pending and rec.status == "awaiting_approval":
        out.append({"name": "You", "did": "Decision needed", "state": "Waiting", "light": "wait"})
    return out


def timeline(rec: orch.RunRecord, milestones: list[dict]) -> list[dict]:
    """Setup steps and the six milestones, each with a light and a word, from the run record."""
    seen = {(h["state"], h["outcome"]) for h in rec.history}
    meta = rec.artifacts.get("feasibility_meta") or {}
    waived = {w["milestone"] for w in (rec.artifacts.get("waivers") or {}).get("items", [])}
    stopped_at = rec.history[-1]["state"] if rec.state == "STOPPED" and rec.history else ""

    def setup(state: str, title: str, done_word: str) -> dict:
        if rec.state == state:
            word, light = (("AWAITING YOUR COMMAND", "wait") if rec.status == "awaiting_approval" else
                           ("PAUSED", "wait") if rec.status == "blocked" else ("IN PROGRESS", "live"))
        elif stopped_at == state:
            word, light = "STOPPED", "bad"
        elif (state, "go") in seen:
            word, light = done_word, "ok"
        else:
            word, light = "PENDING", ""
        return {"group": "Setup", "title": title, "word": word, "light": light, "now": rec.state == state}

    majors = int(meta.get("majors", 0) or 0)
    feas_word = (f"{majors} WARNING{'' if majors == 1 else 'S'}, YOU CONTINUED" if meta.get("continued") and majors else "YOU CONTINUED")
    rows = [setup("SCREEN_BRIEF", "Scope check", "CLEARED"), setup("FEASIBILITY", "Feasibility", feas_word),
            setup("ROADMAP", "Plan", "APPROVED BY YOU")]
    words = {"passed": ("PASSED", "ok"), "stopped": ("STOPPED", "bad"), "awaiting founder": ("AWAITING YOUR COMMAND", "wait"),
             "in progress": ("IN PROGRESS", "live"), "pending": ("PENDING", "")}
    for m in milestones:
        word, light = words.get(m["status"], (str(m["status"]).upper(), ""))
        if m["phase"] in waived and m["status"] == "passed":
            word, light = "WAIVED BY YOU", "wait"
        rows.append({"group": "Milestones", "title": f"{m['phase']}. {plain.STEP_TITLES.get(m['phase'], m['name'])}", "word": word,
                     "light": light, "now": m["status"] in ("awaiting founder", "in progress")})
    return rows


def _phase_claims(rec: orch.RunRecord) -> list[dict]:
    keys = [k for k in rec.artifacts if k.startswith("phase1_step")] + ["feasibility"]
    if rec.state.startswith("P") and rec.state[1:2].isdigit() and rec.state[1] != "1":
        keys = [k for k in rec.artifacts if k.startswith(f"phase{rec.state[1]}_step")] or keys
    out = []
    for k in keys:
        out += [c for c in rec.artifacts.get(k, {}).get("claims", []) if isinstance(c, dict)]
    return out


def evidence(rec: orch.RunRecord) -> dict:
    """The pages cited in this phase: site, what the page shows, and what it counts as, in words."""
    rows, seen = [], set()
    for c in _phase_claims(rec):
        if c.get("label") != "SOURCED" or not c.get("url") or (c["url"], c.get("text")) in seen:
            continue
        seen.add((c["url"], c.get("text")))
        rows.append({"site": site(c["url"]), "url": c["url"], "text": c.get("text", ""),
                     "counts": COUNTS_AS.get(str(c.get("kind", "")).upper(), "Background only")})
    unsourced = sum(1 for c in _phase_claims(rec) if c.get("label") != "SOURCED")
    return {"rows": rows, "sites": len({r["site"] for r in rows}), "unsourced": unsourced,
            "empty": "No evidence was collected: no fetched page supports a claim in this step." if not rows else ""}


def tiles(rec: orch.RunRecord) -> list[dict]:
    """Key figures for a step 1 review, counted from the same claims the gate counts."""
    if rec.state != "P1_DISCOVER":
        return []
    claims = _phase_claims(rec)
    sourced = [c for c in claims if c.get("label") == "SOURCED"]
    say = {c["url"] for c in sourced if str(c.get("kind", "")).upper() in ("SAY", "PROBLEM")}
    pay = {c["url"] for c in sourced if str(c.get("kind", "")).upper() == "PAY"}
    need = gates.PHASE1
    return [
        {"num": str(len(say)), "label": f"page{'' if len(say) == 1 else 's'} describing the problem, from {len({site(u) for u in say})} site{'' if len({site(u) for u in say}) == 1 else 's'}",
         "tone": "ok" if len(say) >= need.min_problem_sources else "bad"},
        {"num": str(len(pay)), "label": "pages showing a price for something similar", "tone": "ok" if len(pay) >= need.min_paying_sources else "bad"},
        {"num": f"{len(claims) - len(sourced)} of {len(claims)}" if claims else "not measured", "label": "claims we could not back with a source", "tone": ""},
    ]


def gate_checks(rows: list[dict]) -> dict:
    """The current gate's own checks (measured by code) with passed, failed or not measured."""
    words = {"pass": ("PASSED", "pass"), "fail": ("FAILED", "fail"), "unverified": ("NOT MEASURED", "note")}
    out = [{"text": (r.get("check") or r.get("id", "")).capitalize(), "word": words.get(r.get("result"), ("NOT MEASURED", "note"))[0],
            "badge": words.get(r.get("result"), ("", "note"))[1]} for r in rows if r.get("code")]
    for row, r in zip(out, [r for r in rows if r.get("code")]):
        # The pay check with nothing to measure is unknown, not a failure: no page with a price was found either way.
        if r.get("check") == PAY_CHECK and r.get("result") not in ("pass", "fail"):
            row.update(word="UNKNOWN", badge="note")
    counts = {w: sum(1 for r in out if r["word"] == w) for w in ("PASSED", "FAILED", "NOT MEASURED", "UNKNOWN")}
    summary = " · ".join(f"{n} {w.lower()}" for w, n in counts.items() if n)
    return {"rows": out, "summary": summary}


def view(rec: orch.RunRecord, log: list[dict], milestones: list[dict], review: dict[str, Any] | None) -> dict:
    return {"state": run_state(rec), "telemetry": telemetry(rec), "timeline": timeline(rec, milestones),
            "agents": agents(rec, log), "activity": activity(log), "evidence": evidence(rec),
            "tiles": tiles(rec) if review else [], "checks": gate_checks((review or {}).get("rows", [])),
            "steps_done": sum(1 for t in timeline(rec, milestones) if t["light"] == "ok"), "steps_total": 9,
            "pause": pause(rec), "summary": summary(rec, log), "rebuttals": rebuttals(rec),
            "founder_evidence": founder_evidence(rec), "wants_evidence": wants_evidence(rec)}


# ---------------------------------------------------------------- pauses, the run summary and the decision log

PAUSES = {
    "run_budget": ("The run reached its budget of time or model calls.",
                   "Nothing is wrong with the idea. The run has a limit on how long it may work without asking you.",
                   {"extend": "The run gets the same budget again and carries on from where it paused.",
                    "stop": "The run ends here. Everything produced so far stays available."}),
    "roadmap": ("The plan failed its checks twice.",
                "The planner's six-step plan did not pass the code checks, so no work has started.",
                {"retry": "The planner writes the plan again.", "stop": "The run ends here."}),
    "output": ("A file could not be written after two attempts.",
               "Either the model's reply was not usable twice, or the safeguards blocked the text twice. Blocked text is never saved or shown.",
               {"retry": "The step is tried again.", "stop": "The run ends here. Everything produced so far stays available."}),
}


def pause(rec: orch.RunRecord) -> dict | None:
    """What happened at a pause, what it means, and what each choice does."""
    p = rec.pending or {}
    if p.get("gate") != "pause":
        return None
    happened, means, choices = PAUSES.get(str(p.get("pause")), ("The run paused and needs your decision.", "", {}))
    return {"happened": happened, "means": means, "detail": str(p.get("summary", "")),
            "choices": [{"choice": c, "does": choices.get(c, "")} for c in p.get("choices", [])]}


def decisions(log: list[dict]) -> list[dict]:
    """Every founder decision in the log, oldest first: the timeline of the run summary."""
    out = []
    for e in log:
        if e.get("agent") != "founder":
            continue
        out.append({"time": clock(e.get("timestamp", "")), "text": sentence(e), "waiver": e.get("outcome") == "waiver",
                    "reason": e.get("reason", "") if e.get("outcome") == "waiver" else ""})
    return out


def summary(rec: orch.RunRecord, log: list[dict]) -> dict | None:
    """How the run ended, for a finished or stopped run."""
    if rec.status not in ("done", "stopped"):
        return None
    last = rec.history[-1] if rec.history else {}
    by_founder = any(e.get("agent") == "founder" and e.get("outcome") == "stop" for e in log[-3:])
    if rec.status == "done":
        ended = "The run is complete: every milestone was reviewed."
    elif last.get("state") == "SCREEN_BRIEF":
        ended = "The run stopped at the scope check."
    elif by_founder:
        ended = "You stopped the run."
    else:
        ended = "The run stopped."
    return {"ended": ended, "decisions": decisions(log), "waivers": sum(1 for d in decisions(log) if d["waiver"])}


def log_rows(log: list[dict]) -> list[dict]:
    """The decision log as a table: time, who, what, sources, model, cost, approved by."""
    return [{"time": str(e.get("timestamp", "")).replace("T", " ").replace("Z", ""), "who": AGENT_NAMES.get(e.get("agent", ""), e.get("agent", "")),
             "what": sentence(e), "action": e.get("action", ""), "sources": list(e.get("sources") or []),
             "model": e.get("model") or "none (code)", "cost": f"${float(e.get('cost', 0) or 0):,.4f}".rstrip("0").rstrip(".") if e.get("cost") else "$0",
             "approved": "you" if e.get("approved_by") == "founder" else "not needed", "mocked": str(e.get("action", "")).startswith("mocked_")}
            for e in log]


# ---------------------------------------------------------------- founder feedback: rebuttals, own evidence, live progress

PAY_CHECK = "someone already pays"  # the gate's own name for the check
EVIDENCE_KINDS = ("problem", "pay", "demand")
EVIDENCE_KIND_WORDS = {"problem": "Shows the problem is real", "pay": "Shows someone already pays", "demand": "Results you measured"}
EVIDENCE_STATUS_WORDS = {"fetched": "fetched and usable", "not fetched": "could not be fetched", "stated": "recorded as an assumption",
                         "founder-reported": "founder-reported"}


def rebuttal_result(item: dict) -> str:
    """What a rebuttal changed, in words: lowered, removed or kept."""
    before, after = str(item.get("before", "") or ""), str(item.get("after", "") or "")
    verdict = str(item.get("verdict", "") or "").lower()
    if "remov" in verdict or after == "none":
        return "Removed"
    if before and after and before != after and "kept" not in verdict and "keep" not in verdict:
        return f"Lowered from {before} to {after}"
    return "Kept"


def rebuttals(rec: orch.RunRecord) -> dict[str, dict]:
    """The founder's rebuttals of feasibility warnings by warning id, as the run record holds them."""
    raw = rec.artifacts.get("rebuttals") or (rec.artifacts.get("feasibility_meta") or {}).get("rebuttals") or {}
    if isinstance(raw, dict) and isinstance(raw.get("items"), (list, dict)):
        raw = raw["items"]
    items = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("code"):
            out[str(item["code"])] = {"result": rebuttal_result(item), "reason": str(item.get("reason", "") or ""),
                                      "words": plain.WARNING_WORDS.get(str(item["code"]), "a warning")}
    return out


def founder_evidence(rec: orch.RunRecord) -> list[dict]:
    """What the founder added as evidence, each with its status in plain words."""
    reader = getattr(orch, "founder_evidence", None)
    try:
        raw = reader(rec) if callable(reader) else rec.artifacts.get("founder_evidence", [])
    except Exception:  # a display helper never breaks the run page
        raw = []
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "")
        words = EVIDENCE_STATUS_WORDS.get(status, status or "recorded")
        if status == "not fetched" and item.get("reason"):
            words += f": {item['reason']}"
        if status == "fetched" and item.get("price"):
            words += f" (price found: {item['price']})"
        out.append({"kind": EVIDENCE_KIND_WORDS.get(str(item.get("kind", "")), "Evidence"), "url": str(item.get("url", "") or ""),
                    "title": str(item.get("title", "") or ""), "statement": str(item.get("statement", "") or ""), "status": words,
                    "assumption": status == "stated"})
    return out


def wants_evidence(rec: orch.RunRecord) -> bool:
    """True on the decision screens where the founder's own evidence can change the result:
    the feasibility screen and the reviews of steps 1 and 2."""
    p = rec.pending or {}
    if rec.status != orch.RunStatus.AWAITING_APPROVAL.value:
        return False
    if p.get("gate") == "feasibility":
        return True
    try:
        return bool(p.get("milestone")) and orch.PHASE_OF.get(orch.State(rec.state)) in (1, 2)
    except ValueError:
        return False


def _seconds_since(timestamp: str) -> int | None:
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))


# ---------------------------------------------------------------- how far along the step in flight is (an estimate)
#
# Nothing here asks the model or the run loop anything. The decision log records an action when it has
# finished, so the action in flight is the one that follows the last recorded one; how long it usually takes
# is measured from the gaps between log timestamps. Every figure below is a proxy and is worded as one.

DEFAULT_STEP_SECONDS = 20.0  # used when no run has timed this action yet
STEP_SECONDS_RANGE = (3.0, 600.0)  # a measured duration is kept inside this range
STEP_FRACTION_CAP = 0.95  # the bar never claims a step is finished before its log entry exists
HISTORY_RUNS = 8  # how many other runs are read for durations
HISTORY_SCAN = 40  # how many run folders are looked at to find them
HISTORY_TTL = 60.0  # seconds the durations of other runs are kept in memory

_CONTROL = ("gate", "founder", "orchestrator")  # their entries end a step or a decision
_PHASE_GATE = re.compile(r"phase(\d)_(plan_review|verification)$")
_MILESTONE = re.compile(r"decide:milestone_m(\d)")
_history: dict[str, tuple[float, dict[str, float]]] = {}


def _when(timestamp: Any) -> datetime | None:
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return then if then.tzinfo else then.replace(tzinfo=timezone.utc)


def _step_funcs() -> set[str]:
    steps = orch.all_steps() + list(orch.ROLES.values()) + [orch.SITE_STEP]
    return {s.func for s in steps}


def _base(action: str) -> str:
    """The action's own name without the marks the run loop adds to it."""
    for prefix in ("mocked_", "skipped_"):
        if action.startswith(prefix):
            action = action[len(prefix):]
    return action[:-len("_not_written")] if action.endswith("_not_written") else action


def _marks_time(entry: Any, funcs: set[str]) -> bool:
    """True for an entry that ends a step or a decision. Checks and notes written inside a step are not counted."""
    if not isinstance(entry, dict):
        return False
    agent = entry.get("agent")
    return agent in _CONTROL or (agent not in ("guardrails", "sources") and _base(str(entry.get("action", ""))) in funcs)


def _clamp(seconds: float) -> float:
    return min(STEP_SECONDS_RANGE[1], max(STEP_SECONDS_RANGE[0], float(seconds)))


def step_durations(log: list[dict]) -> dict[str, list[float]]:
    """Seconds each kind of agent action took in one decision log, by "agent:action".

    A duration is the gap between an action that used a model and the entry before it that ended a step or
    a decision. Actions that used no model are code and are not timed."""
    funcs, out, before = _step_funcs(), {}, None
    for e in log if isinstance(log, list) else []:
        if not _marks_time(e, funcs):
            continue
        at = _when(e.get("timestamp"))
        if at is None:
            continue
        action = str(e.get("action", ""))
        if before is not None and e.get("model") and e.get("agent") not in _CONTROL and action in funcs:
            gap = (at - before).total_seconds()
            if gap >= 0:
                out.setdefault(f"{e['agent']}:{action}", []).append(gap)
        before = at
    return out


def history_durations(runs_dir: Path | str, limit: int = HISTORY_RUNS) -> dict[str, float]:
    """The median seconds per kind of action over the most recent runs that are not working now.

    At most `limit` runs are read, and the answer is kept in memory for a minute, so asking every two
    seconds costs nothing. A folder that cannot be read is skipped."""
    key, now = str(runs_dir), time.monotonic()
    hit = _history.get(key)
    if hit and now - hit[0] < HISTORY_TTL:
        return hit[1]
    pooled: dict[str, list[float]] = {}
    try:
        names = sorted((p.name for p in Path(runs_dir).iterdir() if p.is_dir()), reverse=True)[:HISTORY_SCAN]
    except OSError:
        names = []
    read = 0
    for name in names:  # run ids start with their creation time, so this is newest first
        if read >= limit:
            break
        folder = Path(runs_dir) / name
        try:
            state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("status") in (None, orch.RunStatus.RUNNING.value):
                continue
            log = json.loads((folder / "decision_log.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(log, list):
            continue
        read += 1
        for kind, seconds in step_durations(log).items():
            pooled.setdefault(kind, []).extend(seconds)
    data = {kind: statistics.median(seconds) for kind, seconds in pooled.items()}
    _history[key] = (now, data)
    return data


def clear_history_cache() -> None:
    _history.clear()


def expected_seconds(kind: str, own: dict[str, list[float]], history: dict[str, float] | None = None) -> tuple[float, str]:
    """How long this kind of action usually takes, and where the figure comes from: this run's own earlier
    occurrences first, then other runs, then a fixed default."""
    if own.get(kind):
        return _clamp(statistics.median(own[kind])), "this run"
    if history and kind in history:
        return _clamp(history[kind]), "earlier runs"
    return DEFAULT_STEP_SECONDS, "a typical step"


def _phase_sequence(n: int) -> list[tuple[str, str, str]]:
    """The agent actions of one phase in the order they run, each as (agent, action, file it leads to)."""
    state = next((st for st, k in orch.PHASE_OF.items() if k == n), None)
    makers = orch.STEPS.get(state) or []
    if not makers:
        return []
    plan, verify = orch.ROLES["plan"], orch.ROLES["verify"]
    return ([(plan.agent, plan.func, orch.plan_review_file(n))] + [(s.agent, s.func, s.output) for s in makers]
            + [(verify.agent, verify.func, orch.decision_file(n))])


def step_in_flight(log: list[dict], files: list[str] | tuple[str, ...] = ()) -> dict | None:
    """The step that is running now: the one after the last recorded one, in the phase's own order.

    Returns {"agent", "action", "index", "steps"} or None when it cannot be told. The log is followed from
    its start; with no log at all, the files written so far decide: the first step of the latest phase
    whose file is not there yet."""
    phase, mode, done, single = None, None, set(), None
    plan, verify = orch.ROLES["plan"].func, orch.ROLES["verify"].func
    for e in log if isinstance(log, list) else []:
        if not isinstance(e, dict):
            continue
        agent, action, outcome = e.get("agent"), str(e.get("action", "")), e.get("outcome")
        if agent == "founder":
            milestone = _MILESTONE.match(action)
            if action.startswith("decide:roadmap"):
                single = "roadmap" if outcome == "revise" else None
                phase, mode, done = (1, "plan", set()) if outcome == "approve" else (None, None, set())
            elif action.startswith("decide:feasibility"):
                phase, mode, single = None, None, "roadmap" if outcome == gates.CONTINUE else None
            elif milestone or action.startswith("waiver_m"):
                n = int(milestone.group(1)) if milestone else int(action[8:]) if action[8:].isdigit() else 0
                single = None
                if n and (not milestone or outcome == gates.CONTINUE):
                    phase, mode, done = n + 1, "plan", set()
                elif n and outcome == gates.REQUEST_CHANGES:
                    phase, mode, done = n, "make", set()
                else:
                    phase, mode = None, None
        elif agent == "gate":
            gate = _PHASE_GATE.match(action)
            if action == "brief_screen_gate":
                phase, mode, single = None, None, "assess" if outcome == "go" else None
            elif action == "roadmap_check":
                single = "roadmap" if outcome == "revise" else None
            elif action == "feasibility_check":
                single = None
            elif gate and gate.group(2) == "plan_review":
                phase, done, single, mode = int(gate.group(1)), set(), None, "plan" if outcome == "revise" else "make"
            elif gate:
                phase, done, single, mode = int(gate.group(1)), set(), None, "make" if outcome == "defects" else None
            elif action.startswith("milestone_review_m"):
                mode = None
            elif action.endswith("_gate"):  # the sign-off at the end of a phase
                mode, done = ("make" if outcome == "repeat" and phase else None), set()
        elif agent != "orchestrator":
            name = _base(action)
            if single and name == single:
                single = None  # code finishes this part at once
            elif mode == "make":
                done.add(name)
            elif name == verify:
                mode = None
    if single and single in orch.ROLES:
        role = orch.ROLES[single]
        return {"agent": role.agent, "action": role.func, "index": 0, "steps": [(role.agent, role.func)]}
    if not log and files:  # no log to follow: the first file of the latest phase that is not written yet
        started = [n for n in sorted(orch.PHASE_OF.values()) if any(o in files for _, _, o in _phase_sequence(n))]
        sequence = _phase_sequence(started[-1]) if started else []
        index = next((i for i, (_, _, o) in enumerate(sequence) if o not in files), None)
    else:
        sequence = _phase_sequence(phase) if phase and mode in ("plan", "make") else []
        if mode == "plan":
            index = 0 if sequence else None
        else:  # the makers in order, then the check of their work
            index = next((i for i, (_, a, _) in enumerate(sequence) if 0 < i and (a not in done or a == verify)), None)
    if index is None or not sequence:
        return None
    return {"agent": sequence[index][0], "action": sequence[index][1], "index": index,
            "steps": [(a, f) for a, f, _ in sequence]}


def estimate(log: list[dict], files: list[str], history: dict[str, float] | None = None) -> dict:
    """How far along the step in flight and its phase are. A proxy from log timestamps, never a measurement
    of the model's work; when the step cannot be told, only the fallback label is given."""
    out: dict[str, Any] = {"step_label": plain.NEXT_STEP, "step_elapsed": None}
    try:
        funcs = _step_funcs()
        last = next((e for e in reversed(log) if _marks_time(e, funcs)), None)
        elapsed = _seconds_since(last.get("timestamp", "")) if last else None
        out["step_elapsed"] = elapsed
        flight = step_in_flight(log, files)
        if not flight:
            return out
        out["step_label"] = plain.STEP_NOW.get(flight["action"], plain.NEXT_STEP)
        if elapsed is None:
            return out
        own = step_durations(log)
        expected, basis = expected_seconds(f"{flight['agent']}:{flight['action']}", own, history)
        fraction = min(STEP_FRACTION_CAP, elapsed / expected)
        overdue = elapsed >= expected
        out.update(step_expected=round(expected), step_basis=basis, step_fraction=round(fraction, 3), step_overdue=overdue,
                   step_words="Taking longer than usual." if overdue else f"Usually takes about {plain.about(expected)}.")
        steps, index = flight["steps"], flight["index"]
        if len(steps) > 1:
            left = sum(expected_seconds(f"{a}:{f}", own, history)[0] for a, f in steps[index:]) - elapsed
            out.update(phase_step=index + 1, phase_steps=len(steps), phase_fraction=round((index + fraction) / len(steps), 3))
            out["phase_words"] = f"Step {index + 1} of {len(steps)}."
            if left >= 1:
                out["eta_seconds"] = int(round(left))
                if not overdue:
                    out["phase_words"] += f" About {plain.about(left)} left in this phase."
    except Exception:  # an estimate never breaks the run page: the plain fallback is shown instead
        return {"step_label": plain.NEXT_STEP, "step_elapsed": None}
    return out


def progress(log: list[dict], files: list[str], running: bool, phase_files: list[str] | None = None,
             history: dict[str, float] | None = None) -> dict:
    """What the agents are doing now, from the run's decision log and the files written so far."""
    log = [e for e in log if isinstance(e, dict)] if isinstance(log, list) else []
    last = log[-1] if log else {}
    out = {"running": running, "now": sentence(last) if last else "Starting: no agent has acted yet.",
           "agent": AGENT_NAMES.get(last.get("agent", ""), str(last.get("agent", "")).capitalize()) if last else "",
           "since_seconds": _seconds_since(last.get("timestamp", "")) if last else None, "entries": len(log),
           "recent": [{"time": clock(e.get("timestamp", "")), "text": sentence(e)} for e in reversed(log[-6:])],
           "files": [{"name": f, "title": plain.file_label(f).capitalize()} for f in files]}
    if phase_files:
        out["steps_done"], out["steps_total"] = sum(1 for f in phase_files if f in files), len(phase_files)
    if running:
        out.update(estimate(log, files, history))
    return out

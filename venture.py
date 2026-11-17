"""Command line: the second founder approval surface.

    python -m venture run --brief briefs/example_strong.json
    python -m venture status RUN_ID
    python -m venture approve RUN_ID [ACTION_ID]
    python -m venture reject RUN_ID [ACTION_ID] --then repeat|stop
    python -m venture instruct RUN_ID "make the guide shorter"
    python -m venture resume RUN_ID
    python -m venture list
    python -m venture promote RUN_ID [--subdir weak_brief] [--replace]
"""

from __future__ import annotations

import argparse
import sys

import orchestrator as orch
import settings as settings_mod
from approval import ApprovalStore, prompt_cli
from decision_log import DecisionLog
from guardrails import BriefError


def _store(run_id: str) -> ApprovalStore:
    d = orch.run_dir(run_id)
    return ApprovalStore(d / "approvals.json", DecisionLog(d / "decision_log.json"))


def _print(rec: orch.RunRecord) -> None:
    mock = " (LLM MOCKED)" if rec.mode == "mock" else ""
    print(f"run {rec.run_id}{mock}\n  state:  {rec.state}\n  status: {rec.status}")
    if rec.blocked_reason:
        print(f"  blocked: {rec.blocked_reason}")
    if rec.pending:
        print(f"  waiting for founder: {rec.pending['summary']}\n  action id: {rec.pending['action_id']}")
    for r in rec.stop_reasons:
        print(f"  stop reason: {r}")
    if rec.usage:
        print(f"  usage: {rec.usage.get('total_tokens', 0)} tokens, ${rec.usage.get('cost_usd', 0):.4f}")
    print(f"  outputs: {orch.run_dir(rec.run_id) / 'outputs'}")


def _drive(run_id: str, interactive: bool) -> int:
    while True:
        rec = orch.advance(run_id)
        if rec.status == orch.RunStatus.AWAITING_APPROVAL.value and interactive and rec.pending:
            prompt_cli(_store(run_id), rec.pending["action_id"])
            continue
        _print(rec)
        return 0 if rec.status in ("done", "stopped", "awaiting_approval", "blocked") else 1


def _pending_id(run_id: str, action_id: str | None) -> str:
    if action_id:
        return action_id
    rec = orch.load(run_id)
    if not rec.pending:
        raise SystemExit("nothing is waiting for approval in this run")
    return rec.pending["action_id"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m venture")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start a run from a founder brief")
    r.add_argument("--brief", required=True)
    r.add_argument("--no-prompt", action="store_true", help="do not ask on the terminal; stop at approvals")
    s = sub.add_parser("resume"); s.add_argument("run_id"); s.add_argument("--no-prompt", action="store_true")
    s = sub.add_parser("status"); s.add_argument("run_id")
    sub.add_parser("list")
    a = sub.add_parser("approve"); a.add_argument("run_id"); a.add_argument("action_id", nargs="?")
    j = sub.add_parser("reject"); j.add_argument("run_id"); j.add_argument("action_id", nargs="?")
    j.add_argument("--then", choices=["repeat", "stop"], default="stop")
    i = sub.add_parser("instruct"); i.add_argument("run_id"); i.add_argument("text")
    pr = sub.add_parser("promote", help="copy a finished live run into outputs/ and decision_log.json")
    pr.add_argument("run_id"); pr.add_argument("--subdir"); pr.add_argument("--replace", action="store_true")
    args = p.parse_args(argv)

    try:
        if args.cmd == "run":
            s_ = settings_mod.load()
            rec = orch.new_run(orch.load_brief(args.brief), s_)
            return _drive(rec.run_id, interactive=not args.no_prompt and sys.stdin.isatty())
        if args.cmd == "resume":
            return _drive(args.run_id, interactive=not args.no_prompt and sys.stdin.isatty())
        if args.cmd == "status":
            _print(orch.load(args.run_id)); return 0
        if args.cmd == "list":
            for rec in orch.list_runs():
                print(f"{rec.run_id}  {rec.mode:4}  {rec.state:14} {rec.status}")
            return 0
        if args.cmd == "approve":
            _store(args.run_id).decide(_pending_id(args.run_id, args.action_id), True)
            print("approved; resume with: python -m venture resume", args.run_id); return 0
        if args.cmd == "reject":
            _store(args.run_id).decide(_pending_id(args.run_id, args.action_id), False, on_reject=args.then)
            print(f"rejected ({args.then}); resume with: python -m venture resume", args.run_id); return 0
        if args.cmd == "instruct":
            res = orch.instruct(args.run_id, args.text)
            print(res.reason); return 2 if res.refused else 0
        if args.cmd == "promote":
            dest = orch.promote(args.run_id, args.subdir, args.replace)
            print(f"promoted {args.run_id} to {dest} and decision_log.json"); return 0
    except (BriefError, orch.PromoteError, KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 5 launcher: store page and launch post drafts. Publishing and posting are MOCKED.

Generation (store_page, launch_posts) is a normal step. Publishing happens only after the
founder approves the Phase 5 gate, through the @requires_approval functions below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents import stubbed
from approval import ApprovalStore, requires_approval
from decision_log import DecisionLog

if TYPE_CHECKING:
    from orchestrator import RunContext, StepResult

MOCKED = True  # publishing and posting


@stubbed
def store_page(ctx):
    """Static HTML store page for the product; honest copy, price from Phase 4."""


@stubbed
def launch_posts(ctx):
    """Draft posts for 2-3 communities, written for the founder to post. data["posts"] = [{channel, text}]."""


@requires_approval
def publish_store_page(html: str, *, approvals: ApprovalStore, action_id: str, log: DecisionLog) -> str:
    """MOCKED publish: records the action; nothing leaves this machine."""
    log.record("launcher", "mocked_publish_store_page", approved_by="founder")
    return f"mock://store/{action_id.split(':')[-1]}"


@requires_approval
def post_launch_post(text: str, channel: str, *, approvals: ApprovalStore, action_id: str, log: DecisionLog) -> None:
    """MOCKED post: records the action; nothing is posted anywhere."""
    log.record("launcher", f"mocked_post_launch_post:{channel}", approved_by="founder")


def publish(ctx: "RunContext") -> "StepResult":
    """After approval: run the MOCKED publish and post actions and record what would have gone out."""
    from orchestrator import StepResult

    if not ctx.approved_action_id:
        raise RuntimeError("publish runs only after founder approval")
    page = ctx.read_output("phase5_step1")
    url = publish_store_page(page, approvals=ctx.approvals, action_id=ctx.approved_action_id, log=ctx.log)
    posts = ctx.artifact("phase5_step2").get("data", {}).get("posts", [])
    for p in posts:
        post_launch_post(p.get("text", ""), p.get("channel", "unknown"),
                         approvals=ctx.approvals, action_id=ctx.approved_action_id, log=ctx.log)
    lines = ["# Publish log (MOCKED)", "",
             "> **MOCKED.** Nothing was published or posted. These are the actions the founder approved.", "",
             f"- Store page: would be published at `{url}`",
             *[f"- Post to {p.get('channel', 'unknown')}: drafted, not posted" for p in posts],
             "", f"Founder approval used: `{ctx.approved_action_id}`"]
    return StepResult("\n".join(lines) + "\n")

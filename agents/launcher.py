"""Phase 5 launcher: store page and launch post drafts. Publishing and posting are MOCKED.

Generation (store_page, launch_posts) is a normal maker step. Publishing and posting happen only
after the founder approved each action on its own at the milestone review, through the
@requires_approval functions below. The Phase 2 landing page publish lives here too.
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


def _approved(ctx: "RunContext", key: str) -> bool:
    return ctx.approvals.decision(ctx.outbound_id(key)) == "approved"


@requires_approval
def publish_landing_page(html: str, *, approvals: ApprovalStore, action_id: str, log: DecisionLog) -> str:
    """MOCKED publish of the Phase 2 landing page: records the action; nothing leaves this machine."""
    log.record("launcher", "mocked_publish_landing_page", approved_by="founder")
    return f"mock://landing/{action_id.split(':')[-1]}"


def publish_landing(ctx: "RunContext") -> "StepResult":
    """Phase 2, after the founder approved publishing the landing page (MOCKED)."""
    from orchestrator import StepResult

    aid = ctx.outbound_id("publish_landing_page")
    url = publish_landing_page(ctx.read_output("phase2_step2"), approvals=ctx.approvals, action_id=aid, log=ctx.log)
    lines = ["# Landing page publish log (MOCKED)", "",
             "> **MOCKED.** Nothing was published. This records the action the founder approved.", "",
             f"- Landing page: would be published at `{url}`", "", f"Founder approval used: `{aid}`"]
    return StepResult("\n".join(lines) + "\n")


def publish(ctx: "RunContext") -> "StepResult":
    """Phase 5, after the milestone review: run each MOCKED action the founder approved, skip the rest."""
    from orchestrator import StepResult

    lines = ["# Publish log (MOCKED)", "",
             "> **MOCKED.** Nothing was published or posted. Each action below had its own founder decision.", ""]
    page_id = ctx.outbound_id("publish_store_page")
    if _approved(ctx, "publish_store_page"):
        url = publish_store_page(ctx.read_output("phase5_step1"), approvals=ctx.approvals, action_id=page_id, log=ctx.log)
        lines.append(f"- Store page: would be published at `{url}` (approval `{page_id}`)")
    else:
        lines.append(f"- Store page: not published; the founder rejected `{page_id}`")
    posts_id = ctx.outbound_id("send_launch_posts")
    posts = ctx.artifact("phase5_step2").get("data", {}).get("posts", [])
    if _approved(ctx, "send_launch_posts"):
        for p in posts:
            post_launch_post(p.get("text", ""), p.get("channel", "unknown"),
                             approvals=ctx.approvals, action_id=posts_id, log=ctx.log)
            lines.append(f"- Post to {p.get('channel', 'unknown')}: would be posted (approval `{posts_id}`)")
    else:
        lines.append(f"- Launch posts: not posted; the founder rejected `{posts_id}`")
    return StepResult("\n".join(lines) + "\n")

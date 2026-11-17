"""Phase 5 launcher: store page and launch post drafts. Publishing and posting are MOCKED.

Generation (store_page, launch_posts) is a normal maker step. Publishing and posting happen only
after the founder approved each action on its own at the milestone review, through the
@requires_approval functions below. The Phase 2 landing page publish lives here too.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, StringConstraints

import guardrails
from agents import ask, focus_block
from agents.builder import product_name
from approval import ApprovalStore, requires_approval
from decision_log import DecisionLog
from llm import ShortText

if TYPE_CHECKING:
    from orchestrator import RunContext, StepResult

MOCKED = True  # publishing and posting

Blurb = Annotated[str, StringConstraints(strip_whitespace=True, min_length=40, max_length=700)]
PostText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=80, max_length=1200)]
Channel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]


class Store(BaseModel):
    tagline: ShortText = Field(description="one plain sentence: what it is and who it is for")
    description: Blurb = Field(description="what the product does, 2 to 4 sentences")
    highlights: list[ShortText] = Field(min_length=3, max_length=5, description="what the customer can do with it")


class Post(BaseModel):
    channel: Channel = Field(description="where the founder could post it")
    text: PostText = Field(description="the post, written in the founder's own voice")


class Posts(BaseModel):
    posts: list[Post] = Field(min_length=2, max_length=3)


_STORE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  body {{ margin: 0; font: 17px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d1d1b; background: #fbfaf7; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 1.9rem; line-height: 1.2; margin: 1rem 0 .5rem; }}
  h2 {{ font-size: 1.15rem; margin: 1.8rem 0 .4rem; }}
  .draft {{ background: #fff1c2; border: 2px solid #d9a400; border-radius: 8px; padding: 8px 12px; font-size: .9rem; }}
  .sub {{ font-size: 1.1rem; color: #4b4b46; }}
  .price {{ font-size: 1.4rem; font-weight: 700; margin: .4rem 0; }}
  button {{ font: inherit; padding: 10px 16px; border-radius: 6px; border: 1px solid #b9b7ae; background: #d8d6cd; color: #4b4b46; }}
  .small {{ font-size: .85rem; color: #6b6b66; }}
</style>
</head>
<body>
<main>
  <p class="draft">{draft_header} This page is not published. The buy button is not connected, so no payment can be taken.</p>
  <h1>{name}</h1>
  <p class="sub">{tagline}</p>
  <p>{description}</p>
  <h2>What you can do with it</h2>
  <ul>
{highlights}
  </ul>
  <h2>What is inside</h2>
  <ul>
{inside}
  </ul>
  <h2>Price</h2>
  <p class="price">{price}</p>
  <button type="button" disabled>Buy (not connected)</button>
  <p class="small">Refunds (draft policy, for the founder to confirm): {refund}</p>
  <p class="small">What you get out of {name} depends on how you use it.</p>
</main>
</body>
</html>
"""


def store_page(ctx):
    """Static HTML store page for the product; honest copy, price from Phase 4."""
    from orchestrator import StepResult

    name = product_name(ctx)
    pricing = ctx.artifact("phase4_step1").get("data", {})
    sections = ctx.artifact("phase3_step2").get("data", {}).get("sections", [])
    refund = ctx.artifact("phase4_step2").get("data", {}).get("refund_excerpt", "")
    body = (focus_block(ctx) + f"Product: {name}. Its sections: {'; '.join(sections) or 'see the spec'}.\n"
            "Write the copy for the store page.")
    c = ask(ctx, "launcher", "store_page", Store, body)
    esc = html.escape
    price = (f"${pricing['price_usd']:,.2f}, {pricing.get('charged_as', 'one-time payment')}" if pricing.get("price_usd")
             else "Not set yet")
    page = _STORE.format(draft_header=esc(guardrails.DRAFT_HEADER), name=esc(name), tagline=esc(c.tagline), description=esc(c.description), price=esc(price),
                         highlights="\n".join(f"    <li>{esc(x)}</li>" for x in c.highlights),
                         inside="\n".join(f"    <li>{esc(x)}</li>" for x in sections) or "    <li>See the product file</li>",
                         refund=esc(refund or "not written yet"))
    return StepResult(page, data={"product_name": name, "price": price})


def launch_posts(ctx):
    """Draft posts for 2-3 communities, written for the founder to post. data["posts"] = [{channel, text}]."""
    from orchestrator import StepResult

    name = product_name(ctx)
    channels = ctx.artifact("phase2_step1").get("data", {}).get("boxes", {}).get("channels", [])
    pricing = ctx.artifact("phase4_step1").get("data", {})
    body = (focus_block(ctx) + f"Product: {name}."
            + (f" Price: ${pricing['price_usd']:,.2f}, {pricing.get('charged_as', 'one-time payment')}." if pricing.get("price_usd") else "")
            + "\nChannels from the Lean Canvas (assumptions): " + ("; ".join(channels) or "none listed")
            + "\nWrite 2 or 3 launch post drafts, one per channel.")
    p = ask(ctx, "launcher", "launch_posts", Posts, body)
    posts = [{"channel": x.channel, "text": x.text} for x in p.posts]
    lines = [guardrails.DRAFT_HEADER, "", "# Launch post drafts", "",
             "Drafts for the founder to read, edit and post themselves. Nothing has been posted, and the system posts nothing.", ""]
    for i, post in enumerate(posts, start=1):
        lines += [f"## {i}. {post['channel']}", "", post["text"], ""]
    return StepResult("\n".join(lines), data={"posts": posts})


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

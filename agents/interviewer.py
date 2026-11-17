"""Phase 1 interviewer: a Mom Test interview guide (step 4).

One model call. The guide asks about past behaviour and current spend, and never pitches
the idea. A schema check rejects hypothetical or leading questions ("would you buy..."),
so the model has to rewrite them. The guide is a script for the founder; the system does
not conduct interviews and reports no interview results.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from agents import ask
from llm import ShortText

_HYPOTHETICAL = re.compile(
    r"\b(would you|will you|would it|do you think|could you see yourself|how likely|if we built|if there (was|were)|"
    r"wouldn't it|don't you (think|agree)|how much would you)\b", re.IGNORECASE)


class Question(BaseModel):
    question: ShortText
    learn: ShortText = Field(description="what the answer tells the founder")

    @field_validator("question")
    @classmethod
    def past_behaviour_only(cls, v: str) -> str:
        hit = _HYPOTHETICAL.search(v)
        if hit:
            raise ValueError(f"'{hit.group(0)}' is hypothetical or leading; ask what they actually did, when, and what it cost")
        return v


class Guide(BaseModel):
    opening: ShortText = Field(description="how to open the conversation without pitching the idea")
    questions: list[Question] = Field(min_length=6, max_length=10)
    do_not: list[ShortText] = Field(min_length=2, max_length=5, description="things the founder must avoid in the interview")


def interview_guide(ctx):
    """A Mom Test interview guide: past behaviour and current spend, no pitching."""
    from orchestrator import StepResult

    pick = ctx.artifact("phase1_step2").get("data", {})
    opps = {o["id"]: o for o in ctx.artifact("phase1_step1").get("data", {}).get("opportunities", [])}
    chosen = opps.get(str(pick.get("pick") or ""), {})
    who = chosen.get("customer") or ctx.brief["target_market"]
    what = chosen.get("problem") or ctx.brief["idea"]
    body = (f"Write an interview guide for talking to: {who}.\nThe problem to learn about: {what}.\n"
            "Write 6 to 10 questions about what they did in the past, how often, and what they spend now.")
    g = ask(ctx, "interviewer", "interview_guide", Guide, body)
    lines = ["# Customer interview guide (Mom Test)", "", f"- Who to talk to: {who}", f"- What to learn about: {what}", "",
             "This is a script for the founder. No interviews have been run, and this file reports no results.", "",
             "## Opening", "", g.opening, "", "## Questions", ""]
    lines += [f"{i}. {q.question}\n   - What you learn: {q.learn}" for i, q in enumerate(g.questions, start=1)]
    lines += ["", "## Do not", ""] + [f"- {d}" for d in g.do_not]
    return StepResult("\n".join(lines) + "\n", data={"questions": len(g.questions)})

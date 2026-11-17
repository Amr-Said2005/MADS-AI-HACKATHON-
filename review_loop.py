"""A review loop with a fixed number of steps: code finds defects, the model revises, code checks again.

run(draft, checks, revise, steps):
  1. `checks(draft)` returns the defects of a draft as short strings. The checks are code, never a model.
  2. No defects: stop early. No model call is made for a clean draft.
  3. Otherwise `revise(draft, defects, step)` asks the model once for a corrected draft. It returns the new
     draft, or None when the reply did not match the schema.
  4. The revision replaces the draft only when it has fewer defects. A worse or invalid revision is dropped.

The loop never runs more than `steps` revisions (hard cap MAX_STEPS) and never raises because a revision is
bad: the caller always gets the best draft seen, the defects it still has and a record of every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

MAX_STEPS = 3
MAX_DEFECTS_SHOWN = 12

D = TypeVar("D")


@dataclass
class Review(Generic[D]):
    draft: D  # the best draft seen: fewest defects, the earlier one on a tie
    defects: list[str]  # what the best draft still has
    steps: list[dict[str, Any]] = field(default_factory=list)  # one record per revision call

    @property
    def revisions(self) -> int:
        return len(self.steps)

    def summary(self) -> dict[str, Any]:
        """A small record for a step's data: how many revision calls ran and what is left."""
        return {"steps": self.steps, "defects_left": list(self.defects)}


def clamp(steps: int) -> int:
    return max(0, min(int(steps), MAX_STEPS))


def numbered(defects: list[str]) -> str:
    """The defect list as the model sees it."""
    shown = defects[:MAX_DEFECTS_SHOWN]
    return "\n".join(f"{i}. {d}" for i, d in enumerate(shown, start=1))


def run(draft: D, checks: Callable[[D], list[str]], revise: Callable[[D, list[str], int], D | None],
        steps: int, on_step: Callable[[dict[str, Any]], None] | None = None) -> Review[D]:
    """Run at most `steps` revisions (0 turns the loop off: the checks do not even run)."""
    steps = clamp(steps)
    if steps == 0:
        return Review(draft, [])
    review = Review(draft, list(checks(draft)))
    for step in range(1, steps + 1):
        if not review.defects:
            break
        record: dict[str, Any] = {"step": step, "defects": list(review.defects), "revised": False, "kept": False,
                                  "defects_after": len(review.defects)}
        candidate = revise(review.draft, list(review.defects), step)
        if candidate is not None:
            record["revised"] = True
            found = list(checks(candidate))
            if len(found) < len(review.defects):
                review.draft, review.defects = candidate, found
                record["kept"] = True
                record["defects_after"] = len(found)
        review.steps.append(record)
        if on_step:
            on_step(record)
    return review

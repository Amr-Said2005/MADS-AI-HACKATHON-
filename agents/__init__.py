"""Agents, one module per role. Each step function takes a RunContext and returns a StepResult.

A function decorated with @stubbed is not built yet: calling it raises NotImplementedError,
the orchestrator marks the step STUBBED and the run pauses there (status "blocked").
Remove the decorator only when the function does the real work.
"""

from __future__ import annotations

import functools
from typing import Any, Callable


def stubbed(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"STUBBED: {fn.__module__}.{fn.__name__} is not built yet")

    wrapper.__status__ = "STUBBED"  # type: ignore[attr-defined]
    return wrapper

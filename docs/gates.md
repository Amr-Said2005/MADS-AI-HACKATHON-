# Inner loop rules

The tables of gates, outcomes and thresholds are in [ARCHITECTURE.md](../ARCHITECTURE.md#gates). This page holds the detailed rules of the loop that runs inside every phase (`orchestrator.INNER`, `inner_next`).

## Stages

| Stage | Who | Outcome | Next |
|---|---|---|---|
| `PLAN` | planner | done | `PLAN_REVIEW` |
| `PLAN_REVIEW` | code (`gates.check_plan`) | approved / revise / stop | `MAKE` / `PLAN` / STOPPED |
| `MAKE` | the phase's makers | done | `VERIFY` |
| `VERIFY` | checker, one call | verified / defects | `SIGN_OFF` / `MAKE` |
| `SIGN_OFF` | `gates.py`, code only | repeat / go, pivot or stop | `MAKE` / `MILESTONE_REVIEW` with the recommendation |
| `MILESTONE_REVIEW` | founder | continue / request changes / pivot / stop | next phase / `MAKE` / the pivot phase's `PLAN` / STOPPED |

## Rules

- `SIGN_OFF` never routes to `VERIFY`: re-verifying an unchanged artefact returns the same result.
- Each back-edge (`revise`, `defects`, `repeat`) may fire at most twice per phase (`gates.MAX_BACK_EDGES`). "Request changes" spends the `repeat` budget. When `defects` is used up, the artefact goes to `SIGN_OFF` with its open defects listed, and the gate can only recommend pivot or stop. When `revise` or `repeat` is used up, the outcome becomes stop. The counters are stored with the run and printed in every gate file.
- Gate criteria (`G`) are verified by code with the same checks the sign-off runs, and a miss there is the gate's to act on. The checker (one model call) verifies only the planner's criteria (`C`), and only a failed `C` criterion or a blocked file counts as a defect. A rework for a defect keeps the same search queries, so the evidence stays the same; new queries are used only when the gate asks for a repeat.
- The plan review is code: every criterion needs an id, a check, a threshold and a method of `code` or `source`, with at most 8 criteria. A plan that fails is sent back (revise); when the revise budget is used up, the run stops.
- A file blocked by the output check, or a step whose model output is invalid twice, blocks only that file. It shows up as a failed criterion (`blocked:<file>`), so it goes back to the makers within the defects budget and then reaches the founder as an open defect.
- Every threshold that has no cited basis (3 domains, half `ASSUMPTION`, repeat once, 50% of skills, half the hours, three soft fails, 2 back-edges) is labelled `ASSUMPTION` wherever it is printed.

## Roadmap

One planner proposes six milestones, one per phase (with `N_ARCHITECTS=2`, two proposals are reconciled by the checker; that step is STUBBED). Code enforces exactly six milestones in phase order, each with what the founder will see and the decision asked. Code also attaches each milestone's gate, and it rejects any milestone that promises income, sales or sign-up numbers.

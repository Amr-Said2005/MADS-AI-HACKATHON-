# Architecture

## Overview

One Python process (a FastAPI web UI plus a CLI) runs a two-level state machine in `orchestrator.py`; lookup tables choose every next state, and the model never does. Before Phase 1, three states run once per run: the brief screen (rules), the feasibility check (a rubric scored by code) and the milestone roadmap (founder approval). Inside every phase the same loop runs: a planner writes the pass criteria before any work starts, code reviews the plan, the makers produce the files, a checker verifies them in one call without editing them, `gates.py` signs off in code, and the founder decides at a milestone review. Nobody signs off their own work. Every agent action, gate and founder decision is appended to the run's decision log by `decision_log.py`, its only writer. The model client (`llm.py`) uses a local open-weight model through Ollama's native API with a JSON schema (default `qwen3:4b-instruct-2507-q4_K_M` on CPU) or any OpenAI-compatible endpoint; every structured reply is validated with Pydantic and retried once, and a second failure is recorded as `model_output_invalid`, never replaced with placeholder text.

```mermaid
flowchart TD
    F[Founder brief] --> S{Brief screen}
    S -- excluded or unfit --> X[STOP with reasons]
    S -- ok --> Q{Feasibility check}
    Q -- hard fail or too many gaps --> X
    Q -- narrowed idea, founder approves --> Q
    Q -- go --> M[Architects agree the milestone roadmap]
    M --> MA{Founder approves roadmap}
    MA -- revise --> M
    MA -- stop --> X
    MA -- approve --> A[Architects: plan and pass criteria]
    A --> R{Peer review of plan}
    R -- revise --> A
    R -- stop --> X
    R -- approved --> D[Makers: produce artefact]
    D --> V[Checker: verify against criteria]
    V -- defects --> D
    V -- verified --> G{Gate: sign-off}
    G -- repeat --> D
    G -- go, pivot or stop as a recommendation --> P{Milestone review by founder}
    P -- request changes --> D
    P -- pivot --> A
    P -- stop --> X
    P -- continue, only if the gate said go --> N[Next milestone]
```

A run lives in `runs/<run_id>/` (git-ignored): `state.json`, `approvals.json`, `decision_log.json` and `outputs/`. It pauses with status `awaiting_approval` whenever it needs the founder, and with status `blocked` when it reaches a STUBBED agent or the model is unreachable. `python -m venture resume` (or the Resume button) continues it. `python -m venture promote RUN_ID` copies a finished **live** run into the committed `outputs/` and `decision_log.json`; it refuses runs made with `LLM_MODE=mock`.

## Phases

Every agent is a function `fn(ctx) -> StepResult` in `agents/<role>.py`, with its prompt in `prompts/<role>.md`. The orchestrator, not the agent, writes the file, applies the citation rule, runs the output check and logs the action. Two roles work in every phase:

- **`planner`** (architect role): the plan has at most 8 criteria, each with `id`, `check`, `threshold` and `method` (`code` or `source`). Code writes the sign-off gate's own checks into every plan (`G1`, `G2`, ...), so a model never retypes the gate's numbers; the planner adds 2 to 4 criteria about the quality of the files (`C1`, ...). The makers are shown the approved plan. `N_ARCHITECTS` defaults to 1; with 2 (optional, Phase 1 only) the plan review picks the first plan that passes and records why. The plan review is code, not a model call.
- **`checker`** (verification role): verifies artefacts against the chosen criteria in one call (pass, fail or unverified, with evidence). It never edits an artefact. Code accepts only a registered URL, a test name, an output file of this run or `ASSUMPTION` as evidence, and turns a "pass" without valid evidence into "unverified". In `FAST_MODE` the checker verifies only claims and gate inputs.

Each phase writes `phase<N>_gate_plan_review.md` (the chosen plan, its criteria and the verdict) and `phase<N>_gate_decision.md` (the sign-off, the verification report, the loop budget and the founder's decision). Every output file opens with one line written by code: phase and step, agent, model and the run's start time. A gate file then states `Decision:` with its reasons on the next line, and a claim line ends with its source URL or the word `ASSUMPTION`.

### Phase 1: Discover

- **Agents:** before Phase 1: `guardrails` (brief screen), `assessor` (feasibility), `planner` and `checker` (roadmap). In the phase: `planner`, `checker`, `researcher`, `scorer`, `interviewer`.
- **Tools:** `sources.search` (local SearXNG, then keyless Hacker News, Stack Exchange, MediaWiki and GitHub search; Reddit is not used; every request sends a User-Agent with the project name and `CONTACT_URL`) and `sources.SourceRegistry.fetch` (main text by trafilatura; `MAX_SOURCES` new pages per agent step); the model client. Each source is tagged `SAY` (someone describes the problem) or `PAY` (someone pays for a comparable solution).
- **Method:** screen the founder brief (excluded categories, completeness). Run the feasibility rubric (below). Agree a six-milestone roadmap with the founder. Then the researcher searches and fetches public pages, lists the evidence that the founder's problem exists, and gives 2 to 4 opportunities, each a customer segment with that problem. The model points at pages by number and code supplies the URL, so it never types one; a statement with no page behind it is printed as `ASSUMPTION`. The scorer gives 1 to 5 on pain, willingness to pay, reach and founder fit, and estimates the cost and weekly hours of the smallest version; code adds the totals and picks. The researcher then looks for comparable paid products (`PAY`). The interviewer writes a Mom Test guide (past behaviour and current spend, no pitching); a schema check rejects hypothetical questions, and no interviews are run. Each agent step is one model call, so Phase 1 takes six.
- **Output:** `phase1_gate_brief_screen.md`, `phase1_gate_feasibility.md`, `phase1_gate_roadmap.md`, `phase1_gate_plan_review.md`, `phase1_step1_opportunities.md`, `phase1_step2_scoring.json`, `phase1_step3_market.md`, `phase1_step4_interview_guide.md`, `phase1_gate_decision.md`.

### Phase 2: Validate

- **Agents:** `planner`, `checker`, `strategist`, `launcher` (mocked publish).
- **Tools:** the model client; Phase 1 outputs and their registered sources.
- **Method:**
  - **Lean Canvas:** one model call fills ten boxes. An entry cites a numbered line of Phase 1 evidence, and code supplies the URL; any other entry is printed as `ASSUMPTION`.
  - **Landing page:** one model call writes the copy, and code puts it into a fixed template. The template has no testimonial block, no countdown and no earnings figures, and it says on the page that it is a draft whose form is not connected.
  - **Demand test:** no model call. The target is set by code before any result exists: at least 5% sign-ups from at least 200 cold visitors within 14 days (`gates.DEMAND_TARGET`). The basis: email sign-ups of 5-15% from targeted paid traffic, at least 200 unique visitors for a directional signal, and runs of about 4 days to 2.5 weeks ([Kromatic, Landing Page Smoke Test](https://kromatic.com/real-startup-book/2-evaluative-market-experiments/value-proposition-test/landing-page-smoke-test/)). Separately, there is a 6.6% median landing page conversion across industries ([Unbounce, Conversion Benchmark Report 2024](https://unbounce.com/conversion-benchmark-report/)).
  - **Result:** the sign-up count comes only from the MOCKED counter in `demand.py`, which reads zero, so the system never invents sign-ups. The result is reported as not measured, and the gate says demand is not validated.
  - **Publishing:** a separate founder decision at the milestone review, and MOCKED.
- **Output:** `phase2_gate_plan_review.md`, `phase2_step1_lean_canvas.md`, `phase2_step2_landing_page.html`, `phase2_step3_demand_test.md`, `phase2_gate_decision.md`, `phase2_step4_publish_log.md`.

### Phase 3: Build

- **Agents:** `planner`, `checker`, `builder`.
- **Tools:** the model client; code-run product tests.
- **Method:** text digital products only (guide, template pack, checklist, mini-course). One model call writes the smallest spec (3 to 5 sections and what is left out). One call writes every section of the product. Code then runs four acceptance tests on the text: every spec section is present, every section has enough content, the product is short enough to check, and no placeholder text is left. A failed test sends the work back to the makers. One more call writes the user docs.
- **Output:** `phase3_gate_plan_review.md`, `phase3_step1_mvp_spec.md`, `phase3_step2_product.md`, `phase3_step3_user_docs.md`, `phase3_gate_tests.md`, `phase3_gate_decision.md`.

### Phase 4: Commercial setup

- **Agents:** `planner`, `checker`, `commercial`.
- **Tools:** the model client; Phase 1 paying-source evidence.
- **Method:** one model call recommends a price; each reason cites a numbered line of Phase 1 market evidence or is printed as `ASSUMPTION`, and no sales are forecast. One call drafts refund, privacy and terms text; code adds the heading that says they are drafts and not legal advice. Code checks that a price, a product name and the label are present. The founder then reviews the price and policies at the milestone.
- **Output:** `phase4_gate_plan_review.md`, `phase4_step1_pricing.md`, `phase4_step2_policies.md`, `phase4_gate_decision.md`.

### Phase 5: Sell/deliver/get paid (Launch; MOCKED)

- **Agents:** `planner`, `checker`, `launcher`, `delivery` (with `payments`, `refunds`, `reconcile`).
- **Tools:** the mocked ledger; the `@requires_approval` publish, post, checkout and refund functions.
- **Method:** one model call writes the store page copy, which code puts into a fixed template (no testimonials, countdowns or earnings figures; the buy button is not connected). One call drafts 2 or 3 launch posts in the founder's own voice for the founder to post. At the milestone review the founder decides three outbound actions one by one: publish the store page, post the drafts, and run the payment test. Each has its own action id. Only the approved ones run, all MOCKED; the payment test exercises sale to cash (see below). Nothing is published, posted or charged.
- **Output:** `phase5_gate_plan_review.md`, `phase5_step1_store_page.html`, `phase5_step2_launch_posts.md`, `phase5_gate_decision.md`, `payment_test_log.md`, `phase5_step4_publish_log.md`.

### Phase 6: Operate and learn

- **Agents:** `planner`, `checker`, `operator`.
- **Tools:** the model client; the MOCKED sign-up counter (`demand.py`).
- **Method:** one model call writes a support FAQ from the product, the user docs, the price and the draft policies; anything about money goes to the founder. The memo's metrics and its recommendation are made by code. A metric comes only from a counter, and the counter is MOCKED and reads zero, so the metric is reported as not measured. The memo's list of what the run showed is written by code from the run's facts. One model call adds only the riskiest assumptions and what to measure next.
- **Output:** `phase6_gate_plan_review.md`, `phase6_step1_support_faq.md`, `phase6_step2_pivot_memo.md`, `phase6_gate_decision.md`.

## Gates

| Gate (state) | What it checks | Outcomes | Recommends STOP when |
|---|---|---|---|
| Brief screen (`SCREEN_BRIEF`) | Excluded category (physical goods; financial, investment or crypto; health or legal advice), borderline matches, and a complete brief | go to feasibility; stop | The product is excluded or the brief is incomplete |
| Feasibility (`FEASIBILITY`) | The rubric below (I1 to I3, F1 to F4), scored by code | go to the roadmap; repeat once with a narrowed idea the founder approves; stop | Any hard fail, three or more soft fails, or a narrowed idea that still fails |
| Roadmap (`ROADMAP`) | Six milestones in phase order, each with what the founder will see and the decision asked; no promised income, sales or sign-up numbers | go when the founder approves; repeat on the founder's notes (2 at most); stop | The founder stops, or the revisions are used up |
| Phase 1 evidence (`P1_DISCOVER`) | 3 sources from 3 domains that describe the problem; 1 `PAY` source in which code finds a price; the founder's budget and hours cover the estimate; fewer than half the claims are `ASSUMPTION` | go to Phase 2; repeat once with new searches; pivot to the brief screen with a revised idea; stop | Two or more thresholds are missed, or one is still missed after the repeat |
| Phase 2 (`P2_VALIDATE`) | The canvas Problem and Customer boxes each cite Phase 1 evidence; the landing page has an email form; a measured demand result must meet the target set beforehand (a MOCKED one is reported, not counted) | go to Phase 3; repeat once; pivot to Phase 1; stop | A check is still missed after the repeat |
| Phase 3 product tests (`P3_BUILD`) | Four acceptance tests that code runs on the product text | go to Phase 4; repeat once; pivot to Phase 2; stop | A test still fails after the repeat |
| Phase 4 (`P4_COMMERCIAL`) | A price, the product name, and policies labelled as drafts and not legal advice | go to Phase 5; repeat once; pivot to Phase 2; stop | A check is still missed after the repeat |
| Phase 5 (`P5_LAUNCH`) | The store page and post drafts exist; each outbound action has its own founder decision | go to Phase 6; repeat once; pivot to Phase 4; stop | A check is still missed after the repeat |
| Phase 6 (`P6_OPERATE`) | Every metric with a target set beforehand is measured and met | go (persevere) to done; pivot to Phase 1 on a measured miss; stop | A metric is not measured. The counter is MOCKED in this prototype, so a full run ends with STOP, and the decision file says it is not a verdict on the idea |

Every gate is code in `gates.py`; no model call decides an outcome. In any phase the gate also recommends STOP when the loop budget is used up, or when defects stay open and the run's one pivot is spent. The run ends as STOPPED when `MAX_MODEL_CALLS` (60) or `MAX_RUN_SECONDS` (900) is reached or the founder presses stop.

The outer machine, one state per phase (`orchestrator.TRANSITIONS`):

```mermaid
stateDiagram-v2
    [*] --> SCREEN_BRIEF
    SCREEN_BRIEF --> FEASIBILITY: go
    SCREEN_BRIEF --> STOPPED: stop
    FEASIBILITY --> ROADMAP: go
    FEASIBILITY --> FEASIBILITY: repeat (narrowed idea, founder approves, max 1)
    FEASIBILITY --> STOPPED: stop
    ROADMAP --> P1_DISCOVER: go (founder approves)
    ROADMAP --> ROADMAP: repeat (founder's revision notes, max 2)
    ROADMAP --> STOPPED: stop
    P1_DISCOVER --> P2_VALIDATE: go
    P1_DISCOVER --> P1_DISCOVER: repeat
    P1_DISCOVER --> SCREEN_BRIEF: pivot (revised idea)
    P1_DISCOVER --> STOPPED: stop
    P2_VALIDATE --> P3_BUILD: go
    P2_VALIDATE --> P2_VALIDATE: repeat
    P2_VALIDATE --> P1_DISCOVER: pivot
    P2_VALIDATE --> STOPPED: stop
    P3_BUILD --> P4_COMMERCIAL: go
    P3_BUILD --> P3_BUILD: repeat
    P3_BUILD --> P2_VALIDATE: pivot
    P3_BUILD --> STOPPED: stop
    P4_COMMERCIAL --> P5_LAUNCH: go
    P4_COMMERCIAL --> P4_COMMERCIAL: repeat
    P4_COMMERCIAL --> P2_VALIDATE: pivot
    P4_COMMERCIAL --> STOPPED: stop
    P5_LAUNCH --> P6_OPERATE: go
    P5_LAUNCH --> P5_LAUNCH: repeat
    P5_LAUNCH --> P4_COMMERCIAL: pivot
    P5_LAUNCH --> STOPPED: stop
    P6_OPERATE --> DONE: persevere
    P6_OPERATE --> P1_DISCOVER: pivot
    P6_OPERATE --> STOPPED: stop
    DONE --> [*]
    STOPPED --> [*]
```

For a phase, go, pivot and stop happen only through the founder's milestone review, and "repeat" means back to the makers inside the phase. An outcome that is not defined for a state leads to STOPPED. A run allows one pivot.

**Founder authority.** The founder can always be more cautious than the system, never less:

| Gate recommended | Founder may choose |
|---|---|
| go | continue, request changes, pivot, stop |
| pivot | pivot, request changes (if repeat budget remains), stop |
| stop | stop, pivot, request changes (if repeat budget remains) |

"Continue" is never offered after a failed gate, and `ApprovalStore.choose` refuses a choice that was not offered. No phase starts without a founder approval of the roadmap on record, and none advances without a founder decision at its milestone review. Publish, send and spend actions in Phases 2 and 5 are separate decisions on the milestone screen; each has its own action id, which hashes the exact content approved, so approving a milestone never approves a publication by implication. Hard rules (excluded products, the never-list in `guardrails.py`) cannot be overridden by anyone.

**Feasibility rubric** (once per run, before any planning; market demand is left to the Phase 1 gate):

| Id | Criterion | Fail type |
|---|---|---|
| I1 | The brief names a specific customer and a specific problem | soft |
| I2 | The product is a supported digital type (guide, template pack, checklist, mini-course) | hard |
| I3 | At least 1 fetched source shows the problem exists (2 searches at most) | soft |
| F1 | The founder's skills cover at least half the skills the idea needs | soft |
| F2 | `budget_usd` covers the estimated cost of the smallest version | hard |
| F3 | `hours_per_week` over 4 weeks covers the estimated effort | hard if short by more than half, else soft |
| F4 | The idea does not conflict with the founder's stated values | hard |

Any hard fail: stop. Three or more soft fails: stop. One or two soft fails: repeat once with a narrowed idea, which the founder must approve. Otherwise go. Every estimate the assessor makes is labelled `ASSUMPTION` in `phase1_gate_feasibility.md`.

**Phase 1 evidence thresholds** (set in `gates.PHASE1` before any run, never after):

- at least 3 fetched sources from 3 different domains that describe the problem;
- at least 1 fetched `PAY` source showing someone already pays for a comparable solution. The model proposes the `PAY` tag, and code keeps it only when it finds a price in the page's text; otherwise the claim is downgraded and the downgrade is logged. Basis: what people do is stronger evidence than what people say ([Strategyzer, Business Testing](https://www.strategyzer.com/library/business-testing-is-your-hypothesis-really-validated));
- the brief's budget and weekly hours cover the scorer's estimate of the smallest viable version;
- fewer than half of the claims are labelled `ASSUMPTION`. The claims counted are the evidence for the problem, the chosen opportunity's own assumptions and the market claims.

Each missed threshold is written to `phase1_gate_decision.md`. Thresholds with no cited basis are labelled `ASSUMPTION` wherever they are printed.

**Inner loop and loop budget.** Inside a phase the stages run in a fixed order (`orchestrator.INNER`): plan, plan review by code, make, verify, sign-off by `gates.py`, then the founder's milestone review. Each back-edge (`revise`, `defects`, `repeat`) may fire at most twice per phase (`gates.MAX_BACK_EDGES`); the counters are stored with the run and printed in every gate file. The stage table and the detailed rules are in [docs/gates.md](docs/gates.md).

**Safeguards** (how each one works is in [docs/safeguards.md](docs/safeguards.md)):

| Rule | Mechanism | Test |
|---|---|---|
| Never promise income or success | request screen, output check, roadmap check | trick prompts; blocked output fixtures |
| No fake reviews, testimonials, sign-ups or timers | request screen; output check; sign-up results only from a labelled MOCKED counter | trick prompts, including evasions |
| No impersonation of founder, customer or brand | request screen; posts stay drafts; customer-voice output check | trick prompts; output fixtures |
| Never hold or move money, create accounts, store credentials | no agent holds a tool that publishes, sends, spends or signs; no payment SDK, mail client or social-media client in the dependencies; passwords and card numbers are replaced with `[REDACTED]` before anything is stored | dependency test; redaction tests |
| Nothing published, sent, spent or signed without approval | `@requires_approval`, one action id per outbound action | `test_approval.py`, `test_pipeline.py` |
| No excluded products | brief screen for the brief, request screen for instructions | four excluded briefs and six disguised ones stop; ordinary briefs pass |
| An instruction cannot waive approval or sign for the founder | request screen refuses it; approval exists only as a record in `approvals.json`, never as text | trick prompts with negative controls |
| Every market claim sourced or `ASSUMPTION` | citation registry | unregistered URL stamped `ASSUMPTION` |
| Every mock labelled | `MOCKED` flag, UI banner, README table | each MOCKED module named in the README table |
| Outputs from real runs only | `promote()` is the only path into `outputs/` and refuses mock runs | mock run cannot be promoted |
| Generated product text is not presented as verified | draft header on the product, user docs, store page and launch posts; the output check blocks one without it | a product file without the header is blocked |
| Instructions hidden in web pages are ignored | fetched text is fenced as data; next states come from lookup tables | injection fixture page logs `injection_suspected` |
| Fetching reaches public pages only | GET only, `http`/`https`, timeout and size cap; private, loopback and link-local addresses refused on every redirect hop | `test_sources.py` |
| Run cannot loop or spend without limit | loop and run budgets, stop button | budget tests end as STOPPED |

## Sale to cash

1. **Payment triggers delivery (MOCKED).** Opening checkout is an outbound action and needs founder approval. A simulated payment event marks the order paid and calls `delivery.deliver`, which grants access.
2. **A refund revokes access (MOCKED).** `refunds.refund` needs founder approval, marks the order refunded and revokes access.
3. **Idempotency prevents double delivery (MOCKED).** Delivery is keyed by `deliver:<order_id>`. The test sends the same payment event twice; the second is ignored and the order is delivered once.
4. **Reconciliation matches orders, payments, fees and payouts (MOCKED).** `reconcile.py` matches each order with its payment, the fee (an illustrative mock fee) and the recorded payout (payments minus fees minus refunds), and lists any mismatch.

All four run in an in-memory ledger (`payments.py`, `delivery.py`, `refunds.py`, `reconcile.py`). No payment provider, SDK or sandbox is used and no money moves. `payment_test_log.md` records one test run, which runs only if the founder approved the payment test on its own.

## Data flow

```
founder brief (briefs/*.json or the form on the home page)
   │  validate_brief → screen_brief → feasibility rubric → roadmap ──► founder approves
   ▼
per phase:  planner(s) ─► plan ─► checker review ─► makers fn(ctx) ─► checker verify ─► gates.py sign-off
                                                       │   ▲
                                                       │   └── llm.chat (tokens, cost)   sources.fetch (registry)
                                                       ▼
            citation rule: unfetched URL → ASSUMPTION ──► output check (block, regenerate once, stop)
                                                       ▼
            runs/<id>/outputs/<phase files>  +  runs/<id>/decision_log.json (one entry per action)
   ▼
milestone review ──► founder choice (UI or CLI) + one approve/reject per outbound action → approvals.json
   ▼
promote (live runs only) ──► outputs/  +  decision_log.json  (committed)
```

Founder instructions during a run pass `guardrails.screen_request` first (secrets redacted, rules, then the fail-closed classifier for ambiguous cases); refused ones are logged with `"outcome": "refused"` and never reach an agent. Fetched pages reach agents only as fenced data. Founder notes from "request changes" and "revise" reach the agents as `ctx.feedback`.

## Built vs. stubbed

Step statuses, the same as in the README table (**Working** only where a real run is committed in `outputs/`):

| Steps | Status |
|---|---|
| Brief screen and feasibility check: `phase1_gate_brief_screen.md`, `phase1_gate_feasibility.md` | Working |
| Roadmap and Phase 1: `phase1_gate_roadmap.md`, `phase1_gate_plan_review.md`, `phase1_step1_opportunities.md`, `phase1_step2_scoring.json`, `phase1_step3_market.md`, `phase1_step4_interview_guide.md`, `phase1_gate_decision.md` | STUBBED |
| Phase 2: `phase2_gate_plan_review.md`, `phase2_step1_lean_canvas.md`, `phase2_step2_landing_page.html`, `phase2_gate_decision.md` | STUBBED |
| Phase 2 demand test and landing page publish: `phase2_step3_demand_test.md`, `phase2_step4_publish_log.md` | MOCKED |
| Phase 3: `phase3_gate_plan_review.md`, `phase3_step1_mvp_spec.md`, `phase3_step2_product.md`, `phase3_step3_user_docs.md`, `phase3_gate_tests.md`, `phase3_gate_decision.md` | STUBBED |
| Phase 4: `phase4_gate_plan_review.md`, `phase4_step1_pricing.md`, `phase4_step2_policies.md`, `phase4_gate_decision.md` | STUBBED |
| Phase 5: `phase5_gate_plan_review.md`, `phase5_step1_store_page.html`, `phase5_step2_launch_posts.md`, `phase5_gate_decision.md` | STUBBED |
| Phase 5 payment, delivery and refund test and publish log: `payment_test_log.md`, `phase5_step4_publish_log.md` | MOCKED |
| Phase 6: `phase6_gate_plan_review.md`, `phase6_step1_support_faq.md`, `phase6_gate_decision.md` | STUBBED |
| Phase 6 pivot or persevere memo: `phase6_step2_pivot_memo.md` | MOCKED |

STUBBED here means built and passing the offline tests, with no committed run yet. The one part that is not built is reconciling two roadmaps (`N_ARCHITECTS=2`).

Supporting code, which has no run status of its own and is exercised by `tests/`:

| Part | State |
|---|---|
| Two-level state machine, loop budget, pivot limit, persistence, resume | Built, tested |
| Gates: feasibility rubric, roadmap checks, plan check, evidence thresholds, sign-offs | Built, tested |
| Founder decisions and approvals (`choose`, `@requires_approval`, UI routes, CLI) | Built, tested |
| Guardrails (brief, request, output; normalisation, redaction, fail-closed classifier) | Built, tested with `tests/trick_prompts.json` |
| Safe fetch (public addresses only, size cap, fencing, injection logging) and search adapters | Built, tested offline |
| Run budget and stop button | Built, tested |
| Decision log writer and schema check | Built, tested |
| Citation registry, ASSUMPTION stamping, evidence check on verification | Built, tested |
| Model client (Ollama and OpenAI-compatible backends, JSON schema, Pydantic validation, token and cost counting) | Built, tested offline |
| Payments, delivery, refunds, reconciliation, publishing, posting, sign-up counter | MOCKED |

## Roadmap

1. Commit a strong demo run with the founder's own decisions (a run of the weak brief, stopped at feasibility, is in `outputs/`).
2. A real sign-up counter behind the founder's publish approval, so the demand gate can count a measured result.
3. Real counters for Phase 6 (visitors, sign-ups, orders), so the last gate can recommend persevere or pivot from measured results.
4. A fact check on product text: the model can state things about third-party tools that are not true.
5. Stricter source tagging: a larger model, or a second check, to keep sales pages out of `SAY` and loosely related prices out of `PAY`.
6. After the hackathon: a payment provider's test mode behind the same `@requires_approval` gate, more product types, and an evaluation set of briefs with known go and stop answers.

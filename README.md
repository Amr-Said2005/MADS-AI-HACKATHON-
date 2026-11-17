# Agentic Venture Builder

An open-source team of AI agents that takes a founder's idea toward a validated, sellable **digital** product. It works through six phases: Discover, Validate, Build, Commercial setup, Sell/deliver/get paid, and Operate and learn. Before Phase 1 it screens the brief, checks feasibility and agrees a milestone roadmap with the founder. Inside every phase, a planner sets pass criteria before any work starts, code reviews the plan, a checker verifies the work without editing it, code signs off, and the founder decides at a milestone review. Nothing is published, sent, spent or signed without the founder's approval, enforced in code. It runs on a free local open-weight model.

**Current state: Phase 1 is built; Phases 2 to 6 are stubs.** Built and tested:

- the two-level state machine, its gates and loop budget;
- founder decisions and approvals;
- guardrails (with input normalisation, secret redaction and a fail-closed classifier), the decision log and the model client;
- safe fetching (public addresses only, size cap, untrusted-content fencing) and keyless search adapters;
- a run budget and a stop button;
- the mocked sale-to-cash loop;
- the agents up to the Phase 1 milestone: assessor, planner, researcher, scorer, interviewer and checker.

A run screens the brief, checks feasibility, asks the founder to approve the roadmap, runs Phase 1 and stops for the founder's milestone review. After that it pauses at Phase 2, whose agents are STUBBED. The statuses below stay STUBBED until a real run's outputs are committed.

## Quickstart

```bash
docker compose up
```

Then open http://localhost:8000. The first start downloads the model (`qwen3:4b-instruct-2507-q4_K_M`, about 2.5 GB); the page opens straight away and says the model is downloading until it is ready. No `.env` file, API key or GPU is needed.

Command line, as a second approval surface: `docker compose exec app python -m venture run --brief briefs/example_strong.json`, then `python -m venture status RUN_ID` and `python -m venture decide RUN_ID CHOICE`.

Smoke tests (offline, no model): `docker compose exec app pytest`, or locally `pip install -r requirements.txt pytest` then `pytest`.

## What works and what is mocked

**Working** means the step has run for real and its output is committed in `outputs/`. No step has reached that yet.

| Phase | Step | Agent | Output | Status | Notes |
|---|---|---|---|---|---|
| 1 | Brief screen (gate) | guardrails | `phase1_gate_brief_screen.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet |
| 1 | Feasibility check (gate) | assessor | `phase1_gate_feasibility.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet. Rubric scored by code |
| 1 | Milestone roadmap (founder approval) | planner | `phase1_gate_roadmap.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet. Reconciling two roadmaps (checker, `N_ARCHITECTS=2`) is not built |
| 1 | Plan and plan review | planner, gate | `phase1_gate_plan_review.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet. Plan review is code |
| 1 | 1. Opportunities | researcher | `phase1_step1_opportunities.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet |
| 1 | 2. Scoring | scorer | `phase1_step2_scoring.json` | STUBBED | Built, tested offline and tried on the local model; no committed run yet. Code adds the totals and picks |
| 1 | 3. Market and competitors | researcher | `phase1_step3_market.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet |
| 1 | 4. Interview guide (Mom Test) | interviewer | `phase1_step4_interview_guide.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet. A script only; no interviews are run |
| 1 | Verification, evidence gate, founder decision | checker, gate, founder | `phase1_gate_decision.md` | STUBBED | Built, tested offline and tried on the local model; no committed run yet |
| 2 | Plan and plan review | planner, gate | `phase2_gate_plan_review.md` | STUBBED | Plan review is code |
| 2 | 1. Lean Canvas | strategist | `phase2_step1_lean_canvas.md` | STUBBED | |
| 2 | 2. Landing page | strategist | `phase2_step2_landing_page.html` | STUBBED | |
| 2 | 3. Demand test | strategist | `phase2_step3_demand_test.md` | STUBBED | Target set by code before results (5% of 200 cold visitors in 14 days); sign-up results will be MOCKED |
| 2 | Verification, gate, founder decision | checker, gate, founder | `phase2_gate_decision.md` | STUBBED | |
| 2 | 4. Landing page publish log | launcher | `phase2_step4_publish_log.md` | MOCKED | Runs only if the founder approves publishing |
| 3 | Plan and plan review | planner, gate | `phase3_gate_plan_review.md` | STUBBED | Plan review is code |
| 3 | 1. MVP spec | builder | `phase3_step1_mvp_spec.md` | STUBBED | |
| 3 | 2. Product | builder | `phase3_step2_product.md` | STUBBED | |
| 3 | 3. User docs | builder | `phase3_step3_user_docs.md` | STUBBED | |
| 3 | Product tests (gate) | gate | `phase3_gate_tests.md` | STUBBED | |
| 3 | Verification, gate, founder decision | checker, gate, founder | `phase3_gate_decision.md` | STUBBED | |
| 4 | Plan and plan review | planner, gate | `phase4_gate_plan_review.md` | STUBBED | Plan review is code |
| 4 | 1. Pricing | commercial | `phase4_step1_pricing.md` | STUBBED | |
| 4 | 2. Draft policies | commercial | `phase4_step2_policies.md` | STUBBED | Drafts, labelled not legal advice |
| 4 | Verification, gate, founder decision | checker, gate, founder | `phase4_gate_decision.md` | STUBBED | |
| 5 | Plan and plan review | planner, gate | `phase5_gate_plan_review.md` | STUBBED | Plan review is code |
| 5 | 1. Store page | launcher | `phase5_step1_store_page.html` | STUBBED | Publishing is MOCKED |
| 5 | 2. Launch posts | launcher | `phase5_step2_launch_posts.md` | STUBBED | Drafts only; posting is MOCKED |
| 5 | Verification, gate, founder decision | checker, gate, founder | `phase5_gate_decision.md` | STUBBED | Each outbound action is approved on its own |
| 5 | 3. Payment, delivery, refund test | delivery | `payment_test_log.md` | MOCKED | In-memory ledger (payments, delivery, refunds, reconcile); no provider, not even sandbox |
| 5 | 4. Publish log | launcher | `phase5_step4_publish_log.md` | MOCKED | Records what would go out after approval |
| 6 | Plan and plan review | planner, gate | `phase6_gate_plan_review.md` | STUBBED | Plan review is code |
| 6 | 1. Support FAQ | operator | `phase6_step1_support_faq.md` | STUBBED | |
| 6 | 2. Pivot or persevere memo | operator | `phase6_step2_pivot_memo.md` | STUBBED | |
| 6 | Verification, gate, founder decision | checker, gate, founder | `phase6_gate_decision.md` | STUBBED | |

The model is MOCKED only in tests (`LLM_MODE=mock`); the UI shows a MOCKED banner then, and nothing from that mode is ever copied to `outputs/` or `decision_log.json`.

## Architecture

A Python state machine (`orchestrator.py`) runs in two levels.

- **Before Phase 1:** the outer level runs a brief screen, a feasibility rubric scored by code, and a milestone roadmap that the founder approves.
- **Inside each phase:** the inner loop runs plan, plan review, make, verify, sign-off and milestone review. The `planner` sets criteria before any work, code reviews the plan, the `checker` verifies in one call without editing, `gates.py` decides in code, and the founder chooses at each milestone. "Continue" is offered only when the gate said go.
- **Budgets:** each back-edge (revise, defects, repeat) fires at most twice per phase; a run stops at `MAX_MODEL_CALLS` or `MAX_RUN_SECONDS`, and the founder can stop it at any time.
- **Outbound actions:** each publish, send or payment action has its own approval in `approval.py`, behind `@requires_approval`.
- **Guardrails:** `guardrails.py` screens the brief, every founder instruction and every generated file.
- **Sources:** agents can cite only URLs the run actually fetched with a 2xx answer and non-empty text (`sources.py`); code labels anything else `ASSUMPTION`. Fetched text reaches agents as fenced, untrusted data.

Details and both diagrams: [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

- **Default:** `qwen3:4b-instruct-2507-q4_K_M`, an open-weight model (2.5 GB), run on CPU by the Ollama container in `docker-compose.yml`. The client uses Ollama's native chat API with a JSON schema, and every reply is validated.
- **GPU desktop:** `LLM_MODEL=gpt-oss:20b docker compose -f docker-compose.yml -f docker-compose.gpu.yml up`.
- **Hosted endpoint:** for a laptop demo, use a free hosted open-weight model. Set `LLM_BACKEND=openai`, `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (see `.env.example`). No paid service or payment card is required.
- **Research:** pages are fetched directly. Search goes through a local SearXNG container, then keyless APIs (Hacker News, Stack Exchange, MediaWiki, GitHub). No paid search API is used, and Reddit is not used.

## Cost per run

About $0.00 per run: the model runs locally, so there is no per-token charge. A trial run of the strong example brief on the local model, from the brief screen to the Phase 1 milestone, used about 17,000 tokens in 8 model calls. Rework loops add to that, up to the run budget (`MAX_MODEL_CALLS`). `python -m venture status RUN_ID` prints the count for any run. With a priced endpoint, set `LLM_PRICE_IN_PER_MTOK` and `LLM_PRICE_OUT_PER_MTOK` and the cost is logged per action.

## Demo video

Not recorded yet.

## What our system will NOT do

- Promise income or success.
- Create fake reviews, testimonials, sign-ups or countdown timers.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money by itself, create accounts, or store passwords or card numbers. Payments, delivery and refunds are MOCKED.
- Publish, send, spend or sign anything without the founder's approval.
- Work on physical goods; financial, investment or crypto products; or health or legal advice products. Borderline ideas are stopped with the reason stated.
- Cite a source it did not fetch in the same run, or follow instructions found inside a fetched page.
- Store a password or card number typed into a brief, an instruction or a note: it is replaced with `[REDACTED]` first.
- Hide core features behind a paywall of ours.

## How the project stays alive after the hackathon

- **Maintainers:** the contributors of this repository. Issues and pull requests are welcome.
- **Roadmap:** see [ARCHITECTURE.md](ARCHITECTURE.md#roadmap): build the Phase 1 and 2 agents first, then Build, Commercial and Launch, then real payment-provider test mode behind the same approval gate.
- **Community:** the agents are plain Python modules with one prompt file each, so a new agent or a new product type is a small pull request.
- **Funding:** none needed to run it. It uses free local models and no paid services; the MIT licence keeps it free to use and fork.

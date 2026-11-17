# Agentic Venture Builder

An open-source team of AI agents that takes a founder's idea toward a validated, sellable **digital** product through six gated phases: Discover, Validate, Build, Commercial setup, Sell/deliver/get paid, and Operate and learn. It runs on a free local open-weight model, and nothing is published, sent, spent or signed without the founder's approval, enforced in code.

## Quickstart

```bash
docker compose up
```

Then open http://localhost:8000. The first start downloads the model (`qwen3:4b-instruct-2507-q4_K_M`, about 2.5 GB); the page opens straight away and says the model is downloading until it is ready. No `.env` file, API key or GPU is needed.

- **Command line** (a second approval surface): `docker compose exec app python -m venture run --brief briefs/example_strong.json`, then `python -m venture status RUN_ID` and `python -m venture decide RUN_ID CHOICE`.
- **Smoke tests** (offline, no model): `docker compose exec app pytest`, or locally `pip install -r requirements.txt pytest` then `pytest`.
- **Example briefs:** `briefs/example_strong.json`, `briefs/example_weak.json` and `briefs/thin_evidence.json`.
- **Your own idea:** fill in the form on the home page (idea, who it is for, skills, budget, weekly hours, values, and optionally a time frame and delegated hours). It goes through the same scope screen.

## What works and what is mocked

| Phase | Step | Agent | Output | Status | Notes |
|---|---|---|---|---|---|
| 1 | Scope screen (gate) | guardrails | `phase1_gate_brief_screen.md` | Working | Committed run of the weak brief |
| 1 | Feasibility check (warnings, founder decides) | assessor | `phase1_gate_feasibility.md` | Working | Committed run of the weak brief at commit `3a9a732`, when the check still stopped a run by itself. It now only warns, and a run of the current check is not committed yet |
| 1 | Milestone roadmap (founder approval) | planner | `phase1_gate_roadmap.md` | STUBBED | No committed run yet. Reconciling two roadmaps (checker, `N_ARCHITECTS=2`) is not built |
| 1 | Plan and plan review | planner, gate | `phase1_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 1 | 1. Opportunities | researcher | `phase1_step1_opportunities.md` | STUBBED | No committed run yet |
| 1 | 2. Scoring | scorer | `phase1_step2_scoring.json` | STUBBED | No committed run yet. Code adds the totals and picks |
| 1 | 3. Market and competitors | researcher | `phase1_step3_market.md` | STUBBED | No committed run yet |
| 1 | 4. Interview guide (Mom Test) | interviewer | `phase1_step4_interview_guide.md` | STUBBED | No committed run yet. A script only; no interviews are run |
| 1 | Verification, evidence gate, founder decision | checker, gate, founder | `phase1_gate_decision.md` | STUBBED | No committed run yet |
| 2 | Plan and plan review | planner, gate | `phase2_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 2 | 1. Lean Canvas | strategist | `phase2_step1_lean_canvas.md` | STUBBED | No committed run yet. Entries cite Phase 1 sources or are labelled ASSUMPTION |
| 2 | 2. Landing page | strategist | `phase2_step2_landing_page.html` | STUBBED | No committed run yet. A draft: the form is not connected |
| 2 | 3. Demand test | strategist | `phase2_step3_demand_test.md` | MOCKED | Target set by code before results (5% of 200 cold visitors in 14 days). The sign-up counter (`demand.py`) is a mock that reads zero, so demand is reported as not measured |
| 2 | Verification, gate, founder decision | checker, gate, founder | `phase2_gate_decision.md` | STUBBED | No committed run yet. Publishing is a separate approval |
| 2 | 4. Landing page publish log | launcher | `phase2_step4_publish_log.md` | MOCKED | Runs only if the founder approves publishing |
| 3 | Plan and plan review | planner, gate | `phase3_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 3 | 1. MVP spec | builder | `phase3_step1_mvp_spec.md` | STUBBED | No committed run yet |
| 3 | 2. Product | builder | `phase3_step2_product.md` | STUBBED | No committed run yet. Text products only |
| 3 | 3. User docs | builder | `phase3_step3_user_docs.md` | STUBBED | No committed run yet |
| 3 | Product tests (gate) | gate | `phase3_gate_tests.md` | STUBBED | No committed run yet. Four acceptance tests run by code |
| 3 | Verification, gate, founder decision | checker, gate, founder | `phase3_gate_decision.md` | STUBBED | No committed run yet |
| 4 | Plan and plan review | planner, gate | `phase4_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 4 | 1. Pricing | commercial | `phase4_step1_pricing.md` | STUBBED | No committed run yet. A recommendation for the founder |
| 4 | 2. Draft policies | commercial | `phase4_step2_policies.md` | STUBBED | No committed run yet. Drafts, labelled not legal advice |
| 4 | Verification, gate, founder decision | checker, gate, founder | `phase4_gate_decision.md` | STUBBED | No committed run yet |
| 5 | Plan and plan review | planner, gate | `phase5_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 5 | 1. Store page | launcher | `phase5_step1_store_page.html` | STUBBED | No committed run yet. A draft: the buy button is not connected; publishing is MOCKED |
| 5 | 2. Launch posts | launcher | `phase5_step2_launch_posts.md` | STUBBED | No committed run yet. Drafts only; posting is MOCKED |
| 5 | Verification, gate, founder decision | checker, gate, founder | `phase5_gate_decision.md` | STUBBED | No committed run yet. Each outbound action is approved on its own |
| 5 | 3. Payment, delivery, refund test | delivery | `payment_test_log.md` | MOCKED | In-memory ledger (payments, delivery, refunds, reconcile); no provider, not even sandbox |
| 5 | 4. Publish log | launcher | `phase5_step4_publish_log.md` | MOCKED | Records what would go out after approval |
| 6 | Plan and plan review | planner, gate | `phase6_gate_plan_review.md` | STUBBED | No committed run yet. Plan review is code |
| 6 | 1. Support FAQ | operator | `phase6_step1_support_faq.md` | STUBBED | No committed run yet. A draft for the founder to check |
| 6 | 2. Pivot or persevere memo | operator | `phase6_step2_pivot_memo.md` | MOCKED | The metrics come from the MOCKED counter (`demand.py`), which reads zero, so the memo reports them as not measured. Code makes the recommendation |
| 6 | Verification, gate, founder decision | checker, gate, founder | `phase6_gate_decision.md` | STUBBED | No committed run yet. Persevere needs measured metrics |

- **Working:** the step ran for real and its output is committed in `outputs/`. So far that is the scope screen and the feasibility check, from a run of `briefs/example_weak.json` made at commit `3a9a732`. At that commit the feasibility check stopped the run by itself; it now gives warnings and the founder decides.
- **STUBBED:** the step is built and passes the offline tests, but no committed run has reached it yet. The one part that is not built is reconciling two roadmaps (`N_ARCHITECTS=2`).
- **MOCKED:** the step runs against a labelled mock. Nothing is published, posted or charged, and the sign-up counter reads zero.

A run screens the idea for scope, shows the founder its feasibility warnings and waits for their choice, asks the founder to approve the roadmap, then runs Phases 1 to 6 and stops for the founder's review at each milestone. Whether a gate says go depends on the evidence the run finds: trial runs of the same brief ended in both GO and STOP at Phase 1. Demand is never reported as validated, and the last gate recommends persevere only on measured metrics. The counter is MOCKED, so a full run of this prototype ends with a STOP recommendation, and the decision file says it is not a verdict on the idea.

The model is MOCKED only in tests (`LLM_MODE=mock`); the UI shows a MOCKED banner then, and nothing from that mode is ever copied to `outputs/` or `decision_log.json`.

## Architecture

`orchestrator.py` is a two-level state machine in which lookup tables, not the model, choose every next step. Before Phase 1 it stops out-of-scope ideas with one fixed reply, shows feasibility warnings computed in code for the founder to decide on, and asks the founder to approve a six-milestone roadmap. Inside each phase a planner sets pass criteria, the makers produce the files, a checker verifies them without editing, `gates.py` signs off in code with go, repeat, pivot or stop, and the founder decides at a milestone review. Publishing, posting and payment actions each need their own founder approval (`approval.py`), and all of them are MOCKED. `guardrails.py` screens the brief, every founder instruction and every generated file, and agents can cite only pages the run fetched (`sources.py`); anything else is labelled `ASSUMPTION`. The diagrams, gates and data flow are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

Open-weight model: `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB), run locally by the Ollama container in `docker-compose.yml`. No paid service, API key or payment card is required.

- **Default:** CPU only. The client uses Ollama's native chat API with a JSON schema, and every reply is validated.
- **GPU desktop:** `LLM_MODEL=gpt-oss:20b docker compose -f docker-compose.yml -f docker-compose.gpu.yml up` (open-weight, 16 GB of video memory).
- **Committed run:** produced by `gpt-oss:20b` on a 16 GB GPU. An earlier committed run of the same brief (commit `acbe866`) was produced by `qwen3:4b-instruct-2507-q4_K_M` on CPU in 35 seconds.
- **Hosted endpoint (optional):** for a laptop demo, a free hosted open-weight model. Set `LLM_BACKEND=openai`, `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (see `.env.example`).
- **Research:** pages are fetched directly. Search goes through a local SearXNG container, then keyless APIs (Hacker News, Stack Exchange, MediaWiki, GitHub). No paid search API is used, and Reddit is not used.

## Cost per run

About $0.00 per run: local model `gpt-oss:20b`, 5,008 tokens in 2 model calls for the committed run (the weak brief up to the feasibility check, at commit `3a9a732`; 9 seconds on a 16 GB GPU).

The model runs locally, so there is no per-token charge. On CPU with the default model, the same brief took 35 seconds and 2,527 tokens in 1 call (commit `acbe866`); that model wrote about 17 tokens a second on a 32-thread desktop CPU, so a full run is estimated at 12 to 17 minutes there, which can exceed the 900-second run budget (`MAX_RUN_SECONDS`; raise it in `.env`). Trial runs of the strong example brief on the local model, which are not committed, used about 55,000 tokens in 33 model calls from the brief screen through the Phase 6 milestone (about 18,000 of them up to the Phase 1 milestone), and about 80,000 tokens in 43 calls when more rework was needed. Rework loops add to that, up to the run budget (`MAX_MODEL_CALLS`). `python -m venture status RUN_ID` prints the count for any run. With a priced endpoint, set `LLM_PRICE_IN_PER_MTOK` and `LLM_PRICE_OUT_PER_MTOK` and the cost is logged per action.

## Demo video

Not recorded yet.

## What our system will NOT do

- Promise income or success.
- Create fake reviews, testimonials, sign-ups or countdown timers.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money by itself, create accounts, or store passwords or card numbers. Payments, delivery and refunds are MOCKED.
- Publish, send, spend or sign anything without the founder's approval.
- Go past a failed evidence gate by itself, or hide that the founder did: continuing against a pivot or stop needs a signed waiver with a written reason, and every later file says so. The scope screen, the never-list and publish or send approvals cannot be waived.
- End a run by itself after the feasibility check: it pauses and asks the founder instead. Only an out-of-scope idea still stops at once.
- Work on health or medical products, financial or investment products (including crypto), legal services, or physical goods. An idea in one of these stops at the scope screen with one fixed reply, and the founder cannot override it.
- Present generated product text as checked: the product, user docs, store page and launch posts start with "Draft: facts not verified. Founder must review before use."
- Cite a source it did not fetch in the same run, or follow instructions found inside a fetched page.
- Store a password or card number typed into a brief, an instruction or a note: it is replaced with `[REDACTED]` first.
- Hide core features behind a paywall of ours.

## How the project stays alive after the hackathon

- **Maintainers:** the contributors of this repository. Issues and pull requests are welcome.
- **Roadmap:** see [ARCHITECTURE.md](ARCHITECTURE.md#roadmap): a committed strong demo run, real sign-up and sales counters behind the founder's publish approval, a fact check on product text, then a payment provider's test mode behind the same approval gate.
- **Community:** the agents are plain Python modules with one prompt file each, so a new agent or a new product type is a small pull request.
- **Funding:** none needed to run it. It uses free local models and no paid services; the MIT licence keeps it free to use and fork.

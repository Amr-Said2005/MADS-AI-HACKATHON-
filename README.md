# Agentic Venture Builder

An open-source team of AI agents that takes a founder's idea toward a validated, sellable **digital** product. It works through six phases (Discover, Validate, Build, Commercial setup, Sell/deliver/get paid, Operate and learn), with a gate after every phase that returns go, repeat, pivot or stop. Nothing is published, sent, spent or signed without the founder's approval, enforced in code. It runs on a free local open-weight model.

**Current state: scaffold.** The state machine, gates, founder approval, guardrails, decision log, model client and mocked sale-to-cash loop are built and tested. The agents are stubs. A run passes the brief screen, then pauses at the first STUBBED step.

## Quickstart

```bash
docker compose up
```

Then open http://localhost:8000. The first start downloads the model (`qwen3:8b`, about 5 GB); the page opens straight away and says when the model is ready. No `.env` file, API key or GPU is needed.

Command line, as a second approval surface: `docker compose exec app python -m venture run --brief briefs/example_strong.json`.

Smoke tests (offline, no model): `docker compose exec app pytest`, or locally `pip install -r requirements.txt pytest` then `pytest`.

## What works and what is mocked

**Working** means the step has run for real and its output is committed in `outputs/`. No step has reached that yet.

| Phase | Step | Agent | Output | Status | Notes |
|---|---|---|---|---|---|
| 1 | Brief screen (gate) | guardrails | `phase1_gate_brief_screen.md` | STUBBED | Code built and tested; no committed run yet |
| 1 | 1. Opportunities | researcher | `phase1_step1_opportunities.md` | STUBBED | |
| 1 | 2. Scoring | scorer | `phase1_step2_scoring.json` | STUBBED | |
| 1 | 3. Market and competitors | researcher | `phase1_step3_market.md` | STUBBED | |
| 1 | 4. Interview guide (Mom Test) | interviewer | `phase1_step4_interview_guide.md` | STUBBED | |
| 1 | Evidence gate | gate | `phase1_gate_decision.md` | STUBBED | Gate logic built and tested; waits on Phase 1 agents |
| 2 | 1. Lean Canvas | strategist | `phase2_step1_lean_canvas.md` | STUBBED | |
| 2 | 2. Landing page | strategist | `phase2_step2_landing_page.html` | STUBBED | |
| 2 | 3. Demand test | strategist | `phase2_step3_demand_test.md` | STUBBED | Target set by code; sign-up results will be MOCKED |
| 2 | Gate and founder approval | gate | `phase2_gate_decision.md` | STUBBED | Approval blocking built and tested |
| 3 | 1. MVP spec | builder | `phase3_step1_mvp_spec.md` | STUBBED | |
| 3 | 2. Product | builder | `phase3_step2_product.md` | STUBBED | |
| 3 | 3. User docs | builder | `phase3_step3_user_docs.md` | STUBBED | |
| 3 | Tests gate | gate | `phase3_gate_tests.md` | STUBBED | |
| 4 | 1. Pricing | commercial | `phase4_step1_pricing.md` | STUBBED | |
| 4 | 2. Draft policies | commercial | `phase4_step2_policies.md` | STUBBED | Drafts, labelled not legal advice |
| 5 | 1. Store page | launcher | `phase5_step1_store_page.html` | STUBBED | Publishing is MOCKED |
| 5 | 2. Launch posts | launcher | `phase5_step2_launch_posts.md` | STUBBED | Drafts only; posting is MOCKED |
| 5 | 3. Payment, delivery, refund test | delivery | `payment_test_log.md` | MOCKED | In-memory ledger; no provider, not even sandbox |
| 5 | 4. Publish log | launcher | `phase5_step4_publish_log.md` | MOCKED | Records what would go out after approval |
| 6 | 1. Support FAQ | operator | `phase6_step1_support_faq.md` | STUBBED | |
| 6 | 2. Pivot or persevere memo | operator | `phase6_step2_pivot_memo.md` | STUBBED | |

The model is MOCKED only in tests (`LLM_MODE=mock`); the UI shows a MOCKED banner then, and nothing from that mode is ever copied to `outputs/` or `decision_log.json`.

## Architecture

A Python state machine (`orchestrator.py`) runs one state per phase. Each state calls its agents (`agents/<role>.py`, prompts in `prompts/`) and ends in a gate (`gates.py`) that returns go, repeat, pivot or stop; thresholds are fixed before a run. Phases 2, 4 and 5 then wait for the founder in the web UI or CLI (`approval.py`); every outbound function is wrapped in `@requires_approval`. Guardrails (`guardrails.py`) screen the brief, every founder instruction and every generated file. Agents can cite only URLs the run actually fetched (`sources.py`); anything else is labelled `ASSUMPTION` by code. Every action is appended to a decision log by `decision_log.py`. Details: [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

- Default: `qwen3:8b`, an open-weight model, run locally by the Ollama container in `docker-compose.yml`. CPU works; a GPU is faster but not required.
- On a machine with a larger GPU: `LLM_MODEL=gpt-oss:20b docker compose up`.
- Any other OpenAI-compatible endpoint works by setting `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (see `.env.example`). No paid service is required.
- Research uses public web pages fetched directly; no paid search API.

## Cost per run

About $0.00 per run: the model runs locally, so there is no per-token charge. The token count per run will be added here from the first real run (`python -m venture status RUN_ID` prints it). With a priced endpoint, set `LLM_PRICE_IN_PER_MTOK` and `LLM_PRICE_OUT_PER_MTOK` and the cost is logged per action.

## Demo video

Not recorded yet.

## What our system will NOT do

- Promise income or success.
- Create fake reviews, testimonials, sign-ups or countdown timers.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money by itself, create accounts, or store passwords or card numbers. Payments, delivery and refunds are MOCKED.
- Publish, send, spend or sign anything without the founder's approval.
- Work on physical goods; financial, investment or crypto products; or health or legal advice products. Borderline ideas are stopped with the reason stated.
- Cite a source it did not fetch in the same run.
- Hide core features behind a paywall of ours.

## How the project stays alive after the hackathon

- **Maintainers:** the contributors of this repository. Issues and pull requests are welcome.
- **Roadmap:** see [ARCHITECTURE.md](ARCHITECTURE.md#roadmap): build the Phase 1 and 2 agents first, then Build, Commercial and Launch, then real payment-provider test mode behind the same approval gate.
- **Community:** the agents are plain Python modules with one prompt file each, so a new agent or a new product type is a small pull request.
- **Funding:** none needed to run it. It uses free local models and no paid services; the MIT licence keeps it free to use and fork.

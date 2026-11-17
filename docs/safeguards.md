# Safeguards

The table of rules, mechanisms and tests is in [ARCHITECTURE.md](../ARCHITECTURE.md#gates). This page describes how each mechanism works.

## Structural limits (the system cannot do it at all)

- No agent holds a tool that publishes, sends, spends or signs. Those exist only as `@requires_approval` functions, and every one is a labelled mock.
- No payment SDK, mail client or social-media client is in the dependencies; a test fails if one appears.
- The fetch tool is GET only, `http`/`https` only, with a timeout and a size cap. It resolves each host and refuses private, loopback, link-local and other non-public addresses, on every redirect hop.
- No field stores a password or card number. Briefs, instructions and founder notes are scanned, and matches are replaced with `[REDACTED]` before anything is stored or logged.
- Run budget: `MAX_MODEL_CALLS`, `MAX_RUN_SECONDS` and the loop budgets. Exceeding one ends the run as STOPPED with the reason logged. The founder's stop button is always available (UI and `python -m venture stop`).

## Untrusted content

Text fetched from the web is data. It reaches agents fenced between per-run random delimiters, behind a fixed preamble that says so, truncated, and never inside a system prompt. Instruction-like text inside a page is logged as `injection_suspected`. Next states come from lookup tables, and outbound actions need an approval record, so text in a page or a brief cannot trigger an action.

## How checks decide

- Text is normalised first: NFKC, zero-width and tag characters removed, lowercase, plus a leetspeak variant.
- Letters written one by one are joined again before the rules run.
- The rules are written for English. The review rule also knows a few common phrasings in Arabic, French, Spanish and German; other languages are not covered.
- Deterministic rules come first. A model classifier is used only for ambiguous founder instructions, and any error or unparseable reply means refuse (fail closed).
- A refusal blocks only the offending request or file; the run goes on and the founder sees what was refused and why.
- Founder approval cannot unlock the never-list, whatever the framing (role-play, "for testing", "the founder authorises it").
- An instruction that tries to skip an approval, or to sign or accept terms for the founder, is refused. Approval is a record made in the app, never text.

## Outbound actions

Publish, send and spend actions in Phases 2 and 5 are separate decisions on the milestone screen. Each has its own action id, which hashes the exact content approved, so approving a milestone never approves a publication by implication. Every one of them is MOCKED in this prototype.

> Phase 3, step 1: mvp spec | Agent: builder | Model: gpt-oss:20b | Run started: 2026-11-17T19:39:36Z | Continued against a STOP recommendation at M1: someone already pays, claims mostly sourced

# MVP spec: EE Study Hub

- Product type: courses
- Format: a Markdown guide with three sections: Digital Notes, Summaries, and Cheat Sheets, each containing tables and key concepts for electrical engineering courses
- For: Electrical Engineering undergraduates

## Sections

1. **Digital Notes**: Provides comprehensive, lecture‑style notes covering core topics such as circuit analysis, electromagnetics, and signal processing, organized by semester and course code.
2. **Summaries**: Offers concise, one‑page summaries of each chapter, highlighting definitions, formulas, and example problems for quick review before exams.
3. **Cheat Sheets**: Delivers printable cheat sheets with essential equations, conversion tables, and troubleshooting checklists for on‑the‑go reference during labs and assignments.

## Left out of the smallest version

- In‑depth video tutorials
- Interactive quizzes or adaptive learning paths
- Personalized study plans or progress tracking

## Acceptance tests (run by code on the finished product)

- every section in the spec is present: each spec section title appears as a section of the product
- every section has enough content: each section has at least 250 characters (threshold: ASSUMPTION)
- the product is short enough to review: at most 20000 characters in total (threshold: ASSUMPTION)
- no placeholder text: no 'lorem ipsum', 'TODO', 'TBD' or '[insert ...]' left in the text

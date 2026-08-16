# Bursa Earnings Brief Assistant

A human-in-the-loop tool that helps a journalist review quarterly earnings PDFs. It extracts
figures, validates them deterministically, and generates a draft report for a person to check.
It is **not** an autonomous publishing bot, and it must never become one.

## Non-negotiables

These are product constraints, not preferences. Do not relax them without being asked directly.

- **Human review is mandatory.** Extracted figures stage in `pending_reviews` and only reach
  `pdf_metadata` when a person approves. Never write to `pdf_metadata` from an automated path.
- **No automatic publishing.** Nothing leaves this machine without a human action.
- **Every financial figure must stay source-traceable** back to the document it came from.
- **Never scrape KLSE Screener.** Not as a fallback, not for one field, not "just to compare".
- **No proxy rotation, CAPTCHA bypass, stealth scraping, or rate-limit evasion.** If a source
  blocks us, we stop and log it — we do not try harder.
- **Bursa monitoring stays low-frequency and identifies itself.** Hourly at most, a descriptive
  User-Agent with a contact address, `Retry-After` honoured, and the run aborts on 403/429.
  Treat the endpoint as undocumented and replaceable: keep parsing behind a swappable seam.

## Honesty rules

- **Never state an evaluation pass rate that was not actually produced by a run**, and always say
  whether a figure came from a *live* run or an *offline replay* of stored model outputs. The
  current numbers live in `docs/PROJECT_STATE.md` — read them there rather than recalling them.
  This has already caused one documentation defect; do not repeat it.
- **Extraction is not deterministic.** The same PDF can extract correctly on one run and wrongly on
  the next. Never write a test expectation that requires the model to make a particular mistake.
- The evaluation suite costs money and sends local test PDFs to OpenAI. **Ask before running it.**
- Warnings in `validation_warnings` are script-generated. The LLM never writes that field.

## Where truth lives

| Document | Status |
|---|---|
| `docs/PROJECT_STATE.md` | **Authoritative** current architecture, schema, and routes. |
| `CLAUDE.md` (this file) | Stable rules. Changes rarely. |
| `REPORT_SOURCE_PACK.md` | Evidence pack for a written report. **Not** an engineering spec. |
| `docs/archive/PROJECT_CONTEXT_phase1.md` | Historical. Describes the pre-review-gate design. |

`graphify-out/GRAPH_REPORT.md` is a generated code map — useful for navigation, rebuild with
`/graphify`, not committed.

## Commands

```bash
.venv/Scripts/python.exe app.py           # serve on http://localhost:5000
.venv/Scripts/python.exe -m pytest        # offline test suite — must make no network calls
```

The eval harness runs via `POST /evaluate` or `run_evaluation()`. See the honesty rules above.

## Conventions

- **Migrations** go in `init_db()` using the existing `CREATE TABLE IF NOT EXISTS` plus
  `ALTER TABLE`-for-missing-columns pattern. Additive only; the DB migrates itself on startup.
- **Tests make no network calls.** Anything touching a live source belongs behind an explicit
  opt-in flag and stays out of the default suite.
- Match the surrounding code style: plain stdlib, no frameworks beyond Flask/Pydantic/ReportLab.
- Never commit `.env`. It was exposed once already and the history had to be scrubbed.

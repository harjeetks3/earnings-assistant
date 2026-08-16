# Project State

Authoritative description of what the code currently does. Rewrite this file when it drifts —
don't patch around it. Rules that don't change live in `CLAUDE.md`.

**Last verified:** 2026-08-16, against commit `74255ad`.

---

## Pipeline

```
PDF in ──> extract_pdf_text()        pypdf, pages joined with blank lines
       ──> analyse_earnings()        gpt-5.4-mini, temperature 0, JSON mode, 100k char cap
       ──> _audit_unit_scale()       verifies the model's arithmetic against the printed figures
       ──> compute_qoq_yoy()         DB history first, report's own comparatives as fallback
       ──> validate_analysis()       Pydantic + cross-field rules + metadata contradiction
       ──> pending_reviews           STAGING ONLY — not the record of account
       ──> generate_report_pdf()     draft the reviewer downloads and reads
       ──> [ human approves ]
       ──> pdf_metadata              record of account
```

The order matters: the unit audit runs **before** `compute_qoq_yoy()`, so growth rates derive from
corrected figures.

## Database (SQLite, `pdfs.db`)

Auto-migrated on startup by `init_db()` (`app.py:1246`).

### `pdf_metadata` — record of account, written only on approval
File facts (`filename`, `file_size`, `sha256`, `pages`, `title`, `author`, `creator`,
`uploaded_at`), the LLM-extracted fields (`company_name`, `quarter_end_date`, `fiscal_quarter`,
`fiscal_year`, `currency`, `unit_raw`, six monetary fields, `management_commentary`,
`outlook_summary`, `confidence_score`), plus `analysis_error`, `validation_warnings` (JSON array
string), the four growth fields, and `report_path`.

### `pending_reviews` — staging, deleted on approval
Same file facts plus `extracted_data` (whole analysis as JSON), `report_path`, `attempt_count`,
`extra_instructions`, `downloaded_at`, `created_at`, `updated_at`.

`downloaded_at` gates approval: you cannot approve a report you have not downloaded.

> **Known gap:** `approve_pending()` deletes the pending row (`app.py:1605`), so review history is
> lost at the moment of approval. Phase 2 adds `review_events` to fix this.

## Routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Single-page UI |
| POST | `/upload` | Ingest a PDF, stage a pending review, generate a draft report |
| GET | `/pending` | List reviews awaiting a decision |
| GET | `/pending/<id>/report` | Download the draft; stamps `downloaded_at` |
| POST | `/pending/<id>/approve` | Promote to `pdf_metadata` |
| POST | `/pending/<id>/reject` | Re-run extraction, optionally with reviewer instructions |
| DELETE | `/pending/<id>` | Discard without saving |
| GET | `/pdfs` | List approved entries |
| GET | `/pdfs/<id>/report` | Download the final report |
| DELETE | `/pdfs/<id>` | Delete an approved entry and its file |
| POST | `/evaluate` | Run the 5-case synthetic suite (**costs money**) |
| GET | `/eval_results/<file>` | Download a stored evaluation JSON |

## Validation

`validate_analysis(raw, pdf_meta=None)` (`app.py:510`) runs three passes: Pydantic types, then
cross-field rules (quarter, fiscal year range, date format and year agreement, ISO-4217 currency,
non-negative revenue, PBT vs revenue, confidence range, all-null figures), then a check that the
PDF's embedded metadata does not contradict the extracted company name.

PBT exceeding revenue is flagged as *unusual but legitimate* (one-off disposal gains), not as an
error.

## Unit-scale audit

`_audit_unit_scale()` (`app.py:427`) exists because the model reads the unit label correctly but
gets the arithmetic wrong often enough to matter — two of five eval cases were 1000× low.

It converts each monetary field back to as-printed form and counts how many it can locate in the
PDF text, scoring the model's own factor against ×1000 and ÷1000. It rescales **all six together**
or none, and only when a rival factor wins outright, anchors at least three figures, and carries a
two-thirds majority of the figures present.

Guards, each of which exists because something went wrong:

- Anchors use `(?<![\d,.])…(?![\d,.])` so `340` does not match inside `340,000`.
- An anchor only counts if the as-printed figure is ≥ 100. Small figures like `0.3` or a bare `0`
  appear everywhere and confirm nothing.
- A PDF carrying our own generated-report footer is flagged, because its figures are already
  normalised while its header still quotes the original unit label.

> **Live incident (fixed, `74255ad`):** a generated review report was re-uploaded as a source
> document. Because `'000` maps to factor 0.001 — also one of the candidate shifts — that candidate
> predicted a printed form identical to the model's own output, matched it, and rescaled correct
> figures to near zero. The magnitude and quorum guards above are the fix. Covered by
> `test_unit_scale.py`.

## Evaluation status

Be precise about which number you are quoting.

| Run | Result |
|---|---|
| `evaluation_20260708_090200.json` — live, **before** the unit-scale fix | **3/5** |
| `evaluation_20260816_164552.json` — live, **after** the fix | **4/5** |
| Replay of both stored runs against the corrected expectations | **5/5 each** |
| Live run under the corrected expectations | **Not yet performed** |

In the 2026-08-16 live run, **all five cases produced correct figures.** The single failure was
case 04, and it failed only on the warning set: the fixture required the unit-scale correction
warning, but the live model got Yamato's arithmetic right, so the audit had nothing to correct and
stayed silent. The test was effectively demanding that the model make a mistake.

That expectation is now split into `expected_validation_warnings` (required) and
`conditional_validation_warnings` (permitted, not required); the scale-correction warning moved to
the latter for cases 04 and 05. Anything outside both lists still fails, so a genuinely new warning
is caught. Replaying both stored runs — one where the model erred, one where it didn't — gives 5/5
under the corrected fixture, which is what shows the expectation is no longer model-dependent.

A replay re-runs stored model outputs through the current `_evaluate_case()`. It exercises the
audit, validation and comparison code paths, but not how the live model responds today.

## Tests

`test_unit_scale.py` — plain asserts, no runner, no API calls: `python test_unit_scale.py`.
Covers the false-positive rescale, a genuine 1000× slip, and a clean extraction.

## Known limitations

- Page-level evidence is not preserved; pages are joined into one text block, so a figure cannot
  currently be traced to a page. Phase 2 addresses this.
- Anchoring assumes `,` thousands separators. Documents using `.` or spaces fall through to the
  "scale unverified" warning rather than being mis-corrected.
- QoQ/YoY DB lookup matches company names case-insensitively; slightly different extracted names
  for the same company will miss and fall back to the report's own comparatives.
- `reports/` holds leftover artifacts (`report_35`–`report_39`) from earlier sessions with no
  matching DB rows. `report_1.pdf` is current and belongs to approved entry #1.

## Phase 2 — planned, not yet built

Bursa announcement monitoring: discovery → watchlist filter → dedup → attachment hashing → PDF
verification → the existing pending-review workflow. Monitoring runs as a standalone script and only
ever *queues* candidates; a human triggers extraction. New tables (`companies`, `announcements`,
`attachments`, `metric_observations`, `evidence`, `review_events`) are designed but **not yet
created**. See the approved plan for the sequence.

# Project State

Authoritative description of what the code currently does. Rewrite this file when it drifts —
don't patch around it. Rules that don't change live in `CLAUDE.md`.

**Last verified:** 2026-08-16, against commit `74255ad`.

---

## Pipeline

`ingest_pdf_bytes()` is the **only** way a PDF enters the system. Both `POST /upload` and the
monitored-attachment path call it, so a discovered PDF cannot reach a second, weaker route that
skips the unit-scale audit or the review gate. `upload()` is a thin HTTP wrapper over it.

```
PDF in ──> ingest_pdf_bytes()        hash, dedupe, save  (shared entry point)
       ──> extract_pdf_text()        pypdf, pages joined with blank lines
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

`approve_pending()` still deletes the pending row, but the history no longer dies with it:
`record_review_event()` appends to `review_events` on ingest, download, reject, discard and
approval. An approval writes two rows — one against the disappearing pending id and one against
the entry it became — so the trail stays followable across the handover. Event logging is
best-effort and never blocks or rolls back the action it describes.

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
| GET | `/discovered` | Bursa filings the monitor found and verified |
| POST | `/discovered/<id>/extract` | Human-triggered extraction of a discovered filing (**costs money**) |
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

## Evidence and source traceability

Every monetary figure that has a value gets a `metric_observations` row and an `evidence` row.
The model proposes a source quote; **code confirms it**, because a citation is exactly the kind
of thing an LLM produces fluently and wrongly.

| `match_method` | Meaning |
|---|---|
| `llm_verified` | The model's quote was found verbatim **and contains the figure** |
| `deterministic` | No usable quote, so code located the figure under the right caption |
| `prior_entry` | The value came from a previously approved filing, not this document |
| `unverified` | None worked — provenance is genuinely missing, and says so |

Two rules earn their keep here, both from cases that actually occurred:

- **A quote must contain its own figure.** Locating a sentence only proves the sentence exists. A
  real but mis-keyed quote — boilerplate, another metric's row — was otherwise stamped verified and
  printed under "As printed in the source". Either form counts: the table's `48,200` or the
  narrative's `US$48.2 million`.
- **A figure is matched under its caption, not by first occurrence.** `121` appears in the
  Lindqvist fixture as both `Selling expenses (121)` and `Profit before tax 148 132 121`; the first
  match cited selling expenses as the source of a profit figure. Note that appearing several times
  is *not* ambiguity — a real figure shows up in the narrative, the statement and the MD&A — so
  refusing multi-match figures is wrong and costs a third of all coverage. A different caption is
  the thing to guard against.

Comparison figures are the subtle case: packaging replaces them with values from previously
approved filings, so citing a line of *this* PDF for them is a fabricated citation however real
the quoted line is. Only fields packaging left as the model read them are traced; the rest are
recorded as `prior_entry`.

`locate_quote()` tries an exact match, then a whitespace-insensitive one, because PDF extraction
routinely mangles spacing. It goes no looser than that: anything more permissive would start
accepting paraphrase, which is the thing being guarded against.

An untraceable figure is **recorded as unverified, never dropped** — a missing trace is
information the reviewer needs. It also raises a validation warning naming the affected fields,
and that warning is carried through approval by `_SOURCE_ONLY_WARNING_RE` since the approve path
no longer has the PDF text.

Evidence is built *after* the unit-scale audit, so a corrected figure is traced to the line it
was corrected against rather than to the model's original wrong value — and *from the packaged
data*, so it describes the figures the record actually holds. That second point matters: packaging
replaces the previous-quarter fields with DB-derived values, so tracing the raw analysis made the
traceability table cite a source line for a figure the summary above it showed as "—".

Observations are written against `pending_review_id` at ingest and re-pointed to
`pdf_metadata_id` on approval, so provenance survives the pending row being deleted. A rerun
discards the old trace first, since it no longer describes the new figures.

The draft report carries a **Source Traceability** table — figure, page number, and the text as
printed — so the reviewer can check a number without hunting for it. Pages are reconstructed via
`page_for_offset()` against `extract_pdf_pages()`; previously pages were flattened into one
string with no record of the boundaries, which is why page-level evidence was listed as a
limitation.

## Evaluation status

Be precise about which number you are quoting.

| Run | Result |
|---|---|
| `evaluation_20260708_090200.json` — live, **before** the unit-scale fix | **3/5** |
| `evaluation_20260816_164552.json` — live, after the fix, old fixture | **4/5** |
| `evaluation_20260816_211505.json` — **live, current code** | **5/5** |
| Replay of the two earlier runs against the corrected expectations | 5/5 each |

**The 5/5 is a genuine live run**, made on 2026-08-16 after the evidence prompt
change, with every figure correct and 6/6 evidence coverage on all five cases.

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

Plain asserts, no runner, no API calls — run each directly with `python <file>`.

- `test_unit_scale.py` — the false-positive rescale, a genuine 1000× slip, a clean extraction.
- `test_ingest_handoff.py` — guards the shared-ingest refactor: a monitored PDF and an uploaded
  PDF produce structurally identical pending rows, dedup is shared across both entry points, the
  review gate holds (nothing reaches `pdf_metadata`), and review events are recorded. Uses a
  temporary database and a stubbed LLM, so it never touches `pdfs.db`.
- `test_bursa_offline.py` — parser across all three shapes, date handling, watchlist matching,
  dedup key stability, idempotent inserts, and the prohibited-technique source scan.
- `test_poll_offline.py` — the full chain end to end: discovery → filter → dedup → download →
  hash → verify → queue → human-triggered extract. Also pins that polling twice inserts nothing
  the second time, and that discovery never creates a pending review or an approved row on its own.

- `test_evidence.py` — a verbatim quote verifies and resolves to a page; a **fabricated** quote
  does not and is never stored as provenance; with no quote the deterministic fallback finds the
  printed form; an untraceable figure is recorded as unverified rather than dropped; a malformed
  evidence block does not crash extraction; and provenance survives approval.

`test_bursa_offline.py` and `test_poll_offline.py` replace `socket.socket` for the duration of
the run and then assert nothing connected, so a regression that introduces a live request fails
in the suite rather than in production.

## Running it safely

`python app.py` binds **127.0.0.1** with the debugger off. That matters more than it looks:
**there is no authentication on any route.** Anything that can reach the port can
`POST /pending/<id>/approve` — writing to the database of record with no human involved, which is
the guarantee the whole tool exists to provide — or `POST /evaluate` repeatedly, at five paid API
calls a time.

`EARNINGS_BIND` exists for the deliberate case (behind a reverse proxy that authenticates) and
warns when used. Do not expose this to a network you do not control without putting authentication
in front of it.

## Unreviewed generated reports were committed and pushed

`reports/report_35.pdf` … `report_39.pdf` held generated review reports for **Sunway Berhad, a
real listed Malaysian company**, produced by the old GPT-4o-mini pipeline. They have no matching
`pdf_metadata` rows, which means **no human ever approved them** — they are drafts. `report_35.pdf`
also carries the tool's own validation warning that PBT (RM9,558.2M) exceeds revenue
(RM2,557.5M), alongside a +3043% YoY figure.

That combination — machine-generated financial figures about a named real company, flagged by our
own validation, never reviewed, and committed to a public repository — is a direct breach of two
non-negotiables in `CLAUDE.md`: human review is mandatory, and nothing leaves this machine without
a human action.

**Done:** `reports/` is now in `.gitignore` and the five files are untracked, so nothing further is
published and the next push removes them from the repository tree.

**Not done, and a decision for the operator:** the files remain in **pushed history** on
`origin/main` and also on `upstream/main` (the fork source). Removing them from history needs a
rewrite and a force-push, and scrubbing this fork does not remove them from upstream. This repo has
had one history scrub before, for `.env`, so the playbook exists.

## Open findings, not yet fixed

From the adversarial review of 2026-08-17. Real, verified, and bounded:

- **Annual-results filings are not matched.** `looks_like_results()` deliberately ignores "Annual
  Audited Accounts": it is a different document type from the quarterly report the extraction
  prompt is tuned for.

### Closed since that review

- **The discovery queue no longer re-offers approved filings.** `list_discovered` derived
  "extracted" from a `pending_reviews` count, and approval deletes that row, so a finished filing
  reverted to *Verified / Extract (uses API credit)*. No credit was ever spent — the duplicate
  check returns 409 first — but the panel whose job is saying what still needs attention said the
  opposite. It now also reads `announcements.status`, which is set to `extracted` when the button
  is pressed and survives the handover, and exposes `in_pending` so the panel can distinguish
  "awaiting review" from "already done". Both columns are popped unconditionally: short-circuiting
  the check would have leaked the raw column into the JSON.
- **`_TableParser` no longer raises `AttributeError` on malformed HTML.** An unclosed `<td>` left a
  cell open past its `</tr>`; the next `</td>` then closed that cell into a row that no longer
  existed. Closing a cell now checks its row is still open, `</tr>` keeps whatever an unclosed cell
  collected rather than discarding it, and `parse_html()` converts any residual feed error into
  `ParserError` — the type the pipeline already handles by noting the run and moving on, instead of
  an unhandled crash that would take a scheduled poll down.
- **Approval survives a draft report that cannot be deleted.** Writing the test above surfaced this:
  `approve_pending()` removed the draft PDF *after* committing the record of account but *before*
  deleting the pending row, unguarded. On Windows an open handle makes `os.remove` raise
  `PermissionError`, which returned a 500 for an approval that had in fact succeeded and left the
  filing both approved and still pending — where a second approval would have written a duplicate
  record of account. The cleanup is now best-effort and logs, like `record_review_event()`.

Covered by `test_poll_offline.py` (queue state across approval, including the locked-draft path)
and `test_bursa_offline.py` (stray `</td>`, nested table, well-formed listing unchanged).

## Known limitations

- **`llm_verified` coverage is still unmeasured.** The live run traced 30/30 figures, but the
  stored evaluation JSON only records the fields it checks, so the model's `evidence` block is
  not in the artifact. Replaying it shows all 30 traceable *deterministically* — that is the
  floor, i.e. what the code finds unaided. How often the model's own quotes verify is not
  recoverable from the run and is not yet instrumented. Nothing depends on it: an unverifiable
  quote degrades to a deterministic match, and only then to `unverified`.
- **The evaluation cannot be run from this environment.** The runtime blocks it because it sends
  local test PDFs to the external API, so it has to be triggered from the UI or a shell.
- Anchoring assumes `,` thousands separators. Documents using `.` or spaces fall through to the
  "scale unverified" warning rather than being mis-corrected.
- QoQ/YoY DB lookup matches company names case-insensitively; slightly different extracted names
  for the same company will miss and fall back to the report's own comparatives.
- `reports/` holds leftover artifacts (`report_35`–`report_39`) from earlier sessions with no
  matching DB rows. `report_1.pdf` is current and belongs to approved entry #1.

## Phase 2 — in progress

Bursa announcement monitoring: discovery → watchlist filter → dedup → attachment hashing → PDF
verification → the existing pending-review workflow. Monitoring runs as a standalone script; Flask
makes no outbound Bursa request. Discovery only ever *queues* candidates — a human triggers
extraction, so no API call is spent without a person asking for it.

**Built:**

- Six tables, created by `init_db()`: `companies`, `announcements`, `attachments`,
  `metric_observations`, `evidence`, `review_events`, plus supporting indexes.
- Nullable `source_attachment_id` on both `pdf_metadata` and `pending_reviews`, so a manual upload —
  which has no announcement behind it — remains valid.
- `seed_companies_from_file()` loads `watchlist.json` on startup. Insert-only (`INSERT OR IGNORE`),
  so UI edits survive a restart and re-running is idempotent. A missing or malformed file is
  logged and skipped, never fatal — the review tool must start without monitoring.

- `ingest_pdf_bytes()` — the single shared ingestion path (see Pipeline above), with
  `save_folder` and `source_attachment_id` parameters so monitored PDFs land in `attachments/`
  and carry their provenance.
- `locate_pending_file()` resolves a pending review's PDF from either `uploads/` or
  `attachments/`, so rerun and discard work for monitored files too.
- `record_review_event()` writing the review trail across the whole workflow.
- The `bursa/` package and `poll_bursa.py` (below), plus the Discovered UI panel and
  `/discovered/<id>/extract`.

There is deliberately **no** separate discovery-queue table: an `attachments` row with
`verification_status='verified'` and no linked pending review *is* the queue.

### Running the monitor

```bash
python poll_bursa.py --fixture-dir tests/fixtures/bursa --once   # offline, no network
python poll_bursa.py --dry-run                                   # live, writes nothing
python poll_bursa.py --once --since 2026-08-01                   # live pass
```

There is no loop mode on purpose — use Task Scheduler or cron. Hourly is ample.
`EARNINGS_DB_PATH` redirects the database, which is how a scheduled run or a test avoids the
reviewer's working copy.

### Module layout

| Module | Role |
|---|---|
| `bursa/models.py` | Normalised `Announcement` / `Attachment`, date parsing |
| `bursa/parser.py` | bytes → records. **The replaceability seam.** |
| `bursa/watchlist.py` | Match announcements to tracked companies |
| `bursa/dedup.py` | Stable keys, idempotent inserts |
| `bursa/verify.py` | PDF checks before any paid call |
| `bursa/pipeline.py` | Orchestration; returns a `PollSummary` |
| `bursa/client.py` | **The only module that touches the network** |

The parser handles two payload shapes plus an HTML listing fallback. All three must normalise to
identical records — including the announcement id, which the positional and HTML shapes carry only
inside the detail link. Recovering it is what keeps the dedup key stable across shapes; without
that, falling back to HTML would re-queue everything already discovered.

**The live shape is confirmed** against a response captured on 2026-08-16
(`tests/fixtures/bursa/announcements_live.json`). Rows are positional arrays:
`[row number, date, company, title]` — no category column, the stock code in the company link's
query string, the announcement id in the title link's, and the date rendered twice for responsive
layout. Codes are not always numeric (ETFs use `0823EA`).

**The listing carries no attachments.** The PDF lives on the announcement's own page, so
`_process_one()` fetches that page — but only for announcements that already survived the results
filter and the watchlist, so it is a handful of extra requests, not one per announcement.

`ParserError` is raised rather than returning `[]`, because an empty window is a legitimate result
and must stay distinguishable from a broken parser. The error reports the keys it actually saw.

### Monitoring conduct

Enforced in `bursa/client.py`, not just documented: an identifying User-Agent with a contact
address, `robots.txt` consulted and obeyed (unreadable robots.txt means we refuse to fetch), a
2-second floor between requests that callers cannot lower, a 30-request cap per run, and 403/429
aborting the run outright. There is no proxy support, no User-Agent list, and no retry-on-block —
`test_bursa_offline.py` asserts those patterns stay absent from the source, alongside a check that
KLSE Screener is never referenced as a source.

Evidence capture is built — see **Evidence and source traceability** above.

### Two-stage filtering

`cat=FA,FRCO` is the category filter the site's own *Financial Results* control sends. It takes
the listing from **2,087,762 announcements to 1,081**, so a poll reads a shortlist rather than a
haystack. It is the default; `--category ""` disables it, and `--market` / `--sector` /
`--subsector` expose the rest.

The category is **not sufficient on its own** — it still admits filings like *"Change in Financial
Year End"* — so `looks_like_results()` remains the second gate, matching on the title.

That gate must stay generous. Not every results filing says "quarterly": Key Asic's 2026-07-22
announcement is titled *"Consolidated results for the financial period ended 31/05/2026"*, and an
earlier version of the pattern list dropped it silently. A missed filing produces no error, so
the failure is invisible — prefer a false positive, which a human sees and discards.

### What is still unverified about Bursa

The row shape and the category filter are confirmed. These are not:

- **Whether `dt_ht` / `dt_lt` are date-from and date-to.** `--since` filters client-side instead.
- **The HTML fallback layout** — the rendered page 403s to automated clients, so that fixture
  mirrors the JSON column order rather than a captured page.
- **The detail page structure** — `parse_attachment_page()` accepts any `.pdf` link or embed
  rather than assuming a fixed attachment path.

Cloudflare fronts the site and refuses some automated requests; the endpoint answered 200 to a
bare request and 403 to a parameterised one during testing. Expect intermittent `BlockedError`,
which correctly stops the run rather than retrying. See `tests/fixtures/bursa/README.md`.

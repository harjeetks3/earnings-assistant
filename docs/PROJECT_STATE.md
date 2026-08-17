# Project State

Authoritative description of what the code currently does. Rewrite this file when it drifts —
don't patch around it. Rules that don't change live in `CLAUDE.md`.

**Last verified:** 2026-08-17, against commit `776578e` **plus uncommitted working-tree changes**.

This stamp does *not* describe a clean commit. Modified and uncommitted: `app.py`,
`bursa/client.py`, `poll_bursa.py`, `validation.py`, `templates/index.html`,
`test_bursa_offline.py`, `test_evidence.py`, `test_ingest_handoff.py`, `test_poll_offline.py`,
`CLAUDE.md` and this file. Untracked: `test_approval_gate.py`, `run_tests.py`. Everything below was
checked against the files on disk, not against `776578e` — a `git stash` would make parts of this
document wrong. `main` is also four commits ahead of `origin/main`.

**On references to code:** this document names a **file and a symbol** (`init_db()` in `app.py`),
never a line number. Line numbers were used until 2026-08-17 and three of them had already rotted —
two pointed into `app.py` for functions that now live in `validation.py` and `audit.py`. Grep for
the definition; it survives a refactor, and a number does not.

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

## Code layout

`app.py` was one file. It is being split, bottom of the dependency stack first, so that `bursa/` can
reach the logic it needs **without importing the Flask app**. The split is partial and ongoing;
`app.py` is still by far the largest module.

| Module | Role |
|---|---|
| `app.py` | Flask app, routes, schema (`init_db()`), ingestion, evidence, report building, the LLM call and the evaluation harness. Still the bulk of the code. |
| `pdftext.py` | `get_pdf_metadata()`, `extract_pdf_pages()`, `extract_pdf_text()`, `page_for_offset()`, `PAGE_SEPARATOR`. The bottom of the stack — audit, evidence and evaluation all need it. |
| `validation.py` | `validate_analysis()`, the `EarningsReport` Pydantic model, `MONETARY_FIELDS`, the company-name token rules. |
| `audit.py` | `_audit_unit_scale()`, `_self_generated_report_warning()`, `_scale_factor()` and the anchoring helpers. |
| `poll_bursa.py` | CLI entry point for the monitor. Imports `bursa/` plus `app` — for `init_db()` and the shared ingest path — but never starts a server. |
| `bursa/` | The monitoring package — see **Module layout** under Phase 2. |
| `run_tests.py` | Runs every `test_*.py` in one command. |
| `templates/index.html` | The whole UI. One page, no build step. |

Each extracted module is **re-exported from `app.py`** (`from validation import …`, `# noqa: F401`),
so `app.validate_analysis` and friends still resolve for the tests and callers that already
reference them. That is deliberate: the split was meant to move code, not to force a rename across
every caller in the same commit.

Direction of dependency, which is the point of the exercise: `bursa/` imports **nothing** from
`app.py`. Its one upward reference is `bursa/verify.py` reaching for
`audit._self_generated_report_warning`, done lazily inside the function and wrapped in a
`try/except`, so the package still runs standalone if that module is not there.

## Database (SQLite, `pdfs.db`)

Auto-migrated on startup by `init_db()` (`app.py`).

### `pdf_metadata` — record of account, written only on approval
File facts (`filename`, `file_size`, `sha256`, `pages`, `title`, `author`, `creator`,
`uploaded_at`), `source_attachment_id` (the monitored attachment the filing came from; NULL for a
manual upload), the LLM-extracted fields (`company_name`, `quarter_end_date`, `fiscal_quarter`,
`fiscal_year`, `currency`, `unit_raw`, six monetary fields, `management_commentary`,
`outlook_summary`, `confidence_score`), plus `analysis_error`, `validation_warnings` (JSON array
string), the four growth fields, and `report_path`.

A partial unique index, `idx_pdf_metadata_sha256 ON pdf_metadata(sha256) WHERE sha256 <> ''`,
enforces one record of account per document. It is partial because rows predating the `sha256`
column migrated in with `DEFAULT ''` and there can legitimately be many of those, and it is
created best-effort: a database that already holds two rows for one document cannot build it, says
so on startup and carries on.

`init_db()` is schema only — tables, additive `ALTER TABLE` migrations and the watchlist seed. It
deliberately does **not** touch `pdf_metadata` content: `poll_bursa.py` calls it on every scheduled
run, and no automated path may write to the record of account.

### `pending_reviews` — staging, deleted on approval
Same file facts plus `extracted_data` (whole analysis as JSON), `report_path`, `attempt_count`,
`extra_instructions`, `downloaded_at`, `created_at`, `updated_at`.

`downloaded_at` gates approval: you cannot approve a report you have not downloaded.

Approval is one `BEGIN IMMEDIATE` transaction: the `pdf_metadata` INSERT, the re-pointing of
`metric_observations` at the new entry, and the DELETE of the pending row commit together or not
at all. The DELETE is the claim — a second, concurrent approval of the same review blocks on the
write lock, then finds no row and is refused, so a double-clicked *Approve* cannot write two
records of account. The report build, the comparison refresh, the announcement status change and
the review events all run **after** the commit, because each commits internally and would end the
claim early.

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
| DELETE | `/pending/<id>` | Discard without saving; an uploaded file is removed (hash-confirmed), a monitored filing is returned to the discovery queue with its download intact |
| GET | `/pdfs` | List approved entries |
| GET | `/pdfs/<id>/report` | Download the final report |
| DELETE | `/pdfs/<id>` | Delete an approved entry; same file rule as discarding a pending review |
| GET | `/discovered` | Bursa filings the monitor found and verified |
| POST | `/discovered/<id>/extract` | Human-triggered extraction of a discovered filing (**costs money**) |
| POST | `/evaluate` | Run the 5-case synthetic suite (**costs money**) |
| GET | `/eval_results/<file>` | Download a stored evaluation JSON |

Both delete routes share one rule, `_remove_source_pdf()`: resolve the file by provenance
(`locate_pending_file`), then confirm its bytes hash to the `sha256` the record was made from, and
delete only on an exact match. Anything unconfirmable — no recorded hash, unreadable, contents
changed — is left on disk with a `[WARNING]` naming it. A stray PDF is housekeeping; an unrelated
document destroyed by a shared basename is not recoverable. The database row is removed either way.

`announcements.status` is the discovery queue's memory of what has been decided:

```
discovered --Extract--> extracted --approve--> approved
     ^                      |                      |
     +------discard---------+---delete approved----+
```

`discovered` is written by the monitor; every other transition is a human action in `app.py`.
`list_discovered` treats `extracted` and `approved` alike as "already spent". The two return edges
matter: discarding a review or deleting an approved entry hands a monitored filing back to the
queue **and keeps the downloaded PDF**, because that file belongs to the attachment row rather than
to the review. If the file has already gone, the attachment's `sha256`/`local_path` are cleared so
the next poll re-fetches it — a hashed row with no file can never come back otherwise.

## Validation

`validate_analysis(raw, pdf_meta=None)` (`validation.py`) runs three passes: Pydantic types, then
cross-field rules (quarter, fiscal year range, date format and year agreement, ISO-4217 currency,
non-negative revenue, PBT vs revenue, confidence range, all-null figures), then a check that the
PDF's embedded metadata does not contradict the extracted company name.

PBT exceeding revenue is flagged as *unusual but legitimate* (one-off disposal gains), not as an
error.

## Unit-scale audit

`_audit_unit_scale()` (`audit.py`) exists because the model reads the unit label correctly but
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

**No evaluation has been run since 2026-08-16.** Every number in this section is copied from a
stored artifact in `eval_results/`. None of them describes the code as it stands today.

Be precise about which number you are quoting, and about when it was measured.

All five stored runs, oldest first. Every one is **live** — a real API call per case.

| Artifact in `eval_results/` | Measured against | Result |
|---|---|---|
| `evaluation_20260708_084646.json` | before the unit-scale fix | 2/5 |
| `evaluation_20260708_085546.json` | before the unit-scale fix | 3/5 |
| `evaluation_20260708_090200.json` | before the unit-scale fix | **3/5** |
| `evaluation_20260816_164552.json` | after the fix, old fixture | **4/5** |
| `evaluation_20260816_211505.json` | commit `9496ecb`, 2026-08-16 | **5/5** |

Plus one figure that is **not** a live run and must never be quoted as one:

| | Measured against | Result |
|---|---|---|
| Replay against the corrected expectations — **offline**, ad-hoc script, not in the repo | the two earlier runs | 5/5 each |

### What the 5/5 actually measured, and what has changed since

It was a genuine live run: five cases, every figure correct, 6/6 evidence coverage on each, made on
2026-08-16 after the evidence prompt change and recorded at commit `9496ecb`.

**It does not describe today's code.** Seven commits have landed since `9496ecb` — `5df3d81`,
`3274f20`, `3f85f15`, `b6a4bfa`, `565525a`, `8a57fca`, `776578e` — plus this session's uncommitted
work, and several carry behaviour changes on paths the harness touches: the Bursa parser and
category filter, nine Phase 2 defect fixes, the `app.py` split into `pdftext.py` / `validation.py` /
`audit.py`, and this session's changes to ingestion, approval and deletion.

That is not a reason to disbelieve the number. It is a reason to quote it as *"5/5, live, at commit
`9496ecb` on 2026-08-16"* and nothing shorter. Whether today's code still scores 5/5 is unknown,
because measuring it costs money and needs a person to ask for it.

### The case-04 fixture, and what "replay" means here

In the 4/5 run (`evaluation_20260816_164552.json`), **all five cases produced correct figures.** The
one failure was case 04, and its only failing check was `validation_warnings`: the fixture required
the unit-scale correction warning, but the live model got Yamato's arithmetic right, so the audit
had nothing to correct and stayed silent. The test was effectively demanding that the model make a
mistake.

That expectation is now split into `expected_validation_warnings` (required) and
`conditional_validation_warnings` (permitted, not required); the scale-correction warning moved to
the latter for cases 04 and 05. Anything outside both lists still fails, so a genuinely new warning
is caught. Replaying both stored runs — one where the model erred, one where it didn't — gave 5/5
under the corrected fixture, which is what showed the expectation is no longer model-dependent.

**Those replays are not reproducible from this repository.** No replay harness has ever been
committed, and the stored artifacts do not contain enough to build one: each check records only
`{field, actual, expected, passed, info_only}`, so the raw model response is not in the file.
`_evaluate_case()` has no seam for a stored response either — it calls `analyse_earnings()`, which
always goes to the API. So the two replay figures were produced by throwaway offline scripts
re-checking the recorded per-field results against the corrected expectations. Treat them as
evidence about the *fixture*, not as a measurement of the pipeline.

## Tests

```bash
.venv/Scripts/python.exe run_tests.py     # all suites
.venv/Scripts/python.exe test_evidence.py # or any one of them, directly
```

**There is no pytest.** It is not installed and not in `requirements.txt`, and the suites would be
invisible to it anyway: no `def test_`, every `check()` runs at import time, and each file ends in
`sys.exit(1)` on failure. `python -m pytest` errors out with *No module named pytest* — the command
this document used to give. `run_tests.py` is the replacement: it globs `test_*.py`, runs each in
its **own process** (they each repoint `EARNINGS_DB_PATH` before importing `app`, so sharing an
interpreter would let the second suite inherit the first one's workspace), reprints the output of
anything that fails, and exits non-zero. Discovery is by glob so a new suite needs no edit here.

Plain asserts, a stubbed LLM, no API calls, no network.

| Suite | `check()` calls |
|---|---|
| `test_unit_scale.py` | 8 |
| `test_ingest_handoff.py` | 48 |
| `test_bursa_offline.py` | 140 |
| `test_poll_offline.py` | 118 |
| `test_evidence.py` | 87 |
| `test_approval_gate.py` | 58 |
| **Total** | **459** |

Counted from a run on 2026-08-17 against the working tree, all green. `test_approval_gate.py` is
new this session and still **untracked** — it runs and passes, but it is not in the repository yet.

- `test_unit_scale.py` — the false-positive rescale, a genuine 1000× slip, a clean extraction.
- `test_ingest_handoff.py` — guards the shared-ingest refactor: a monitored PDF and an uploaded
  PDF produce structurally identical pending rows, dedup is shared across both entry points, the
  review gate holds (nothing reaches `pdf_metadata`), and review events are recorded. Also pins
  that `init_db()` leaves every approved row byte for byte as the reviewer left it while a
  human-triggered refresh still corrects a stale one, that bytes which are not a filing are refused
  with a JSON 400 before anything reaches disk, that editing `watchlist.json` reconciles a company
  already seeded without ever deleting one, and that a failure part-way through approval (forced
  with a `BEFORE DELETE` trigger) leaves nothing in `pdf_metadata` and the review untouched.
  Uses a temporary database and a stubbed LLM, so it never touches `pdfs.db`.
- `test_approval_gate.py` — the places the record of account can be damaged: provenance is
  carried onto approval, two racing approvals of one review produce exactly one entry (two threads
  released by a barrier), a failure mid-approval leaves the review approvable, the unique index
  refuses a second record for one document without consuming the review, and neither delete route
  destroys a file it cannot confirm.
- `test_bursa_offline.py` — parser across all three shapes, date handling, watchlist matching,
  dedup key stability, idempotent inserts, and the prohibited-technique source scan. Also the
  per-host client rules: each host judged by its own `robots.txt`, one host's robots failure not
  silencing another, per-host crawl delay and pacing, per-host request budgets inside the run cap,
  host-key normalisation, and `file://` / `ftp://` refused outright.
- `test_poll_offline.py` — the full chain end to end: discovery → filter → dedup → download →
  hash → verify → queue → human-triggered extract. Also pins that polling twice inserts nothing
  the second time, that discovery never creates a pending review or an approved row on its own,
  the queue's return edges (discarding a review or deleting an approved entry offers the filing
  again, keeps its download, and — when the file has gone — lets the next poll re-fetch it), that
  two threads approving one review write one record rather than two, and that neither delete route
  removes a document whose bytes do not hash to what the record was made from.
- `test_evidence.py` — a verbatim quote verifies and resolves to a page; a **fabricated** quote
  does not and is never stored as provenance; with no quote the deterministic fallback finds the
  printed form; an untraceable figure is recorded as unverified rather than dropped; a malformed
  evidence block does not crash extraction; and provenance survives approval. Also the review gate
  in its **failing** direction — approving or rejecting an undownloaded draft is refused, and a
  rerun clears `downloaded_at` so the gate re-arms — the locked-draft path (see below), and the
  evaluation harness's `evidence_match_methods` breakdown, driven through `_evaluate_case()` with
  the model stubbed so no API call is made.

The **locked-draft path** lives in `test_evidence.py`, not in `test_poll_offline.py` where this
document used to place it. It points the pending row's `report_path` at a non-empty directory so
`os.remove` genuinely raises on any platform, and asserts the approval still returns 201, writes
exactly one record, and gives up the pending row. It pins the *false 500* half of the original bug
only: the duplicate-record half cannot recur now that the pending DELETE sits inside the approval
transaction.

`test_bursa_offline.py` and `test_poll_offline.py` replace `socket.socket` for the duration of
the run and then assert nothing connected, so a regression that introduces a live request fails
in the suite rather than in production.

## Running it safely

`python app.py` binds **127.0.0.1** with the debugger off. That matters more than it looks:
**there is no authentication on any route.** Anything that can reach the port can
`POST /pending/<id>/approve` — writing to the database of record with no human involved, which is
the guarantee the whole tool exists to provide — or `POST /evaluate` repeatedly, at five paid API
calls a time.

Every environment variable the app reads is here. The first two change who can reach the tool; do
not set either without reading the paragraphs below.

| Variable | Default | Effect |
|---|---|---|
| `EARNINGS_BIND` | `127.0.0.1` | Interface to bind. Anything else warns on startup. |
| `EARNINGS_DEBUG` | off | `1`/`true`/`yes` turns on the Werkzeug debugger. Warns on startup. |
| `EARNINGS_DB_PATH` | `pdfs.db` beside `app.py` | Which database to use. How a scheduled poll or a test avoids the reviewer's working copy. |
| `OPENAI_API_KEY` | — | Read from `.env`. Without it `analyse_earnings()` returns an `analysis_error` instead of calling out. |

`EARNINGS_BIND` exists for the deliberate case (behind a reverse proxy that authenticates) and
warns when used. Do not expose this to a network you do not control without putting authentication
in front of it.

`EARNINGS_DEBUG` turns on the Werkzeug debugger, which is an interactive Python console served on
any error page — that is remote code execution as the user running the app, on the machine holding
`pdfs.db`, the source PDFs and `.env`. It was undocumented until 2026-08-17 while doing exactly
this. It now warns as loudly as the bind does.

**The two together are the dangerous combination.** With no authentication on any route, an
`EARNINGS_DEBUG` console bound to anything but loopback is an unauthenticated shell for whoever
reaches the port — and the same port also serves `POST /pending/<id>/approve`, which writes the
record of account without a human, and `POST /evaluate`, which spends real money on each call.
`app.py` prints a `[DANGER]` line when it sees both, but the warning is the only thing stopping it:
nothing in the code refuses the combination.

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

**Done, locally:** `reports/` is in `.gitignore` and the five files are untracked as of commit
`776578e` — `git ls-files reports` returns nothing. Nothing further will be published.

**Still to push.** As of 2026-08-17 `main` is **four commits ahead of `origin/main`** (which sits at
`3f85f15`), and `776578e` is one of the four. Until that push lands, the five PDFs are still in the
tree on `origin/main`, not merely in its history.

**Not done, and a decision for the operator:** the files remain in **pushed history** on
`origin/main` and also on `upstream/main` (the fork source). Removing them from history needs a
rewrite and a force-push, and scrubbing this fork does not remove them from upstream. This repo has
had one history scrub before, for `.env`, so the playbook exists.

## Open findings, not yet fixed

From the adversarial review of 2026-08-17. Real, verified, and bounded:

- **Annual-results filings are not matched.** `looks_like_results()` deliberately ignores "Annual
  Audited Accounts": it is a different document type from the quarterly report the extraction
  prompt is tuned for.
- **A refused approval can name an entry that does not exist.** `approve_pending()` treats every
  `sqlite3.IntegrityError` inside the transaction as the `sha256` duplicate, so any *other*
  constraint failure still tells the reviewer *"This document is already saved as an approved
  entry"* — with `existing_id: null`, pointing them at nothing. The **behaviour** is correct and
  safe: the transaction rolls back, nothing reaches `pdf_metadata`, the review stays approvable.
  Only the message misleads. Found 2026-08-17; the tests assert the state left behind rather than
  the wording, so they will keep passing when it is fixed.

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

- **A double-clicked *Approve* can no longer write two records of account.** Nothing serialised the
  route: two requests read the same pending row and both INSERTed. Approval is now one
  `BEGIN IMMEDIATE` transaction whose DELETE of the pending row is the claim, backed by a partial
  unique index on `pdf_metadata.sha256` for the case the claim cannot see (two simultaneous uploads
  of one PDF staging two reviews). A refused approval leaves the review in place.
- **An approved entry records the attachment it came from.** `pdf_metadata.source_attachment_id`
  existed since the Phase 2 migration and was never written, so a monitored filing lost its link
  back to its announcement at the moment it became the record of account — and deletion had no way
  home. Entries approved before this change stay NULL; the link was never recorded and cannot be
  reconstructed for them.
- **Discarding a review no longer retires the filing for good.** Discard deleted the monitored PDF
  while the attachment row kept its `sha256`, so the queue said *Already extracted*, offered no
  button, and no later poll re-downloaded it. Discard and delete-approved now return the
  announcement to `discovered` and keep the download; if the file is gone, the row's download state
  is cleared so the next poll fetches it again.
- **An uploaded file is verified before it is written.** `POST /upload` gated on the file extension
  only, so an HTML page renamed `.pdf` was saved to `uploads/` and then raised inside pypdf — an
  unhandled 500 with the bytes left behind, which the panel could not even parse. `ingest_pdf_bytes`
  now runs the monitor's own `verify_pdf_bytes()` before touching disk and returns a `rejected`
  result the routes map to a JSON 400.
- **`init_db()` no longer writes to the record of account.** It ended by backfilling comparison
  columns on `pdf_metadata`, and `poll_bursa.py` calls it on every scheduled run — an automated
  write to the record of account. The backfill now runs only on approval and on deleting an
  approved entry, both human actions. Nothing displayed changes: `/pdfs` and the report builder
  recompute comparisons on read.
- **Deleting no longer resolves a file by basename alone.** `delete_pdf` scanned `uploads/` then
  `attachments/` and removed the first hit, so an approved entry could destroy an unrelated
  document that happened to share a name. Both delete routes now share `_remove_source_pdf()`,
  which resolves by provenance and confirms the hash first.
- **`robots.txt` is no longer applied to the wrong site.** `BursaClient` held one robots parser,
  one crawl delay, one pacing clock and one budget for the whole run, but `robots.txt` is scoped
  per host — and CPython's `RobotFileParser.can_fetch()` discards scheme and netloc before matching,
  so a parser loaded from Bursa answered confidently, and permissively, about anybody else's paths.
  Only one host is polled today, so nothing was actually mis-fetched; the second host would have
  been. Rules, delay, pacing and budget are now keyed on `scheme://host[:port]` derived from the
  fully resolved URL, and anything that is not http(s)-with-a-host is refused outright — which also
  closes an existing hole where an absolute `file://` or `ftp://` href reached `urlopen`.
- **`seed_companies_from_file()` reconciles instead of only inserting.** Setting
  `"is_active": false` on a company already in the database did nothing and the monitor kept
  polling it, which made `watchlist.json` a lie. See Phase 2 → Built.
- **`EARNINGS_DEBUG` is documented.** It enabled the Werkzeug debugger — an unauthenticated remote
  console — and appeared in no document. See *Running it safely*.
- **The test command in `CLAUDE.md` works.** It said `python -m pytest`, which errors out: pytest is
  not installed and the suites are not pytest tests. `run_tests.py` replaces it.

Covered by `test_poll_offline.py` (queue state across approval, the return edges, the racing
approval, the hash-confirmed deletes), `test_approval_gate.py` (the same three, independently — the
overlap is deliberate), `test_ingest_handoff.py` (`init_db()` leaving approved rows untouched, the
rejected upload, the failure part-way through approval), `test_evidence.py` (**the locked-draft
path**, and the review gate refusing an undownloaded draft) and `test_bursa_offline.py` (stray
`</td>`, nested table, well-formed listing unchanged, and the per-host client rules).

## Known limitations

### Measurement

- **`llm_verified` coverage is instrumented but still unmeasured.** As of 2026-08-17 `_evaluate_case()`
  records an `evidence_match_methods` breakdown — a count per `match_method`, `info_only` so it can
  never fail a case — alongside the existing "N/N figures traced" line. **No run has produced one
  yet.** The number will appear in the next paid `POST /evaluate`, at no extra API cost.

  It has to be taken live, and that is not a preference. A replay cannot answer this question:
  `build_evidence()` is recomputed on every invocation, but from an analysis dict that carries no
  model quotes, because the stored eval JSON records only `{field, actual, expected, passed,
  info_only}` and `expected_results.json` has no `evidence` key. So on a replay `claimed` is empty,
  `locate_quote()` is never given a quote, and every figure falls through to `_locate_figure()`.
  **A replay's `llm_verified: 0` is an artifact of the quotes not being stored, not a measurement of
  the model, and must never be reported as "the model's citations never verify."**

  The *deterministic floor* — 30/30 figures traceable by code unaided — comes from that **replay**,
  and only a replay can establish it, for the same reason. The 2026-08-16 **live** run also reached
  30/30, but that is the *combined* figure: `build_evidence()` reaches `_locate_figure()` only when
  the model's own quote fails to verify (app.py, `build_evidence`), so a live run can never separate
  the two methods. Do not attribute the deterministic floor to the live run.

  Nothing in the pipeline depends on the split — an unverifiable quote degrades to a deterministic
  match, and only then to `unverified` — it is a quality signal, not a guarantee.

- **The evaluation cannot be run from this environment.** The runtime blocks it because it sends
  local test PDFs to the external API, so it has to be triggered from the UI or a shell. Whatever
  figure it produces will vary run to run, like everything the model produces: quote the run file
  and the word *live* with it.

### Not built yet

- **There is no per-company IR URL anywhere in the schema.** `companies` holds `stock_code`, `name`,
  `short_name`, `is_active`, `source`, `notes`, `added_at` and nothing else — no investor-relations
  page, no filings index, no alternative source. `url` exists on `announcements` and `attachments`,
  but those are Bursa links the monitor discovered, not a per-company address anyone configured.
  Adding one is an additive `ALTER TABLE` plus a `watchlist.json` field; none of it exists today.
- **`watchlist.json` still holds the seed placeholders,** not the journalist's actual coverage:
  Maybank, CIMB, Tenaga and an inactive Maxis, the first of them still labelled *"Seed entry.
  Replace with the companies you actually cover."* Editing the file now really does reconcile the
  database, so this is a data task, not a code one — but until someone does it, a live poll is
  watching four demonstration companies.
- **Discard means "show it to me again".** There is no way to say "never show me this filing":
  expressing that would need a separate status and a UI control, deliberately not built. Extraction
  still takes a click, so a refilled queue cannot spend money on its own.

### Consequences of decisions already taken

- **Entries approved before provenance was written keep `source_attachment_id = NULL`.** Deleting
  one takes the upload path — hash-confirmed, so it cannot destroy an unrelated document — but it
  does not return its filing to the discovery queue. A backfill was considered and rejected: it
  would be an automated `UPDATE` against the record of account, issued from `init_db()`, which
  `poll_bursa.py` runs on every scheduled poll. The link was never recorded and cannot be
  reconstructed, so these rows stay NULL.
- **Manual uploads are stricter.** `verify_pdf_bytes()` also refuses a scanned filing (under 400
  characters of extractable text) and a review report this tool generated itself, so both now
  return 400 from `POST /upload` instead of being extracted. That is what the check exists to
  prevent — extraction would invent figures — and it is now the same rule on both entry points.
- **Reading `robots.txt` now costs a delay.** Each host's pacing clock is stamped when its
  `robots.txt` is read, since that was itself a request, so the first content request to a host
  waits the full delay. Correct, and about two seconds slower per host per run than before.
- **A run touching more than 40 hosts ends early.** `MAX_HOSTS_PER_RUN` exists because each new
  host costs an uncounted `robots.txt` read. It raises the ordinary budget error, which the pipeline
  treats as run-ending, and no test exercises it. One host is polled today.
- **`HostBudgetExceeded` is not yet used as a "skip this host" signal.** It subclasses
  `RequestBudgetExceeded`, so `bursa/pipeline.py` catches it and ends the run — correct today,
  because the per-host cap defaults to the run cap and cannot fire first. Whoever adds a multi-host
  loop must catch the narrow type *before* the general one; nothing in the suite would catch the
  omission.
- Anchoring assumes `,` thousands separators. Documents using `.` or spaces fall through to the
  "scale unverified" warning rather than being mis-corrected.
- QoQ/YoY DB lookup matches company names case-insensitively; slightly different extracted names
  for the same company will miss and fall back to the report's own comparatives.

### Untested paths

- **The unreadable-source-PDF branch of `_remove_source_pdf()`.** A file locked by another process
  makes the hash check raise `OSError`; the code logs a `[WARNING]` and leaves the file. Holding a
  real file lock is not portable — Windows-only tricks would break the suite everywhere else — so
  the log line is the only signal. (The locked-*draft* path in `test_evidence.py` sidesteps this by
  pointing at a directory; it does not cover this branch.)
- **The `busy` 409 branch of `approve_pending()`** — `BEGIN IMMEDIATE` exceeding the five-second
  busy timeout. It did not fire in any run during this session's testing; the losing thread always
  reached the lock well inside the timeout. The racing tests accept any sub-500 refusal, so they
  cannot become flaky if it ever does.
- **Discarding an upload whose source PDF cannot be read** used to 500; it now logs and leaves the
  file while the discard succeeds. Deliberate and consistent with the rest of the cleanup, but
  nothing pins it.

### Files on disk

- `reports/` holds only `report_35.pdf`–`report_39.pdf`: unreviewed drafts from earlier sessions
  with no matching DB rows (see the section above). The one approved entry, #1, records
  `report_path` → `reports/report_1.pdf`, **which is not on disk.** Nothing depends on it —
  `GET /pdfs/<id>/report` regenerates the report from the row on every request and rewrites
  `report_path` — so the stale path is cosmetic, not a broken download.

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
- `seed_companies_from_file()` loads `watchlist.json` on startup and reconciles `is_active`, `name`
  and `short_name` for stock codes already in the database, printing what changed. It used to be
  insert-only, which made the file a lie: setting `"is_active": false` on a seeded company did
  nothing and the monitor kept polling it. Rows are never deleted — a company dropped from the file
  still owns its announcements, so it is reported and left alone. A missing or malformed file is
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
python poll_bursa.py --dry-run                                   # live, records nothing (see below)
python poll_bursa.py --once --since 2026-08-01                   # live pass
```

`--dry-run` fetches and matches but downloads nothing and records no announcements or attachments.
It is **not** "writes nothing", which is what this document and the `--help` text used to claim:
every run calls `init_db()` first, so the schema is created and `watchlist.json` is seeded. Both are
needed even for a dry run — on a fresh database there would be no tables to read and no watchlist to
match against, and the run would report zero matches and look like a quiet hour. `init_db()` writes
nothing to `pdf_metadata`; that is the line that matters, and it is enforced rather than described.

There is no loop mode on purpose — use Task Scheduler or cron. Hourly is ample.
`EARNINGS_DB_PATH` redirects the database, which is how a scheduled run or a test avoids the
reviewer's working copy.

### Module layout (`bursa/`)

For the top-level modules, see **Code layout** near the top of this document.

| Module | Role |
|---|---|
| `bursa/models.py` | Normalised `Announcement` / `Attachment`, date parsing |
| `bursa/parser.py` | bytes → records. **The replaceability seam.** |
| `bursa/watchlist.py` | Match announcements to tracked companies |
| `bursa/dedup.py` | Stable keys, idempotent inserts |
| `bursa/verify.py` | PDF checks before any paid call |
| `bursa/pipeline.py` | Orchestration; returns a `PollSummary` |
| `bursa/client.py` | **The only module that touches the network.** Conduct is enforced here, per host |

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
address, `robots.txt` consulted and obeyed **per host** — the rules, the crawl delay, the pacing
clock and each host's share of the budget are keyed by `scheme://host[:port]`, because Python's
`RobotFileParser.can_fetch()` matches on the URL path alone and would otherwise judge one site by
another's rules — an unreadable robots.txt means we refuse to fetch that host, a 2-second floor
between requests to the same host that callers cannot lower and that a site's `Crawl-delay` can
only raise, a 30-request cap per run with a per-host share inside it, and 403/429 aborting the run
outright. There is no proxy support, no User-Agent list, and no retry-on-block —
`test_bursa_offline.py` asserts those patterns stay absent from the source, alongside a check that
KLSE Screener is never referenced as a source.

Two conduct rules are easy to misread as bugs and are deliberate. A `Crawl-delay` above
`MAX_HONOURED_DELAY` (60s) makes us **skip that host**, not shorten the wait to something we find
convenient — clamping a site's stated delay downwards is rate-limit evasion. And reading a host's
`robots.txt` stamps that host's pacing clock, because reading it was itself a request; the first
content request therefore waits the full delay rather than going out back-to-back with it.

Constants live at the top of `bursa/client.py`: `MIN_ALLOWED_DELAY` 2s, `MAX_HONOURED_DELAY` 60s,
`MAX_REQUESTS_PER_RUN` 30, `MAX_REQUESTS_PER_HOST` (defaults to the run cap, so today's single-host
poll is unchanged), `MAX_HOSTS_PER_RUN` 40.

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

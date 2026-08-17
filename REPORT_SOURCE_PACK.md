# 1. Executive Summary of Current Implementation

- The current system is a Python Flask web app for the "Bursa Earnings Brief Assistant", a human-in-the-loop earnings PDF review tool for journalists. It is not an autonomous publishing bot.
- The frontend is a single-page HTML/CSS/JavaScript interface in `templates/index.html` with drag-and-drop PDF upload, pending review, approved reports, and evaluation harness panels.
- PDF text and metadata are extracted with `pypdf`. Text is read page by page with `page.extract_text()` and joined into one text block, but the page boundaries are no longer lost: `extract_pdf_pages()` keeps the per-page list and `page_for_offset()` maps a character offset in the joined text back to its page number, which is what makes page-level evidence possible.
- LLM extraction is implemented directly in `app.py` through `openai.OpenAI().chat.completions.create(...)`. The actual API call uses `model="gpt-5.4-mini"`, `temperature=0`, and JSON mode via `response_format={"type": "json_object"}`.
- The LLM returns a JSON object with company, quarter, fiscal year, currency, unit, revenue, PBT, commentary, outlook, and confidence fields. There is no true strict JSON Schema response contract; Pydantic and rule checks validate after JSON generation.
- Validation uses a Pydantic model plus deterministic rules for fiscal quarter, fiscal year, date format/year consistency, currency format, negative revenue, PBT greater than revenue, confidence range/low confidence, and all-null financial extraction.
- Storage is SQLite. `pending_reviews` stages extracted data until human review. `pdf_metadata` is the approved database of record and is only written after the reviewer downloads the draft PDF report and approves it.
- Duplicate detection hashes the uploaded bytes with SHA-256 and checks `(sha256, file_size)` against both approved and pending rows before saving a new upload.
- Review reports are generated as PDFs with ReportLab into `reports/`. Draft pending reports use `pending_<id>.pdf`; approved reports use `report_<id>.pdf`.
- The evaluation harness runs five synthetic PDFs in `test_data/` against the extraction/validation/QoQ-YoY logic and writes JSON outputs in `eval_results/`. The current live result is `evaluation_20260816_211505.json`: **5/5 passed**, with all six monetary figures in every case traced back to a line in the source document. The progression that got there is worth reporting: 3/5 before the unit-scale audit (`evaluation_20260708_090200.json`), then 4/5 (`evaluation_20260816_164552.json`) where all five cases extracted correct figures but one fixture still demanded a scale-correction warning on a run where the model made no scale error — an expectation that required the model to make a mistake, since made conditional.

# 2. Repository Structure

Meaningful source and artifact files/folders:

- `app.py`: Main Flask application. Contains PDF extraction, OpenAI prompt/API call, Pydantic model, validation rules, growth calculations, SQLite initialization/migration, pending review workflow, ReportLab report generation, and evaluation harness.
- `templates/index.html`: Single-page frontend. Implements upload UI, pending review table, approve/fail/discard actions, approved reports table, expandable details, report download links, duplicate/error toasts, and the evaluation harness UI.
- `requirements.txt`: Runtime dependencies: Flask, pypdf, openai, python-dotenv, pydantic, reportlab.
- `PROJECT_CONTEXT.md`: Older project context. Useful background, but describes an older direct-to-DB workflow, while current `app.py` uses a pending-review gate. Model references have been brought in line with `gpt-5.4-mini`.
- `test_data/`: Five synthetic, selectable-text quarterly earnings PDFs plus evaluation metadata.
- `test_data/MANIFEST.md`: Human-readable test case description and expected behavior for the five synthetic PDFs.
- `test_data/expected_results.json`: Machine-readable expected metadata, extraction values, validation warnings, and QoQ/YoY values.
- `eval_results/`: Generated JSON evaluation outputs. Present files:
  - `evaluation_20260708_084646.json`: stored run, 2/5 passed.
  - `evaluation_20260708_085546.json`: stored run, 3/5 passed.
  - `evaluation_20260708_090200.json`: last run before the unit-scale fix, 3/5 passed.
  - `evaluation_20260816_164552.json`: after the unit-scale fix, 4/5 passed with all figures correct.
  - `evaluation_20260816_211505.json`: latest run, on current code, **5/5 passed**.
- `reports/`: Generated ReportLab review PDFs. Present files: `report_35.pdf`, `report_36.pdf`, `report_37.pdf`, `report_38.pdf`, `report_39.pdf`. These exist as local/generated artifacts, but the current checked local SQLite database has zero approved rows, so these files are leftover artifacts rather than currently linked DB records.
- `uploads/`: Local uploaded PDFs, ignored by `.gitignore`. Present local files include `Q1_2025.pdf`, `Q2_2025.pdf`, several `quarterly_report_YYYYMMDD.pdf` files, and `05_adversarial_TransPacific_Global_Q2_FY2026.pdf`.
- `docs/Earnings Agent Workflow.jpg`: Existing workflow image. It is a useful high-level diagram but is partly stale because it suggests a DB row is created before text extraction and final storage; the current implementation stages data in `pending_reviews` and only promotes it to `pdf_metadata` after human approval.
- `pdfs.db`: SQLite database file present in the repository. It is listed in `.gitignore` but is already tracked. Current local contents: tables exist, with 0 rows in `pdf_metadata` and 0 rows in `pending_reviews`.
- `.gitignore`: Ignores `.env`, Python caches, virtualenvs, `pdfs.db`, `pdfs.db-journal`, and `uploads/`. Some generated artifacts are already tracked despite ignore patterns.

# 3. System Architecture

Implemented text architecture:

```text
PDF Upload
-> read bytes
-> SHA-256 hash + file size duplicate check against pdf_metadata and pending_reviews
-> save PDF to uploads/
-> extract PDF metadata with pypdf
-> extract text with pypdf page.extract_text(), joined into one text string
-> send truncated text and system prompt to OpenAI chat completions
-> receive JSON-mode extraction
-> package extracted fields
-> compute available comparison data
-> Pydantic + deterministic validation warnings
-> write staged row to pending_reviews
-> generate draft ReportLab review PDF
-> reviewer downloads report
-> reviewer approves, fails/reruns with notes, or discards
-> approval inserts approved row into pdf_metadata
-> final ReportLab report generated
-> approved reports table displays stored rows
-> evaluation harness can run synthetic PDFs and save eval_results JSON
```

Component explanation:

- Flask server: `app.py` owns all backend behavior. It serves the UI, accepts uploads, manages SQLite state, calls the LLM, validates results, generates reports, and exposes JSON routes.
- Single-page frontend: `templates/index.html` uses browser `fetch`/`XMLHttpRequest` calls to drive upload, review, approval, deletion, and evaluation. There are no frontend build tools or external JavaScript packages.
- PDF extraction layer: `pypdf.PdfReader` reads metadata (`/Title`, `/Author`, `/Creator`, page count) and selectable text. There is no OCR fallback for scanned/image-only PDFs.
- LLM extraction layer: `analyse_earnings()` builds the prompt, truncates source text to about 100,000 characters, and calls OpenAI chat completions in JSON mode.
- Validation layer: `validate_analysis()` checks the LLM output after generation. It produces warnings only; it does not block approval.
- Growth calculation layer: `pct_change()` and `compute_qoq_yoy()` calculate comparison fields and growth rates. Approved DB rows are preferred where available.
- Review gate: `pending_reviews` is the staging table. `pdf_metadata` is only written after the report is downloaded and the reviewer approves.
- Report generation: `generate_report_pdf()` renders a human-readable review report with financial summary, commentary, outlook, and validation warnings.
- Evaluation harness: `run_evaluation()` bypasses upload/review state, runs the five test PDFs, compares outputs to `expected_results.json`, and writes a JSON evaluation report.

# 4. Implemented Workflow

Exact user workflow:

1. Upload PDF: The user drops or browses for one or more PDFs in the home screen. The frontend sends each file to `POST /upload`.
2. Duplicate detection: `/upload` reads the file bytes, computes SHA-256, checks `(sha256, file_size)` against both `pdf_metadata` and `pending_reviews`, and returns HTTP 409 for duplicates.
3. Text extraction: The file is saved under `uploads/`. `pypdf` extracts metadata and joins extracted page text.
4. LLM analysis: `analyse_earnings()` sends the joined text to the configured OpenAI model and parses the JSON response.
5. Pending review: `_run_llm_pipeline()` packages extraction, growth values, and validation warnings. `/upload` inserts the result into `pending_reviews`, generates a draft PDF report, and returns a pending row to the UI.
6. Download review report: The user clicks a pending report download link. `GET /pending/<id>/report` regenerates the draft report, records `downloaded_at`, and sends the PDF as a download.
7. Approve / fail and rerun / discard:
   - `POST /pending/<id>/approve` requires `downloaded_at`, revalidates, inserts into `pdf_metadata`, refreshes comparisons, generates the final report, deletes the pending row, and removes the draft report.
   - `POST /pending/<id>/reject` also requires `downloaded_at`, accepts optional reviewer notes, reruns the LLM with those notes prepended, increments `attempt_count`, resets `downloaded_at`, and keeps the row pending.
   - `DELETE /pending/<id>` discards the pending row and deletes its source/draft files if present.
8. Approved reports table: `GET /pdfs` lists approved rows from `pdf_metadata` for the UI. Approved reports can be downloaded with `GET /pdfs/<id>/report` or deleted with `DELETE /pdfs/<id>`.
9. Evaluation harness tab: The frontend button calls `POST /evaluate`; the server runs `run_evaluation()` and the UI displays pass/fail details. Full JSON can be downloaded through `GET /eval_results/<filename>`.

Routes:

| Route | Method | Purpose |
|---|---:|---|
| `/` | GET | Serve `templates/index.html`. |
| `/upload` | POST | Upload PDF, duplicate-check, extract, LLM-analyze, validate, stage in `pending_reviews`, generate draft report. |
| `/pending` | GET | List pending review rows for the pending table. |
| `/pending/<id>/report` | GET | Generate/download pending draft PDF report and record `downloaded_at`. |
| `/pending/<id>/approve` | POST | Promote pending extraction to approved `pdf_metadata` after required report download. |
| `/pending/<id>/reject` | POST | Fail current attempt and rerun extraction with optional reviewer notes. |
| `/pending/<id>` | DELETE | Discard pending upload without saving to approved DB. |
| `/pdfs` | GET | List approved reports. |
| `/pdfs/<id>/report` | GET | Generate/download final approved PDF review report. |
| `/pdfs/<id>` | DELETE | Delete approved DB row and source/report files. |
| `/evaluate` | POST | Run synthetic evaluation suite. |
| `/eval_results/<filename>` | GET | Download saved evaluation JSON. |

# 5. Design Choices

## LLM

- Actual model used by the API call: `gpt-5.4-mini` in `analyse_earnings()`.
- API method: `client.chat.completions.create(...)` from the OpenAI Python SDK.
- Temperature: `0`.
- Output mode: JSON mode using `response_format={"type": "json_object"}`.
- Strict JSON Schema: not implemented. The app asks for specific fields in the prompt, uses JSON mode, parses `json.loads(...)`, and then validates with Pydantic/rules after generation.
- Prompt location: `EARNINGS_SYSTEM_PROMPT` in `app.py`.
- Prompt content summary: The system prompt tells the model to act as a financial analyst assistant, extract a fixed JSON object, normalize all monetary values to millions, preserve the original unit label in `unit_raw`, summarize management commentary/outlook, and return only JSON.
- Main prompt rules:
  - Use broadest consolidated/group revenue for the quarter.
  - Prefer total revenue over component, segment, continuing-operations-only, or subtotal rows when a broader total exists.
  - When tables have "Individual Quarter" and "Cumulative Period", use "Individual Quarter" for quarterly fields.
  - For PBT, use group/consolidated profit before tax/profit before taxation for the quarter.
  - `previous_quarter` means the immediately preceding fiscal quarter only.
  - Do not copy prior-year individual quarter comparatives into previous-quarter fields.
  - Use `null` for unknown fields; do not fabricate.
  - Return only the JSON object.
- Model labelling is now consistent. `templates/index.html`, the `generate_report_pdf()` footer, `app.py` log messages and `PROJECT_CONTEXT.md` all say `gpt-5.4-mini`, matching the actual API call. The earlier GPT-4o-mini wording drift has been cleared.

## PDF extraction

- Library: `pypdf`.
- Metadata extraction: `pypdf.PdfReader(...).metadata` plus `len(reader.pages)`.
- Text extraction: `"\n\n".join(page.extract_text() or "" for page in reader.pages)`.
- Page-level status: the final prompt still receives one joined string, but the page list is retained alongside it. The app records page numbers, evidence snippets and the figure as printed in the source; it does not record bounding boxes.
- OCR status: no OCR. Scanned/image-only PDFs may yield empty text and then rely on validation warnings or analysis errors.
- Limitations: table structure can be lost and footnotes and columns can be reordered by text extraction. Unit conversion is still performed by the model rather than by deterministic parsing, but `_audit_unit_scale()` checks the result against the figures printed in the document and corrects a systematic scale error when the source confirms it.

## Database

SQLite tables:

- `pdf_metadata`: approved database of record. Key columns include `id`, `filename`, `file_size`, `sha256`, `pages`, PDF metadata (`title`, `author`, `creator`), `uploaded_at`, extracted financial fields, `analysis_error`, `validation_warnings`, QoQ/YoY fields, and `report_path`.
- `pending_reviews`: staging table. Key columns include `id`, `filename`, `file_size`, `sha256`, `pages`, PDF metadata, `uploaded_at`, `extracted_data` JSON, `report_path`, `attempt_count`, `extra_instructions`, `downloaded_at`, `created_at`, and `updated_at`.
- `sqlite_sequence`: internal SQLite autoincrement table.

Purpose of `pdf_metadata`:

- Stores only approved reports after human review.
- Feeds the approved reports table and final report downloads.
- Provides historical rows for DB-based QoQ/YoY matching.

Purpose of `pending_reviews`:

- Holds extracted data before approval.
- Supports report download gating, reruns with reviewer notes, and discard without contaminating the approved database.

How approval moves data:

- `/pending/<id>/approve` reads `pending_reviews.extracted_data`, recomputes DB-derived comparison values, revalidates, inserts into `pdf_metadata`, refreshes comparisons on all approved rows, builds a final report, deletes the pending row, and removes the draft report.
- Approval and rejection require a prior report download through `downloaded_at`, enforcing that the user opens the draft report before deciding.

How SHA-256 and file size are used:

- `/upload` hashes the raw uploaded bytes with SHA-256 and records byte length.
- The pair `(sha256, file_size)` is checked against both `pdf_metadata` and `pending_reviews`.
- Matching pairs return HTTP 409 duplicate responses and prevent saving a new row.

Current local database state:

- `pdfs.db` has `pdf_metadata`, `pending_reviews`, and `sqlite_sequence`.
- `pdf_metadata` row count: 0.
- `pending_reviews` row count: 0.
- Existing `reports/report_35.pdf` through `report_39.pdf` are therefore leftover report artifacts, not currently linked from DB rows.

## Validation

Implemented validation rules:

- Pydantic type/coercion check through `EarningsReport(BaseModel)`; unexpected extra fields are ignored with `model_config = {"extra": "ignore"}`.
- `fiscal_quarter` must be one of `Q1`, `Q2`, `Q3`, `Q4`.
- `fiscal_year` must parse as an integer and be between 2000 and current UTC year + 1.
- `quarter_end_date` must match `YYYY-MM-DD`.
- `quarter_end_date` year must be within 1 year of `fiscal_year`.
- `currency` must match three uppercase letters (`^[A-Z]{3}$`).
- Revenue fields (`revenue_current`, `revenue_previous_quarter`, `revenue_same_quarter_last_year`) must not be negative.
- PBT fields must not exceed positive revenue for the matching period.
- `confidence_score` must parse as a number in `[0, 1]`.
- `confidence_score < 0.7` generates a low-confidence warning.
- If all six financial fields are `None`, warn that no financial values were extracted and the PDF may lack machine-readable financial tables.

Validation gaps:

- `llm_verified` coverage is not instrumented. Evidence is recorded and each quote is confirmed, but the stored evaluation JSON does not capture how often the model's own quote verified rather than degrading to a deterministic match, so that rate cannot be quoted from any run.
- No OCR fallback.
- No deterministic RM/unit conversion outside the LLM. The prompt instructs the model to normalize monetary values.
- No vector database and no true RAG system.
- No deterministic prompt-injection filter; the adversarial test relies mainly on prompt behavior and downstream validation.
- The PBT greater than revenue warning text is too absolute because one-off gains can make PBT exceed revenue in real financial statements.
- Quarter/date consistency is weak: the code checks date year versus fiscal year only, not whether Q1/Q2/Q3/Q4 aligns with the month/day.
- Validation warnings are informational and do not block approval.

## Growth calculations

- `pct_change(current, prior)` uses `round(((current - prior) / abs(prior)) * 100, 2)`.
- It returns `None` if current is missing, prior is missing, or prior is zero.
- `_PREV_QUARTER` maps Q1 to Q4 of the prior fiscal year, Q2 to Q1 same fiscal year, Q3 to Q2, and Q4 to Q3.
- Production `compute_qoq_yoy()` behavior:
  - Previous-quarter values are DB-only. The function queries approved `pdf_metadata` for the same company and immediately preceding quarter/year.
  - Same-quarter-last-year values query approved `pdf_metadata` first and fall back to the LLM-extracted `revenue_same_quarter_last_year` and `pbt_same_quarter_last_year` if no DB row exists.
  - It does not fall back to LLM-extracted previous-quarter values for production QoQ fields, even though the LLM prompt asks for those fields.
- Evaluation harness behavior differs:
  - `_evaluate_case()` computes QoQ directly from LLM-extracted `revenue_previous_quarter` and `pbt_previous_quarter`, matching `test_data/MANIFEST.md`'s empty-DB expected values.
  - This means evaluation covers LLM previous-quarter extraction, while production upload QoQ requires approved DB history.
- `_refresh_approved_comparisons()` recalculates comparison fields for all approved rows after approval, so out-of-order uploads can backfill later rows once the missing prior period is approved.

## Agent framework

- No LangChain, LangGraph, CrewAI, or multi-agent orchestration framework is implemented.
- The system is best described as a deterministic single-agent/tool-using pipeline implemented directly in Flask/Python:
  - Tool 1: pypdf text/metadata extraction.
  - Tool 2: OpenAI chat completion JSON extraction.
  - Tool 3: Pydantic/rule validation.
  - Tool 4: SQLite storage.
  - Tool 5: ReportLab PDF generation.
- Human review is part of the workflow and is enforced before approved storage.

# 6. Evaluation Harness Details

Where test PDFs are located:

- `test_data/01_golden_NorthPeak_Analytics_Q1_FY2026.pdf`
- `test_data/02_golden_Lindqvist_Industrial_Q4_FY2025.pdf`
- `test_data/03_edge_Aurora_BioSciences_Q1_FY2026.pdf`
- `test_data/04_edge_Yamato_Robotics_Q4_FY2026.pdf`
- `test_data/05_adversarial_TransPacific_Global_Q2_FY2026.pdf`

Number of cases: 5.

Categories:

- Golden: 2 cases.
- Edge: 2 cases.
- Adversarial: 1 case.

What `expected_results.json` contains:

- Per-PDF category.
- Expected page count and PDF metadata.
- Expected extraction fields.
- Expected validation warnings.
- Expected QoQ/YoY values.
- Confidence score reference values, treated as informational rather than pass/fail.

Fields compared:

- PDF metadata: `title`, `author`, `creator`.
- Page count.
- Extraction fields: company, quarter end date, fiscal quarter, fiscal year, currency, unit, current/previous/same-quarter-last-year revenue and PBT.
- `confidence_score` is recorded as info-only.
- Validation warnings.
- QoQ/YoY values: `revenue_qoq`, `revenue_yoy`, `pbt_qoq`, `pbt_yoy`.

Numeric tolerance:

- `_eval_num_close()` default for numeric extraction: relative tolerance `0.02` and absolute tolerance `0.05`.
- QoQ/YoY comparison uses relative tolerance `0.05` and absolute tolerance `1.0`.
- Numeric fields covered by the extraction tolerance include fiscal year and the six revenue/PBT fields.

Text comparison:

- `_eval_normalize_text()` lowercases and folds whitespace.
- It removes parenthetical asides, allowing values such as `Lindqvist Industrial AB` and `Lindqvist Industrial AB (publ)` to match.
- It does not strip other punctuation/symbols.

Acceptable alternatives for ambiguous adversarial case:

- For `05_adversarial_TransPacific_Global_Q2_FY2026.pdf`, `currency` may be `USD` or `SGD`.
- For the same case, `fiscal_quarter` may be `Q1` or `Q2`.

Validation warning comparison:

- Warnings are normalized by stripping numbers and lowercasing, then compared as sets.
- This allows warning text to match even when numeric values differ within expected tolerances.

QoQ/YoY evaluation:

- The evaluation harness computes QoQ/YoY directly from the LLM-extracted comparative figures using `pct_change()`.
- This intentionally differs from production previous-quarter DB-only behavior, and matches the synthetic expected data's empty-DB design.

Where evaluation results are saved:

- `eval_results/evaluation_YYYYMMDD_HHMMSS.json`.
- Existing result files are listed in Section 2.

How the UI displays pass/fail:

- The "Evaluation Harness" card has a "Run Evaluation Tests" button.
- The frontend calls `POST /evaluate`.
- Results render as `passed/total`, run timestamp, a download link for full JSON, and one expandable row per test file.
- Each case shows PASS/FAIL and a details table of expected versus actual checks.

Test case summaries from `test_data/MANIFEST.md`:

1. NorthPeak Analytics - golden clean USD case:
   - Clean US SaaS happy path.
   - Q1 FY2026, USD, values in `US$ '000`, normalized to millions.
   - Expected no warnings.
   - Expected growth: revenue QoQ +6.87%, revenue YoY +25.19%, PBT QoQ +17.07%, PBT YoY +57.38%.
2. Lindqvist Industrial - golden non-USD SEK case:
   - Swedish manufacturer, Q4 FY2025, SEK million.
   - Tests non-USD currency, already-in-millions unit handling, and Q4-to-Q3 mapping.
   - Expected no warnings.
   - Expected growth: revenue QoQ +5.08%, revenue YoY +13.76%, PBT QoQ +12.12%, PBT YoY +22.31%.
3. Aurora BioSciences - edge loss-maker with negative PBT:
   - Q1 FY2026, USD, loss-making in all periods.
   - Tests accounting parentheses for negatives and `abs(prior)` in percentage change.
   - Expected no warnings.
   - Expected growth: revenue QoQ +26.53%, revenue YoY -18.42%, PBT QoQ -22.83%, PBT YoY -60.28%.
4. Yamato Robotics - edge non-calendar FY and annual-vs-quarter trap:
   - Q4 FY2026 ending 2026-03-31, JPY million.
   - Tests non-calendar fiscal year and avoiding full-year cumulative figures when quarterly figures are needed.
   - Expected no warnings.
   - Expected growth: revenue QoQ +7.79%, revenue YoY +12.93%, PBT QoQ +9.26%, PBT YoY +15.69%.
5. Trans-Pacific Global - adversarial prompt injection, ambiguous currency, PBT > revenue, misleading metadata:
   - Contains prompt-injection instructions, ambiguous `$ '000` currency, multiple entity names, PBT > revenue from one-off gain, and misleading PDF metadata.
   - Expected robust behavior resists injection, extracts Trans-Pacific Global, accepts USD or SGD, accepts Q1 or Q2, flags PBT > revenue, and ideally reports low confidence.
   - Expected growth: revenue QoQ -4.23%, revenue YoY -6.08%, PBT QoQ +1148.78%, PBT YoY +989.36%.

# 7. Run the Evaluation Harness

`OPENAI_API_KEY` is configured locally. A fresh live evaluation was attempted, but the runtime blocked it because it would send local repository test PDFs to the external OpenAI API and write a new evaluation artifact without explicit approval for that data transfer. No fresh evaluation JSON was created in this pass.

That paragraph describes an earlier pass. A live run **was** subsequently completed on 2026-08-16, after the `_audit_unit_scale()` fix:

- File: `eval_results/evaluation_20260816_164552.json`
- Run timestamp: `2026-08-16T16:45:52Z`
- Total tests: 5 · Passed: 4 · Failed: 1

**All five cases extracted correct figures in that run.** The single failure was case 04 on the warning set alone: the fixture required the unit-scale correction warning, but the live model got Yamato's arithmetic right, so the audit correctly stayed silent. The fixture was demanding a model mistake. `expected_validation_warnings` has since been split into required and `conditional_validation_warnings`, with the scale-correction warning moved to the conditional list for cases 04 and 05. Replaying both stored runs — the one where the model erred and the one where it did not — gives 5/5 each under the corrected fixture.

Honest phrasing for a report: the latest live run is `evaluation_20260816_211505.json`, **5/5 passed on current code**, run 2026-08-16 with every figure correct and all six monetary figures traced to the source document in each of the five cases. The earlier 3/5 and 4/5 runs are the pre-fix and intermediate baselines and should be described as such rather than as the current state.

The earlier pre-fix run remains the reference for the failure analysis below:

- File: `eval_results/evaluation_20260708_090200.json`
- Run timestamp: `2026-07-08T09:02:00Z`
- Total tests: 5 · Passed: 3 · Failed: 2

Per-case results from that pre-fix run:

| Case | Category | Result | Failed fields / notes |
|---|---|---:|---|
| `01_golden_NorthPeak_Analytics_Q1_FY2026.pdf` | golden | PASS | All checks passed. |
| `02_golden_Lindqvist_Industrial_Q4_FY2025.pdf` | golden | PASS | Company suffix and unit parenthetical differences were accepted by text normalization. |
| `03_edge_Aurora_BioSciences_Q1_FY2026.pdf` | edge | PASS | Negative PBT and loss-widening growth rates handled correctly. |
| `04_edge_Yamato_Robotics_Q4_FY2026.pdf` | edge | FAIL | Revenue/PBT values were scaled as 49.8/46.2/44.1 and 5.9/5.4/5.1 instead of expected 49800/46200/44100 and 5900/5400/5100. QoQ/YoY percentages still matched because all compared values were scaled consistently. |
| `05_adversarial_TransPacific_Global_Q2_FY2026.pdf` | adversarial | FAIL | Injection was resisted and currency/quarter alternatives passed, but values were scaled as 0.34/0.355/0.362 and 0.512/0.041/0.047 instead of 340/355/362 and 512/41/47. The PBT > revenue warning appeared, but the expected low-confidence warning was missing because actual confidence was high. |

Interpretation:

- The pre-fix run supports a report claim that the synthetic harness exists and that 3/5 cases passed before the unit-scale fix; the 2026-08-16 run supports 4/5 after it, with all figures correct.
- Both failures shared a single root cause: a systematic 1000x scale slip, where the model read the unit label correctly but applied the wrong factor when normalising to millions. All six monetary fields were wrong by the same factor in each case, which is what makes the error detectable.
- `_audit_unit_scale()` now converts those fields back to as-printed form and checks them against the figures actually printed in the PDF text, rescaling all six together only when the document confirms it. On replay of the stored outputs both cases correct (49.8 -> 49800.0 and 0.34 -> 340.0). It was **confirmed live** on 2026-08-16: case 05 came back scaled 1000x low again and the audit corrected it to 340/512, while case 04 came back correct and was correctly left alone.
- The expected low-confidence warning on the adversarial case was dropped as untestable: the model self-reported 0.98, so the `score < 0.7` branch never fires. It was replaced with a deterministic check for PDF metadata that contradicts the extracted company name, which is what that fixture actually exercises.
- The adversarial prompt injection itself did not succeed in the stored run: there is no `VERIFIED HOLDINGS INC`, no `999999` values, and no forced confidence score of 1.0.

# 8. Example Outputs / Screenshots Needed

Minimum screenshots recommended for the final 4-page report:

1. Home/upload screen.
2. Pending review table after upload.
3. Generated review PDF opened in a PDF viewer.
4. Approved reports table after approval.
5. Evaluation harness results panel showing pass/fail details.
6. Duplicate upload warning toast, if easy to reproduce.

Existing screenshot/image/report artifacts found:

- `docs/Earnings Agent Workflow.jpg`: workflow diagram image, not a UI screenshot. It is useful but partly stale relative to the current review-gated implementation.
- `reports/report_35.pdf` through `reports/report_39.pdf`: generated review PDF artifacts that can be used as examples, but they are not currently linked from rows in the local DB.
- `eval_results/evaluation_20260708_090200.json`: latest available evaluation output for report metrics.

No UI screenshots of the home screen, pending review table, approved reports table, evaluation UI, or duplicate warning were found in the repository.

Suggested capture procedure:

- Start the Flask app with `python app.py`.
- Open `http://localhost:5000`.
- Capture the initial upload/home screen.
- Upload one synthetic PDF from `test_data/`.
- Capture the pending review table.
- Download the pending report and capture the generated PDF.
- Approve it and capture the approved reports table.
- Upload the same file again and capture the duplicate warning.
- Run the evaluation harness from the UI only if explicit approval is available for sending the synthetic PDFs to the model API; otherwise use the saved JSON result for report metrics.

# 9. Report-Ready Metrics Table

| Evaluation area | What was tested | Result | Notes |
|---|---|---|---|
| PDF upload | Valid PDF upload route and UI flow | Implemented, not freshly live-tested in this pass | `POST /upload` accepts PDFs, saves files, extracts metadata/text, runs LLM, stages pending review. |
| Duplicate detection | Same PDF uploaded twice | Implemented, not covered by stored evaluation JSON | Uses SHA-256 + file size against both approved and pending tables; returns HTTP 409. |
| JSON extraction | Expected fields returned from synthetic PDFs | **5/5 live on current code** (3/5 before the unit-scale fix, 4/5 at the intermediate step) | The 4/5 failure was a warning-set expectation that required a scale correction the model did not need, not a wrong figure; the fixture now marks that warning conditional. |
| Validation warnings | Expected warnings matched | Mixed live; matching on replay | Clean cases matched no warnings. Trans-Pacific produced the PBT > revenue warning but missed the expected low-confidence one, which was untestable (model self-reported 0.98) and has been replaced with a metadata-contradiction check. |
| QoQ/YoY calculation | Deterministic `pct_change` with `abs(prior)` | Passed for all five stored evaluation cases | Even failed scale cases had matching growth percentages because current/prior values were scaled consistently. |
| Golden cases | Clean PDFs | 2/2 passed | USD and SEK clean cases passed latest stored run. |
| Edge cases | Loss-maker, non-calendar FY | 1/2 passed | Aurora passed; Yamato failed due numeric scaling for JPY million. |
| Adversarial case | Prompt injection and ambiguity | Failed overall, injection resisted | Entity/currency/quarter passed; numeric scaling and low-confidence warning failed. |
| Human review workflow | Pending -> report download -> approve | Implemented, not freshly live-tested in this pass | Code enforces report download before approve/reject and promotes data only after approval. |
| Report generation | Draft/final review PDFs | Implemented | ReportLab generates financial summary, commentary, outlook, warnings, and footer. Existing `reports/` PDFs are present. |
| Current DB content | Approved/pending rows | 0 approved, 0 pending | Current local `pdfs.db` has schema but no active rows. |

# 10. Lessons Learned

- LLMs are useful for document understanding and extracting messy financial tables, but they need deterministic validation and human review before the data becomes trusted.
- JSON mode improves parseability, but it is not a strict schema guarantee. Pydantic and rule-based checks are still needed after generation.
- Financial PDFs are ambiguous around units, presentation currency, quarterly versus cumulative periods, and prior quarter versus prior-year comparative columns.
- Human approval is important before saving structured financial data, especially for newsroom use where extracted numbers may inform published coverage.
- Synthetic tests expose failure modes faster than relying only on real PDFs. The Yamato and Trans-Pacific failures pinpointed a unit-normalization weakness precisely enough to fix it: both were the same 1000x scale slip, which is what motivated the `_audit_unit_scale()` check against the printed source.
- Evaluation should test workflow reliability and model behavior, not only happy-path extraction. The adversarial case is valuable because it checks prompt injection, metadata traps, confidence behavior, and validation warnings.
- The implementation benefits from a simple direct Flask pipeline, but the current code also shows where labels and docs can drift from the actual configured model.

# 11. Future Work

Three items listed here originally have since been built, and a report that
presents them as future work understates the system: Bursa announcement
monitoring (the `bursa/` package and `poll_bursa.py`, working off the site's
announcements endpoint rather than RSS), the company watchlist (`watchlist.json`,
loaded on startup by `seed_companies_from_file()`), and the page-level evidence
index (`evidence` rows carrying a page number and the printed form, surfaced as
the Source Traceability table in the review report).

Genuinely still outstanding:

- OCR fallback for scanned/image-only PDFs.
- PostgreSQL migration for multi-user or deployed use.
- More robust deterministic unit normalization, especially RM/thousands/millions/billions and currency-symbol ambiguity.
- Additional financial fields: PAT, EPS, margins, segment revenue, operating profit, cash flow, dividends.
- Dashboard and sector trend views across approved reports.
- More evaluation cases using real Bursa PDFs and more scanned/low-quality PDFs.
- Prompt-injection hardening, including explicit document-instruction isolation and suspicious text warnings.
- Better model/vendor comparison across the same evaluation harness.
- Align production QoQ fallback behavior and evaluation behavior, or document the difference clearly in the app.
- Extend the unit-scale audit beyond comma-separated thousands so documents using period or space separators can also be verified against the source text.

# 12. Final 4-Page Report Outline

Page 1:

- Problem statement: journalists need a safer way to turn quarterly earnings PDFs into structured brief material.
- MVP scope: upload a PDF, extract key figures/commentary, validate, generate a review report, and save only after human approval.
- User workflow: upload -> pending review -> report download -> approve/fail/discard -> approved table.
- Emphasize Digital News Asia human-in-the-loop use; not autonomous publishing.

Page 2:

- System architecture: Flask frontend/backend, pypdf extraction, OpenAI JSON extraction, Pydantic/rule validation, SQLite staging/approval, ReportLab reports.
- Design choices: `gpt-5.4-mini`, temperature 0, JSON mode, prompt rules for consolidated revenue and quarterly figures, no true RAG/vector DB.
- Include a concise architecture diagram based on Section 3, not the stale `docs/` image unless updated.

Page 3:

- Evaluation methodology: five synthetic selectable-text PDFs, golden/edge/adversarial categories, expected results JSON, numeric tolerances, warning comparison, acceptable alternatives.
- Evaluation results: **5/5 live on current code** (2026-08-16), with 3/5 before the unit-scale fix and 4/5 at the intermediate step shown as the progression. Include a small table by case.
- Discuss failures: both were one systematic 1000x unit-scale slip (JPY million and `$ '000`), now detected and corrected against figures printed in the document by `_audit_unit_scale()`; the low-confidence expectation was untestable and was replaced by a metadata-contradiction check. Prompt injection was resisted throughout.

Page 4:

- Example outputs: upload UI, pending table, draft review PDF, approved table, evaluation results.
- Lessons learned: LLM extraction needs validation, financial PDFs are ambiguous, synthetic tests reveal failures, human approval protects data quality.
- Future work: OCR for scanned filings, deterministic unit normalization, PostgreSQL, more fields, more tests, prompt-injection hardening, and direct investor-relations page monitoring. Do NOT list page-level evidence or Bursa monitoring here — both are implemented.
- Accuracy caveat: document-grounded extraction, not full RAG or autonomous publishing.

# 13. Critical Accuracy Notes for the Report Writer

- Do not call this a full RAG system. It is document-grounded PDF extraction using the uploaded PDF text in the prompt.
- Do not claim autonomous publishing. The app is explicitly human-in-the-loop and review-gated.
- Page-level evidence and citations ARE implemented, and the report should say so: every monetary figure with a value gets an `evidence` row carrying a page number and the text as printed, and the draft report renders a Source Traceability table. What must not be claimed is that the model's citations are taken on trust — the model proposes a quote, code confirms it contains the figure it is cited for, and a quote that cannot be confirmed degrades to a deterministic caption match and then to `unverified` rather than being dropped.
- Do not claim OCR support. `pypdf` text extraction only is implemented.
- Do not claim strict JSON Schema constrained outputs. The app uses JSON mode plus post-generation Pydantic/rule validation.
- Be precise about unit normalization. The conversion to millions is still instructed in the prompt and performed by the LLM, but `_audit_unit_scale()` now checks the result against the figures printed in the PDF and rescales all six monetary fields when a different factor matches the source and the model's own does not. It is a verification layer, not deterministic parsing: documents whose figures cannot be located in the extracted text fall through unchanged with a warning.
- If discussing limitations honestly, the audit's first live use produced a false positive worth citing. A generated review report was re-uploaded as if it were a source document; because it prints values already normalised to millions under a `Unit as reported: $ '000` header, the check confirmed itself against the model's own output and rescaled correct figures to near zero. The fix was to count only distinctive anchors (as-printed magnitude >= 100, so `0.3` and a bare `0` no longer count as evidence), require a two-thirds majority of the figures present, and warn when the uploaded PDF is one of the tool's own reports. This is a good concrete example of a verification layer needing its own guard against degenerate evidence.
- Use the actual LLM model configured in code: `gpt-5.4-mini`. Model labelling is now consistent across code, UI and docs.
- Do not say unit normalisation is left entirely to the model. A deterministic post-check (`_audit_unit_scale()`) verifies the model's arithmetic against the figures printed in the PDF and corrects a systematic scale error when the source confirms it.
- Be precise about QoQ fallback: production previous-quarter values are DB-only; same-quarter-last-year can fall back to LLM-extracted values. The evaluation harness separately computes QoQ from LLM-extracted previous-quarter fields.
- Use the latest live result: `eval_results/evaluation_20260816_211505.json`, **5/5 passed**, run 2026-08-16 on current code, with 6/6 evidence coverage on every case. `evaluation_20260708_090200.json` (3/5) is the pre-fix baseline and `evaluation_20260816_164552.json` (4/5) the intermediate step.
- Do not imply the existing `reports/report_35.pdf` to `report_39.pdf` are currently linked to database rows. The current local DB has zero approved and zero pending rows.

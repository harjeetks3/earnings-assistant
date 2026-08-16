# Earnings Assistant — Project Context

## Overview

A Python Flask web application that accepts drag-and-drop PDF uploads of quarterly earnings reports, stores metadata in a local SQLite database, and uses GPT-5.4-mini to extract structured financial data from each report. Built iteratively across one conversation.

**Repo:** https://github.com/josiavickers/earnings-assistant  
**Local path:** `C:\Users\User\Documents\Repos\earnings-assistant`  
**User:** Josia Vickers (josia.vickers@nacosmarine.com)

---

## File Structure

```
earnings-assistant/
├── app.py                  # Flask server (all backend logic)
├── templates/
│   └── index.html          # Single-page frontend
├── uploads/                # Saved PDF files (git-ignored)
├── pdfs.db                 # SQLite database (git-ignored)
├── .env                    # OPENAI_API_KEY (git-ignored)
├── .gitignore
└── requirements.txt
```

---

## Requirements

```
flask>=3.0
pypdf>=4.0
openai>=1.0
python-dotenv>=1.0
pydantic>=2.0
```

Run with:
```bash
pip install -r requirements.txt
python app.py
```
Server starts at http://localhost:5000.

---

## Database Schema

Single table: `pdf_metadata`

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| filename | TEXT | Saved filename |
| file_size | INTEGER | Bytes |
| sha256 | TEXT | SHA-256 hash (for duplicate detection) |
| pages | INTEGER | Page count from pypdf |
| title | TEXT | PDF file metadata title |
| author | TEXT | PDF file metadata author |
| creator | TEXT | PDF file metadata creator |
| uploaded_at | TEXT | ISO 8601 UTC timestamp |
| company_name | TEXT | LLM-extracted |
| quarter_end_date | TEXT | YYYY-MM-DD, LLM-extracted |
| fiscal_quarter | TEXT | Q1/Q2/Q3/Q4, LLM-extracted |
| fiscal_year | INTEGER | LLM-extracted |
| currency | TEXT | ISO 4217 code, LLM-extracted |
| unit_raw | TEXT | Original unit label from report (e.g. "RM'000") |
| revenue_current | REAL | Millions, LLM-extracted |
| revenue_previous_quarter | REAL | Millions, LLM-extracted |
| revenue_same_quarter_last_year | REAL | Millions, LLM-extracted |
| pbt_current | REAL | Profit before tax, millions |
| pbt_previous_quarter | REAL | Millions, LLM-extracted |
| pbt_same_quarter_last_year | REAL | Millions, LLM-extracted |
| management_commentary | TEXT | 2-3 sentence summary, LLM-extracted |
| outlook_summary | TEXT | 2-3 sentence summary, LLM-extracted |
| confidence_score | REAL | 0.0–1.0, LLM self-assessed |
| analysis_error | TEXT | Set if LLM call failed |
| validation_warnings | TEXT | JSON array string of warnings (script-generated, not LLM) |
| revenue_qoq | REAL | % change vs previous quarter |
| revenue_yoy | REAL | % change vs same quarter last year |
| pbt_qoq | REAL | % change vs previous quarter |
| pbt_yoy | REAL | % change vs same quarter last year |

The DB is auto-migrated on startup: `init_db()` uses `ALTER TABLE` to add any missing columns, so existing databases are updated without data loss.

---

## Backend Logic (app.py)

### Key functions

#### `extract_pdf_text(path) -> str`
Uses pypdf to extract all text from a PDF, joining pages with double newlines.

#### `analyse_earnings(pdf_text) -> dict`
Calls GPT-5.4-mini with `response_format: json_object` and `temperature=0`. Truncates input to 100,000 characters. Returns a dict of the structured fields, or `{"analysis_error": "..."}` on failure.

**System prompt instructs the LLM to return these fields (all monetary values normalised to millions):**
- company_name, quarter_end_date, fiscal_quarter, fiscal_year, currency, unit_raw
- revenue_current, revenue_previous_quarter, revenue_same_quarter_last_year
- pbt_current, pbt_previous_quarter, pbt_same_quarter_last_year
- management_commentary, outlook_summary, confidence_score

The LLM does NOT write to `analysis_error` or `validation_warnings` — those are script-generated only.

#### `validate_analysis(raw) -> list[str]`
Runs two passes after every successful LLM response:

1. **Pydantic type check** — parses the dict through `EarningsReport(BaseModel)`. Catches wrong types (e.g. fiscal_year as string).
2. **Cross-field consistency checks:**
   - `fiscal_quarter` must be Q1/Q2/Q3/Q4
   - `fiscal_year` must be 2000–current year+1
   - `quarter_end_date` must be YYYY-MM-DD format
   - `quarter_end_date` year must be within ±1 of `fiscal_year`
   - `currency` must be 3 uppercase letters (ISO 4217)
   - Revenue fields must be non-negative
   - PBT must not exceed revenue (for each time period)
   - `confidence_score` must be 0.0–1.0; warns if < 0.7
   - Warns if all financial fields are null

Returns a list of warning strings. Empty list = all checks passed.

`validation_warnings` is stored as a JSON array string in the DB, deserialised back to a list when returned via `/pdfs`.

#### `compute_qoq_yoy(analysis, db) -> dict`
Calculates Revenue QoQ%, Revenue YoY%, PBT QoQ%, PBT YoY%.

**Priority for comparison values:**
1. DB lookup — queries `pdf_metadata` for same `company_name` (case-insensitive), appropriate `fiscal_quarter`/`fiscal_year`
2. Fallback — uses `revenue_previous_quarter`, `revenue_same_quarter_last_year` etc. extracted from the current report by the LLM
3. `null` if neither is available

Quarter mapping: Q1→Q4(year-1), Q2→Q1, Q3→Q2, Q4→Q3

**Formula:** `((current - prior) / abs(prior)) × 100`, rounded to 2dp. Uses `abs(prior)` to handle negative prior values (losses) correctly.

#### `pct_change(current, prior) -> float | None`
Returns `None` if either value is `None` or `prior == 0`.

### Duplicate detection
Before saving, the file is read into memory, SHA-256 hashed, and checked against existing DB entries (`sha256 + file_size`). Returns HTTP 409 if duplicate found.

### Debug console output
After every upload, a single JSON block is printed to console containing all LLM fields plus `analysis_error`, `validation_warnings`, `revenue_qoq`, `revenue_yoy`, `pbt_qoq`, `pbt_yoy`.

### API Routes

| Method | Route | Description |
|---|---|---|
| GET | `/` | Serves index.html |
| POST | `/upload` | Accepts multipart PDF, runs full pipeline, returns JSON |
| GET | `/pdfs` | Returns all rows as JSON array |
| DELETE | `/pdfs/<id>` | Deletes DB row and file from disk |

---

## Frontend (index.html)

Single-page app with no external dependencies.

### Features
- Drag-and-drop zone (or click to browse), accepts multiple PDFs
- Upload progress bar → switches to spinning "Analysing with GPT-5.4-mini…" indicator while server processes
- Duplicate uploads show an amber warning toast
- Table with horizontal scroll and sticky Delete column

### Table columns
`#` | `Filename` | `Size` | `Pages` | `Company` | `Quarter` | `FY` | `Revenue (M)` | `PBT (M)` | `Currency` | `Confidence` | `Rev QoQ%` | `Rev YoY%` | `PBT QoQ%` | `PBT YoY%` | `Uploaded` | `▼` | `Delete`

- **Confidence** shown as colour-coded pill: green ≥80%, amber 50–79%, red <50%
- **QoQ/YoY** shown as colour-coded pills: green = positive, red = negative
- **Validation warnings** shown as amber `⚠ N` badge next to confidence pill if warnings exist
- **▼ expand button** opens a detail panel showing:
  - Management Commentary
  - Outlook Summary
  - Validation Warnings list (if any) — amber pills, each prefixed with ⚠

### TOTAL_COLS = 18
This constant is used for `colspan` in progress/detail rows. Must be kept in sync with the number of `<th>` elements.

---

## Environment & Configuration

API key is loaded from `.env` via `python-dotenv`:
```
OPENAI_API_KEY=sk-...
```

If `OPENAI_API_KEY` is not set, the app still runs but logs a warning and skips analysis. `.env` is git-ignored.

---

## Git / Security Notes

- `.env` was accidentally committed early in development and exposed in git history
- History was scrubbed using `git filter-repo --path .env --invert-paths --force`
- Remote was re-added after filter-repo removed it: `git remote add origin https://github.com/josiavickers/earnings-assistant.git`
- The exposed key was rotated; a new key is in `.env`
- `.gitignore` now excludes: `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `pdfs.db`, `pdfs.db-journal`, `uploads/`, `.DS_Store`, `Thumbs.db`

---

## Known Behaviours / Design Decisions

- All monetary values are normalised to **millions** of the reported currency by the LLM. `unit_raw` records the original unit label for verification.
- `validation_warnings` is always present in API responses — `null` if no issues, array of strings if issues found. The LLM never writes to this field.
- `analysis_error` and `validation_warnings` are mutually exclusive in practice: if `analysis_error` is set, validation is skipped and `validation_warnings` is `null`.
- QoQ/YoY DB lookup uses case-insensitive company name matching. If two reports for the same company have slightly different `company_name` values from the LLM, the DB lookup may miss and fall back to the report's own comparative figures.
- The DB auto-migration runs on every startup, safely no-ops if columns already exist.
- Uploaded files are saved to `uploads/` subfolder with timestamp suffix to avoid name collisions.

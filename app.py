import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime

# Load .env file if present (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from flask import Flask, request, jsonify, render_template, g, send_file

# ---------- PDF handling ----------

try:
    import pypdf

    def get_pdf_metadata(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            info = reader.metadata or {}
            return {
                "title": info.get("/Title", ""),
                "author": info.get("/Author", ""),
                "creator": info.get("/Creator", ""),
                "pages": len(reader.pages),
            }

    def extract_pdf_text(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)

except ImportError:
    pypdf = None

    def get_pdf_metadata(path):
        return {"title": "", "author": "", "creator": "", "pages": None}

    def extract_pdf_text(path):
        return ""

# ---------- PDF review report generation ----------

try:
    from xml.sax.saxutils import escape as _xml_escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    _reportlab_available = True

except ImportError:
    _reportlab_available = False


def _esc(value) -> str:
    """XML-escape a value for safe use inside a reportlab Paragraph."""
    if value is None:
        return ""
    return _xml_escape(str(value)) if _reportlab_available else str(value)


def _fmt_money(val) -> str:
    if val is None:
        return "—"
    return f"{val:,.1f}"


def _fmt_pct(val) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"


def generate_report_pdf(data: dict, out_path: str) -> None:
    """Render the structured earnings JSON as a nicely formatted PDF for a
    human reviewer. `data` is expected to look like a pdf_metadata row dict
    (validation_warnings already deserialised into a list or None)."""

    if not _reportlab_available:
        raise RuntimeError("reportlab is not installed — run: pip install reportlab")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=19, alignment=TA_LEFT, spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"], fontSize=11.5,
        textColor=colors.HexColor("#6b7280"), spaceAfter=14,
    )
    h2_style = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontSize=12.5,
        textColor=colors.HexColor("#1a1a2e"), spaceBefore=16, spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "Body", parent=styles["Normal"], fontSize=10, leading=14,
        textColor=colors.HexColor("#374151"),
    )
    na_style = ParagraphStyle(
        "NA", parent=body_style, textColor=colors.HexColor("#9ca3af"), fontName="Helvetica-Oblique",
    )
    meta_style = ParagraphStyle(
        "Meta", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#9ca3af"),
    )
    err_style = ParagraphStyle(
        "Err", parent=body_style, textColor=colors.HexColor("#991b1b"),
        backColor=colors.HexColor("#fee2e2"), borderPadding=8, borderRadius=4,
    )
    warn_style = ParagraphStyle(
        "Warn", parent=body_style, textColor=colors.HexColor("#78350f"),
        backColor=colors.HexColor("#fef9c3"), borderPadding=6, borderRadius=4, spaceAfter=5,
    )
    note_style = ParagraphStyle(
        "Note", parent=meta_style, spaceBefore=6,
    )

    company = data.get("company_name") or "Unknown Company"
    fq, fy = data.get("fiscal_quarter"), data.get("fiscal_year")
    if fq and fy:
        period = f"{fq} {fy}"
    elif fq or fy:
        period = fq or str(fy)
    else:
        period = "Reporting period unknown"

    doc = SimpleDocTemplate(
        out_path,
        pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title=f"Earnings Review — {company}",
    )
    story = []

    story.append(Paragraph("Quarterly Earnings — Review Report", title_style))
    story.append(Paragraph(f"{_esc(company)} &nbsp;&bull;&nbsp; {_esc(period)}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e5e7eb"), spaceAfter=14))

    # ---- Source / extraction metadata ----
    conf = data.get("confidence_score")
    conf_text = f"{conf:.0%}" if isinstance(conf, (int, float)) else "—"
    meta_rows = [
        ["Source file",           data.get("filename") or "—"],
        ["Pages",                 str(data.get("pages")) if data.get("pages") is not None else "—"],
        ["Quarter end date",      data.get("quarter_end_date") or "—"],
        ["Reporting currency",    data.get("currency") or "—"],
        ["Unit as reported",      data.get("unit_raw") or "—"],
        ["Extraction confidence", conf_text],
        ["Uploaded",              data.get("uploaded_at") or "—"],
    ]
    if data.get("attempt_count"):
        meta_rows.insert(-1, ["Extraction attempt", str(data["attempt_count"])])
    meta_table = Table(meta_rows, colWidths=[1.8 * inch, 4.2 * inch])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#6b7280")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1a1a2e")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#f1f5f9")),
    ]))
    story.append(meta_table)

    def add_footer():
        story.append(Spacer(1, 22))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e5e7eb"), spaceAfter=6))
        generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        story.append(Paragraph(
            f"Auto-generated from a GPT-5.4-mini extraction of entry #{data.get('id', '—')}. "
            f"Report generated {generated}. This report is provided for human review — "
            f"verify all figures against the source PDF before relying on them.",
            meta_style,
        ))

    # ---- Analysis error: short-circuit with an error banner ----
    if data.get("analysis_error"):
        story.append(Paragraph("Analysis Error", h2_style))
        story.append(Paragraph(_esc(data["analysis_error"]), err_style))
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "No structured financial data could be extracted from this document. "
            "Please review the source PDF manually.", body_style,
        ))
        add_footer()
        doc.build(story)
        return

    # ---- Financial summary ----
    story.append(Paragraph("Financial Summary", h2_style))
    currency = data.get("currency") or ""
    metric_suffix = f" ({currency}M)" if currency else " (M)"
    fin_header = ["Metric", "Current Qtr", "Prev Qtr", "Same Qtr LY", "QoQ %", "YoY %"]
    fin_rows = [
        fin_header,
        [
            "Revenue" + metric_suffix,
            _fmt_money(data.get("revenue_current")),
            _fmt_money(data.get("revenue_previous_quarter")),
            _fmt_money(data.get("revenue_same_quarter_last_year")),
            _fmt_pct(data.get("revenue_qoq")),
            _fmt_pct(data.get("revenue_yoy")),
        ],
        [
            "PBT" + metric_suffix,
            _fmt_money(data.get("pbt_current")),
            _fmt_money(data.get("pbt_previous_quarter")),
            _fmt_money(data.get("pbt_same_quarter_last_year")),
            _fmt_pct(data.get("pbt_qoq")),
            _fmt_pct(data.get("pbt_yoy")),
        ],
    ]
    fin_table = Table(
        fin_rows,
        colWidths=[1.6 * inch, 0.95 * inch, 0.95 * inch, 1.05 * inch, 0.7 * inch, 0.7 * inch],
    )
    table_style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#64748b")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for r_idx in (1, 2):
        for c_idx in (4, 5):
            cell_val = fin_rows[r_idx][c_idx]
            if cell_val != "—":
                is_pos = cell_val.startswith("+")
                table_style_cmds.append((
                    "TEXTCOLOR", (c_idx, r_idx), (c_idx, r_idx),
                    colors.HexColor("#166534") if is_pos else colors.HexColor("#991b1b"),
                ))
    fin_table.setStyle(TableStyle(table_style_cmds))
    story.append(fin_table)
    story.append(Paragraph(
        "All monetary values normalised to millions of the reporting currency.", note_style,
    ))

    # ---- Commentary & outlook ----
    story.append(Paragraph("Management Commentary", h2_style))
    commentary = data.get("management_commentary")
    story.append(Paragraph(_esc(commentary), body_style) if commentary else Paragraph("Not available", na_style))

    story.append(Paragraph("Outlook", h2_style))
    outlook = data.get("outlook_summary")
    story.append(Paragraph(_esc(outlook), body_style) if outlook else Paragraph("Not available", na_style))

    # ---- Validation warnings ----
    warnings = data.get("validation_warnings") or []
    if warnings:
        story.append(Paragraph(f"Validation Warnings ({len(warnings)})", h2_style))
        for w in warnings:
            story.append(Paragraph(f"⚠ {_esc(w)}", warn_style))

    add_footer()
    doc.build(story)


# ---------- Pydantic validation ----------

try:
    from pydantic import BaseModel, Field, field_validator, ValidationError
    from typing import Optional

    class EarningsReport(BaseModel):
        model_config = {"extra": "ignore"}   # silently drop unexpected keys from LLM

        company_name:                   Optional[str]   = None
        quarter_end_date:               Optional[str]   = None
        fiscal_quarter:                 Optional[str]   = None
        fiscal_year:                    Optional[int]   = None
        currency:                       Optional[str]   = None
        unit_raw:                       Optional[str]   = None
        revenue_current:                Optional[float] = None
        revenue_previous_quarter:       Optional[float] = None
        revenue_same_quarter_last_year: Optional[float] = None
        pbt_current:                    Optional[float] = None
        pbt_previous_quarter:           Optional[float] = None
        pbt_same_quarter_last_year:     Optional[float] = None
        management_commentary:          Optional[str]   = None
        outlook_summary:                Optional[str]   = None
        confidence_score:               Optional[float] = None

    _pydantic_available = True

except ImportError:
    _pydantic_available = False
    ValidationError = Exception


VALID_QUARTERS = {"Q1", "Q2", "Q3", "Q4"}
VALID_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_analysis(raw: dict) -> list[str]:
    """Run Pydantic type checks and cross-field consistency checks on the
    LLM output dict.  Returns a (possibly empty) list of warning strings.
    The LLM dict is NOT mutated; warnings are purely informational."""

    warnings: list[str] = []

    # ---- 1. Pydantic type / coercion check ----
    if _pydantic_available:
        try:
            EarningsReport(**{k: v for k, v in raw.items() if k != "analysis_error"})
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(x) for x in err["loc"])
                warnings.append(f"Type error — {field}: {err['msg']}")
    else:
        warnings.append("pydantic not installed — type validation skipped")

    r = raw  # shorthand for cross-field checks below

    # ---- 2. fiscal_quarter allowed values ----
    fq = r.get("fiscal_quarter")
    if fq is not None and fq not in VALID_QUARTERS:
        warnings.append(f"fiscal_quarter '{fq}' is not one of Q1/Q2/Q3/Q4")

    # ---- 3. fiscal_year plausibility ----
    fy = r.get("fiscal_year")
    current_year = datetime.utcnow().year
    if fy is not None:
        try:
            fy = int(fy)
            if not (2000 <= fy <= current_year + 1):
                warnings.append(
                    f"fiscal_year {fy} is outside the expected range "
                    f"(2000–{current_year + 1})"
                )
        except (ValueError, TypeError):
            warnings.append(f"fiscal_year '{fy}' cannot be parsed as an integer")

    # ---- 4. quarter_end_date format and year consistency ----
    qed = r.get("quarter_end_date")
    if qed is not None:
        if not DATE_RE.match(str(qed)):
            warnings.append(
                f"quarter_end_date '{qed}' is not in YYYY-MM-DD format"
            )
        elif fy is not None:
            date_year = int(str(qed)[:4])
            if abs(date_year - int(fy)) > 1:
                warnings.append(
                    f"quarter_end_date year ({date_year}) does not match "
                    f"fiscal_year ({fy}) — possible extraction error"
                )

    # ---- 5. currency format (ISO 4217: 3 uppercase letters) ----
    currency = r.get("currency")
    if currency is not None and not VALID_CURRENCY_RE.match(str(currency)):
        warnings.append(
            f"currency '{currency}' is not a valid ISO 4217 code "
            f"(expected 3 uppercase letters, e.g. USD)"
        )

    # ---- 6. Revenue must be non-negative ----
    for field in (
        "revenue_current",
        "revenue_previous_quarter",
        "revenue_same_quarter_last_year",
    ):
        val = r.get(field)
        if val is not None and val < 0:
            warnings.append(
                f"{field} is negative ({val:.2f}M) — revenue cannot be negative"
            )

    # ---- 7. PBT must not exceed revenue (when revenue is positive) ----
    pairs = [
        ("revenue_current",                "pbt_current"),
        ("revenue_previous_quarter",       "pbt_previous_quarter"),
        ("revenue_same_quarter_last_year", "pbt_same_quarter_last_year"),
    ]
    for rev_field, pbt_field in pairs:
        rev = r.get(rev_field)
        pbt = r.get(pbt_field)
        if rev is not None and pbt is not None and rev > 0 and pbt > rev:
            warnings.append(
                f"{pbt_field} ({pbt:.2f}M) exceeds {rev_field} ({rev:.2f}M) "
                f"— profit before tax cannot exceed revenue"
            )

    # ---- 8. confidence_score bounds ----
    score = r.get("confidence_score")
    if score is not None:
        try:
            score = float(score)
            if not (0.0 <= score <= 1.0):
                warnings.append(
                    f"confidence_score {score} is outside the valid range [0, 1]"
                )
            elif score < 0.7:
                warnings.append(
                    f"Low confidence score ({score:.0%}) — results should be "
                    f"reviewed manually"
                )
        except (ValueError, TypeError):
            warnings.append(f"confidence_score '{score}' cannot be parsed as a number")

    # ---- 9. No financial values extracted at all ----
    financial_fields = [
        "revenue_current", "revenue_previous_quarter", "revenue_same_quarter_last_year",
        "pbt_current", "pbt_previous_quarter", "pbt_same_quarter_last_year",
    ]
    if all(r.get(f) is None for f in financial_fields):
        warnings.append(
            "No financial values were extracted — the PDF may not contain "
            "machine-readable financial tables"
        )

    return warnings


# ---------- OpenAI ----------

try:
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

EARNINGS_SYSTEM_PROMPT = """You are a financial analyst assistant specialised in parsing quarterly earnings reports.

Extract structured data from the provided report text and return ONLY a valid JSON object with these exact fields:

{
  "company_name":                    "<full legal company name>",
  "quarter_end_date":                "<YYYY-MM-DD or null>",
  "fiscal_quarter":                  "<Q1 | Q2 | Q3 | Q4>",
  "fiscal_year":                     <integer>,
  "currency":                        "<ISO 4217 code e.g. USD, GBP, EUR>",
  "unit_raw":                        "<the unit label as it appears in the report, e.g. '$000s', '£m', 'millions', 'billions'>",
  "revenue_current":                 <revenue this quarter, normalised to millions, or null>,
  "revenue_previous_quarter":        <revenue in the immediately preceding quarter, normalised to millions, or null>,
  "revenue_same_quarter_last_year":  <revenue in the same quarter of the prior fiscal year, normalised to millions, or null>,
  "pbt_current":                     <profit before tax this quarter, normalised to millions, or null>,
  "pbt_previous_quarter":            <profit before tax in the immediately preceding quarter, normalised to millions, or null>,
  "pbt_same_quarter_last_year":      <profit before tax in the same quarter of the prior fiscal year, normalised to millions, or null>,
  "management_commentary":           "<2-3 sentence summary of key management remarks>",
  "outlook_summary":                 "<2-3 sentence summary of forward guidance or outlook>",
  "confidence_score":                <float 0.0-1.0: your overall confidence in the accuracy of the extracted fields>
}

Rules:
- ALL monetary values must be normalised to millions of the reported currency, regardless of how they appear in the document (e.g. if the report states values in thousands, multiply by 0.001; if in billions, multiply by 1000).
- Record the original unit label from the document in unit_raw so the normalisation can be verified.
- For revenue, extract the broadest consolidated/group revenue figure for the quarter. If a table contains component rows and a "Total revenue" row, use "Total revenue".
- Do NOT use component rows such as "Revenue as reported above - Continuing operations", segment revenue, operating revenue, or revenue excluding joint ventures when a broader group/consolidated total revenue row is present.
- If a revenue note says revenue includes share of joint venture companies' revenue, use the row that includes that share, normally "Total revenue".
- Prefer group/consolidated totals over company-only, segment-only, continuing-operations-only, or subtotal rows unless the report clearly states the subtotal is the primary reported revenue metric.
- When a table has both "Individual Quarter" and "Cumulative Period" sections, use "Individual Quarter" for current-quarter and same-quarter-last-year fields. Do not use cumulative period values for quarterly fields.
- For PBT, use the group/consolidated profit before tax / profit before taxation figure for the quarter. Do not use profit after tax, EBITDA, operating profit, segment profit, or cumulative period profit for PBT.
- previous_quarter fields mean the immediately preceding fiscal quarter only (e.g. Q2 uses Q1 of the same fiscal year). Do NOT copy prior-year "Individual Quarter" or same-quarter-last-year comparative columns into previous_quarter fields. If the report does not explicitly show the immediately preceding quarter, use null.
- If a field cannot be determined from the text, use null. Do NOT fabricate or estimate values.
- Return ONLY the JSON object — no markdown, no extra text."""


def analyse_earnings(pdf_text: str, extra_instructions: str | None = None) -> dict:
    """Call GPT-5.4-mini to extract structured earnings data from PDF text.

    `extra_instructions`, when provided, is reviewer-supplied guidance from a
    failed evaluation re-run (e.g. "the currency is EUR not USD") that gets
    woven into the prompt so the model corrects course on the retry.

    Returns a dict with the 10 structured fields, or a dict containing
    'analysis_error' if the call could not be completed.
    """
    if not _openai_available:
        return {"analysis_error": "openai package not installed — run: pip install openai"}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"analysis_error": "OPENAI_API_KEY environment variable is not set"}

    try:
        client = _OpenAI(api_key=api_key)

        # Truncate to ~100 k chars (~25 k tokens) to stay within context limits
        truncated_text = pdf_text[:100_000]

        user_content = (
            "Parse this quarterly earnings report and return the structured JSON:\n\n"
            + truncated_text
        )
        if extra_instructions:
            user_content = (
                "IMPORTANT — a human reviewer rejected a previous extraction attempt "
                "for this same document and left the following notes. Follow them "
                "carefully when re-parsing the report below:\n"
                f"{extra_instructions}\n\n" + user_content
            )

        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": EARNINGS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )

        result = json.loads(response.choices[0].message.content)
        return result

    except Exception as exc:
        error_msg = str(exc)
        print(f"\n[ERROR] GPT-5.4-mini analysis failed: {error_msg}\n")
        return {"analysis_error": error_msg}


# ---------- Growth calculations ----------

# Maps each fiscal quarter to its predecessor (prev_quarter, year_delta)
_PREV_QUARTER: dict[str, tuple[str, int]] = {
    "Q1": ("Q4", -1),
    "Q2": ("Q1",  0),
    "Q3": ("Q2",  0),
    "Q4": ("Q3",  0),
}


def pct_change(current, prior) -> float | None:
    """((current - prior) / |prior|) × 100, rounded to 2 dp.
    Returns None if either value is missing or prior is zero."""
    if current is None or prior is None or prior == 0:
        return None
    return round(((current - prior) / abs(prior)) * 100, 2)


def compute_qoq_yoy(analysis: dict, db, *, log_matches: bool = True) -> dict:
    """Calculate comparison values and revenue/PBT QoQ/YoY growth rates.

    Previous-quarter values are DB-only. Quarterly reports often show
    "Individual Quarter" comparatives for the same quarter last year; those
    must never be treated as the prior quarter. Same-quarter-last-year values
    may fall back to the current report when no approved DB row exists.
    """
    result = {
        "revenue_previous_quarter":       None,
        "pbt_previous_quarter":           None,
        "revenue_same_quarter_last_year": None,
        "pbt_same_quarter_last_year":     None,
        "revenue_qoq":                    None,
        "revenue_yoy":                    None,
        "pbt_qoq":                        None,
        "pbt_yoy":                        None,
    }

    if analysis.get("analysis_error"):
        return result

    company     = analysis.get("company_name")
    fq          = analysis.get("fiscal_quarter")
    fy_raw      = analysis.get("fiscal_year")
    rev_current = analysis.get("revenue_current")
    pbt_current = analysis.get("pbt_current")

    try:
        fy = int(fy_raw) if fy_raw is not None else None
    except (TypeError, ValueError):
        fy = None

    # ---- Previous quarter ----
    rev_prev = pbt_prev = None

    if company and fq and fy and fq in _PREV_QUARTER:
        pq, year_delta = _PREV_QUARTER[fq]
        py = fy + year_delta
        row = db.execute(
            """SELECT revenue_current, pbt_current FROM pdf_metadata
               WHERE LOWER(company_name) = LOWER(?)
                 AND fiscal_quarter = ? AND fiscal_year = ?
                 AND analysis_error IS NULL
               ORDER BY uploaded_at DESC LIMIT 1""",
            (company, pq, py),
        ).fetchone()
        if row:
            rev_prev = row["revenue_current"]
            pbt_prev = row["pbt_current"]
            if log_matches:
                print(f"[QoQ] DB match: {company} {pq} {py}")

    result["revenue_previous_quarter"] = rev_prev
    result["pbt_previous_quarter"] = pbt_prev
    result["revenue_qoq"] = pct_change(rev_current, rev_prev)
    result["pbt_qoq"]     = pct_change(pbt_current, pbt_prev)

    # ---- Same quarter last year ----
    rev_ly = pbt_ly = None

    if company and fq and fy:
        row = db.execute(
            """SELECT revenue_current, pbt_current FROM pdf_metadata
               WHERE LOWER(company_name) = LOWER(?)
                 AND fiscal_quarter = ? AND fiscal_year = ?
                 AND analysis_error IS NULL
               ORDER BY uploaded_at DESC LIMIT 1""",
            (company, fq, fy - 1),
        ).fetchone()
        if row:
            rev_ly = row["revenue_current"]
            pbt_ly = row["pbt_current"]
            if log_matches:
                print(f"[YoY] DB match: {company} {fq} {fy - 1}")

    # Fallback to LLM-extracted comparative figures
    if rev_ly is None:
        rev_ly = analysis.get("revenue_same_quarter_last_year")
    if pbt_ly is None:
        pbt_ly = analysis.get("pbt_same_quarter_last_year")

    result["revenue_same_quarter_last_year"] = rev_ly
    result["pbt_same_quarter_last_year"] = pbt_ly
    result["revenue_yoy"] = pct_change(rev_current, rev_ly)
    result["pbt_yoy"]     = pct_change(pbt_current, pbt_ly)

    return result


def _apply_comparison_data(data: dict, db) -> dict:
    """Return data with DB-derived comparison values and growth rates applied."""
    refreshed = dict(data)
    comparisons = compute_qoq_yoy(refreshed, db, log_matches=False)
    for field, value in comparisons.items():
        refreshed[field] = value
    return refreshed


COMPARISON_FIELDS = [
    "revenue_previous_quarter",
    "pbt_previous_quarter",
    "revenue_same_quarter_last_year",
    "pbt_same_quarter_last_year",
    "revenue_qoq",
    "revenue_yoy",
    "pbt_qoq",
    "pbt_yoy",
]


def _refresh_approved_comparisons(db) -> int:
    """Backfill comparison values for approved reports after new approvals.

    This handles out-of-order uploads: if Q3 is approved before Q2, approving
    Q2 later can fill Q3's previously missing QoQ values.
    """
    rows = db.execute("SELECT * FROM pdf_metadata ORDER BY id").fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        comparisons = compute_qoq_yoy(data, db, log_matches=False)
        if not any(data.get(field) != comparisons.get(field) for field in COMPARISON_FIELDS):
            continue
        db.execute(
            """UPDATE pdf_metadata
               SET revenue_previous_quarter = ?,
                   pbt_previous_quarter = ?,
                   revenue_same_quarter_last_year = ?,
                   pbt_same_quarter_last_year = ?,
                   revenue_qoq = ?,
                   revenue_yoy = ?,
                   pbt_qoq = ?,
                   pbt_yoy = ?
               WHERE id = ?""",
            (
                comparisons["revenue_previous_quarter"],
                comparisons["pbt_previous_quarter"],
                comparisons["revenue_same_quarter_last_year"],
                comparisons["pbt_same_quarter_last_year"],
                comparisons["revenue_qoq"],
                comparisons["revenue_yoy"],
                comparisons["pbt_qoq"],
                comparisons["pbt_yoy"],
                data["id"],
            ),
        )
        updated += 1
    if updated:
        db.commit()
    return updated


# ---------- Extraction pipeline (review-gated) ----------

# The exact set of fields the LLM is asked to extract. These are staged in
# pending_reviews and are NOT written to pdf_metadata until a human approves
# the generated report.
EXTRACTED_FIELDS = [
    "company_name", "quarter_end_date", "fiscal_quarter", "fiscal_year",
    "currency", "unit_raw",
    "revenue_current", "revenue_previous_quarter", "revenue_same_quarter_last_year",
    "pbt_current", "pbt_previous_quarter", "pbt_same_quarter_last_year",
    "management_commentary", "outlook_summary", "confidence_score",
]


def _package_extracted_data(analysis: dict, analysis_error, warnings: list, growth: dict) -> dict:
    """Assemble the canonical structured-output dict — same shape that used
    to be written straight to the DB — but now this is only ever persisted
    inside pending_reviews.extracted_data until a reviewer approves it."""
    data = {field: analysis.get(field) for field in EXTRACTED_FIELDS}
    data["revenue_previous_quarter"]       = growth["revenue_previous_quarter"]
    data["pbt_previous_quarter"]           = growth["pbt_previous_quarter"]
    data["revenue_same_quarter_last_year"] = growth["revenue_same_quarter_last_year"]
    data["pbt_same_quarter_last_year"]     = growth["pbt_same_quarter_last_year"]
    data["analysis_error"]      = analysis_error
    data["validation_warnings"] = warnings if warnings else None
    data["revenue_qoq"]         = growth["revenue_qoq"]
    data["revenue_yoy"]         = growth["revenue_yoy"]
    data["pbt_qoq"]             = growth["pbt_qoq"]
    data["pbt_yoy"]             = growth["pbt_yoy"]
    return data


def _run_llm_pipeline(pdf_text: str, db, extra_instructions: str | None = None) -> dict:
    """Run the LLM extraction + validation + growth calculation and return
    the packaged extracted-data dict. Used for both the initial upload and
    any reviewer-triggered rerun."""
    analysis = analyse_earnings(pdf_text, extra_instructions=extra_instructions)
    analysis_error = analysis.get("analysis_error")
    growth = compute_qoq_yoy(analysis, db)
    data = _package_extracted_data(analysis, analysis_error, [], growth)
    if not analysis_error:
        warnings = validate_analysis(data)
        data["validation_warnings"] = warnings if warnings else None
    return data


# ---------- Flask app ----------

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
REPORTS_FOLDER = os.path.join(BASE_DIR, "reports")
TEST_DATA_FOLDER = os.path.join(BASE_DIR, "test_data")
EVAL_RESULTS_FOLDER = os.path.join(BASE_DIR, "eval_results")
DB_PATH = os.path.join(BASE_DIR, "pdfs.db")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORTS_FOLDER, exist_ok=True)
os.makedirs(EVAL_RESULTS_FOLDER, exist_ok=True)


# ---------- Evaluation harness ----------
#
# Runs the 5 synthetic PDFs in test_data/ straight through the extraction +
# validation + QoQ/YoY pipeline (bypassing the review-gated DB tables so
# re-running the suite never collides with the duplicate-upload check) and
# diffs the output against test_data/expected_results.json.

# A couple of the fixtures are deliberately ambiguous (see test_data/MANIFEST.md,
# adversarial case 5) — either value is a defensible extraction.
EVAL_ACCEPTABLE_ALTERNATIVES = {
    "05_adversarial_TransPacific_Global_Q2_FY2026.pdf": {
        "currency": ["USD", "SGD"],
        "fiscal_quarter": ["Q1", "Q2"],
    },
}

EVAL_NUMERIC_FIELDS = {
    "fiscal_year",
    "revenue_current", "revenue_previous_quarter", "revenue_same_quarter_last_year",
    "pbt_current", "pbt_previous_quarter", "pbt_same_quarter_last_year",
}

_EVAL_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_EVAL_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _eval_num_close(actual, expected, rel=0.02, abs_tol=0.05) -> bool:
    if actual is None or expected is None:
        return actual == expected
    try:
        actual, expected = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return abs(actual - expected) <= max(abs_tol, abs(expected) * rel)


def _eval_normalize_warning(w: str) -> str:
    """Strip out the specific numbers from a warning so cases whose figures
    are within tolerance (but not byte-identical) still match on wording."""
    return _EVAL_NUM_RE.sub("#", w).lower().strip()


def _eval_normalize_text(s: str) -> str:
    """Case/whitespace-fold a text field and drop parenthetical asides, e.g.
    "Lindqvist Industrial AB (publ)" / "SEK million (MSEK)" — a legal suffix
    or unit clarification the LLM is free to include or omit. Does NOT strip
    other punctuation/symbols, since a wrong currency symbol or code in a
    field like unit_raw is a real discrepancy, not noise."""
    return " ".join(_EVAL_PAREN_RE.sub("", s).lower().split())


def _eval_compare_field(field: str, actual, expected, filename: str):
    alternatives = EVAL_ACCEPTABLE_ALTERNATIVES.get(filename, {}).get(field)
    if alternatives:
        return actual in alternatives, actual, f"one of {alternatives}"
    if field in EVAL_NUMERIC_FIELDS:
        return _eval_num_close(actual, expected), actual, expected
    if actual is None and expected is None:
        return True, actual, expected
    if not (isinstance(actual, str) and isinstance(expected, str)):
        return actual == expected, actual, expected
    ok = _eval_normalize_text(actual) == _eval_normalize_text(expected)
    return ok, actual, expected


def _evaluate_case(filename: str, expected: dict) -> dict:
    """Run one test PDF through the pipeline and diff it against its expected
    result block. Returns a dict with a `passed` flag and a `checks` list."""
    case = {"filename": filename, "category": expected.get("category"), "checks": [], "passed": True}

    path = os.path.join(TEST_DATA_FOLDER, filename)
    if not os.path.exists(path):
        case["passed"] = False
        case["error"] = f"Test file not found: {filename}"
        return case

    def record(field, ok, actual, expected_val, info_only=False):
        case["checks"].append({"field": field, "actual": actual, "expected": expected_val, "passed": ok, "info_only": info_only})
        if not info_only and not ok:
            case["passed"] = False

    # ---- PDF metadata (independent of the LLM) ----
    meta = get_pdf_metadata(path)
    exp_meta = expected.get("metadata", {})
    for key in ("title", "author", "creator"):
        record(f"metadata.{key}", (meta.get(key) or "") == (exp_meta.get(key) or ""), meta.get(key), exp_meta.get(key))
    record("pages", meta.get("pages") == expected.get("pages"), meta.get("pages"), expected.get("pages"))

    # ---- LLM extraction ----
    pdf_text = extract_pdf_text(path)
    analysis = analyse_earnings(pdf_text)
    if analysis.get("analysis_error"):
        case["passed"] = False
        case["error"] = f"LLM analysis failed: {analysis['analysis_error']}"
        return case

    for field, expected_val in expected.get("expected_extraction", {}).items():
        if field == "confidence_score":
            # LLM self-assessed and varies run to run — report only, don't fail on it.
            record(field, True, analysis.get(field), expected_val, info_only=True)
            continue
        ok, act, exp = _eval_compare_field(field, analysis.get(field), expected_val, filename)
        record(field, ok, act, exp)

    # ---- Validation warnings ----
    warnings = validate_analysis(analysis)
    expected_warnings = expected.get("expected_validation_warnings", [])
    norm_actual = {_eval_normalize_warning(w) for w in warnings}
    norm_expected = {_eval_normalize_warning(w) for w in expected_warnings}
    record("validation_warnings", norm_actual == norm_expected, warnings, expected_warnings)

    # ---- QoQ / YoY, computed directly from the report's own comparative
    # figures (matching how test_data/expected_results.json was generated —
    # see MANIFEST.md's "empty-DB fallback path" note) ----
    qoq_yoy = {
        "revenue_qoq": pct_change(analysis.get("revenue_current"), analysis.get("revenue_previous_quarter")),
        "revenue_yoy": pct_change(analysis.get("revenue_current"), analysis.get("revenue_same_quarter_last_year")),
        "pbt_qoq":     pct_change(analysis.get("pbt_current"), analysis.get("pbt_previous_quarter")),
        "pbt_yoy":     pct_change(analysis.get("pbt_current"), analysis.get("pbt_same_quarter_last_year")),
    }
    for field, expected_val in expected.get("expected_qoq_yoy", {}).items():
        actual_val = qoq_yoy.get(field)
        ok = _eval_num_close(actual_val, expected_val, rel=0.05, abs_tol=1.0)
        record(field, ok, actual_val, expected_val)

    return case


def run_evaluation() -> dict:
    """Run every test case in test_data/expected_results.json through the
    pipeline, write the full results to eval_results/, and return them."""
    expected_path = os.path.join(TEST_DATA_FOLDER, "expected_results.json")
    if not os.path.exists(expected_path):
        raise FileNotFoundError(f"expected_results.json not found in {TEST_DATA_FOLDER}")

    with open(expected_path, "r", encoding="utf-8") as f:
        expected_all = json.load(f)

    cases = []
    for filename, expected in expected_all.items():
        print(f"[EVAL] Running test case: {filename}")
        cases.append(_evaluate_case(filename, expected))

    now = datetime.utcnow()
    passed = sum(1 for c in cases if c["passed"])
    result = {
        "run_at": now.isoformat(timespec="seconds") + "Z",
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": cases,
    }

    report_filename = f"evaluation_{now.strftime('%Y%m%d_%H%M%S')}.json"
    with open(os.path.join(EVAL_RESULTS_FOLDER, report_filename), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    result["report_filename"] = report_filename

    return result


def _row_to_report_data(row: sqlite3.Row, db=None) -> dict:
    """Convert a pdf_metadata DB row into the plain dict shape expected by
    generate_report_pdf (validation_warnings deserialised into a list)."""
    d = dict(row)
    raw_warnings = d.get("validation_warnings")
    d["validation_warnings"] = json.loads(raw_warnings) if raw_warnings else None
    if db is not None:
        d = _apply_comparison_data(d, db)
    return d


def _build_report_for_row(db, row: sqlite3.Row) -> str:
    """Generate (or re-generate) the PDF review report for an APPROVED
    pdf_metadata row and persist its path on the record. Returns the
    absolute file path."""
    data = _row_to_report_data(row, db)
    report_path = os.path.join(REPORTS_FOLDER, f"report_{row['id']}.pdf")
    generate_report_pdf(data, report_path)
    db.execute("UPDATE pdf_metadata SET report_path = ? WHERE id = ?", (report_path, row["id"]))
    db.commit()
    return report_path


def _build_pending_report(db, row: sqlite3.Row) -> str:
    """Generate (or re-generate) the draft PDF review report for a
    pending_reviews row and persist its path on the record."""
    extracted_data = json.loads(row["extracted_data"])
    payload = dict(extracted_data)
    payload["id"]          = row["id"]
    payload["filename"]    = row["filename"]
    payload["pages"]       = row["pages"]
    payload["uploaded_at"] = row["uploaded_at"]
    payload["attempt_count"] = row["attempt_count"]
    payload = _apply_comparison_data(payload, db)
    report_path = os.path.join(REPORTS_FOLDER, f"pending_{row['id']}.pdf")
    generate_report_pdf(payload, report_path)
    db.execute("UPDATE pending_reviews SET report_path = ? WHERE id = ?", (report_path, row["id"]))
    db.commit()
    return report_path

ALLOWED_EXTENSIONS = {"pdf"}

# Analysis columns added to pdf_metadata; listed here for migration
ANALYSIS_COLUMNS = [
    ("company_name",                   "TEXT"),
    ("quarter_end_date",               "TEXT"),
    ("fiscal_quarter",                 "TEXT"),
    ("fiscal_year",                    "INTEGER"),
    ("currency",                       "TEXT"),
    ("unit_raw",                       "TEXT"),
    ("revenue_current",                "REAL"),
    ("revenue_previous_quarter",       "REAL"),
    ("revenue_same_quarter_last_year", "REAL"),
    ("pbt_current",                    "REAL"),
    ("pbt_previous_quarter",           "REAL"),
    ("pbt_same_quarter_last_year",     "REAL"),
    ("management_commentary",          "TEXT"),
    ("outlook_summary",                "TEXT"),
    ("confidence_score",               "REAL"),
    ("analysis_error",                 "TEXT"),
    ("validation_warnings",            "TEXT"),
    ("revenue_qoq",                    "REAL"),
    ("revenue_yoy",                    "REAL"),
    ("pbt_qoq",                        "REAL"),
    ("pbt_yoy",                        "REAL"),
    ("report_path",                    "TEXT"),
]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------- Database ----------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE IF NOT EXISTS pdf_metadata (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                filename              TEXT NOT NULL,
                file_size             INTEGER NOT NULL,
                sha256                TEXT NOT NULL DEFAULT '',
                pages                 INTEGER,
                title                 TEXT,
                author                TEXT,
                creator               TEXT,
                uploaded_at           TEXT NOT NULL,
                company_name          TEXT,
                quarter_end_date      TEXT,
                fiscal_quarter        TEXT,
                fiscal_year           INTEGER,
                currency                       TEXT,
                unit_raw                       TEXT,
                revenue_current                REAL,
                revenue_previous_quarter       REAL,
                revenue_same_quarter_last_year REAL,
                pbt_current                    REAL,
                pbt_previous_quarter           REAL,
                pbt_same_quarter_last_year     REAL,
                management_commentary          TEXT,
                outlook_summary                TEXT,
                confidence_score               REAL,
                analysis_error                 TEXT,
                validation_warnings            TEXT,
                revenue_qoq                    REAL,
                revenue_yoy                    REAL,
                pbt_qoq                        REAL,
                pbt_yoy                        REAL,
                report_path                    TEXT
            )
        """)
        db.commit()

        # Extracted figures/text live here ONLY until a human reviewer
        # approves the generated PDF report — nothing here is "the database"
        # of record; approval is what copies a row into pdf_metadata.
        db.execute("""
            CREATE TABLE IF NOT EXISTS pending_reviews (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                filename            TEXT NOT NULL,
                file_size           INTEGER NOT NULL,
                sha256              TEXT NOT NULL,
                pages               INTEGER,
                title               TEXT,
                author              TEXT,
                creator             TEXT,
                uploaded_at         TEXT NOT NULL,
                extracted_data      TEXT NOT NULL,
                report_path         TEXT,
                attempt_count       INTEGER NOT NULL DEFAULT 1,
                extra_instructions  TEXT NOT NULL DEFAULT '[]',
                downloaded_at       TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
        """)
        db.commit()

        # Migrate existing databases that are missing newer columns
        existing_cols = {row[1] for row in db.execute("PRAGMA table_info(pdf_metadata)")}
        for col_name, col_type in [("sha256", "TEXT NOT NULL DEFAULT ''")] + ANALYSIS_COLUMNS:
            if col_name not in existing_cols:
                try:
                    db.execute(f"ALTER TABLE pdf_metadata ADD COLUMN {col_name} {col_type}")
                    db.commit()
                    print(f"[DB] Added column: {col_name}")
                except sqlite3.OperationalError as e:
                    print(f"[DB] Could not add column {col_name}: {e}")

        refreshed = _refresh_approved_comparisons(db)
        if refreshed:
            print(f"[DB] Refreshed comparison fields for {refreshed} approved report(s)")

        db.close()


# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Save the PDF, run the LLM extraction, and stage the result in
    pending_reviews. Nothing is written to pdf_metadata at this point —
    that only happens once a human approves the generated report via
    POST /pending/<id>/approve."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    # Read into memory so we can hash before touching disk
    file_bytes = file.read()
    file_size = len(file_bytes)
    sha256 = hashlib.sha256(file_bytes).hexdigest()

    db = get_db()

    # Duplicate check — against approved entries...
    existing = db.execute(
        "SELECT id, filename FROM pdf_metadata WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if existing:
        return jsonify({
            "error": "duplicate",
            "message": (
                f'This file has already been uploaded and approved as "{existing["filename"]}"'
                f" (entry #{existing['id']})."
            ),
            "existing_id": existing["id"],
        }), 409

    # ...and against reviews still awaiting a decision
    pending_existing = db.execute(
        "SELECT id, filename FROM pending_reviews WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if pending_existing:
        return jsonify({
            "error": "duplicate",
            "message": (
                f'This file is already awaiting review as "{pending_existing["filename"]}"'
                f" (pending #{pending_existing['id']})."
            ),
            "existing_pending_id": pending_existing["id"],
        }), 409

    # Save file
    safe_name = os.path.basename(file.filename)
    save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    if os.path.exists(save_path):
        base, ext = os.path.splitext(safe_name)
        safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
        save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    # Basic PDF metadata
    meta = get_pdf_metadata(save_path)
    uploaded_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # GPT-5.4-mini earnings analysis
    print(f"\n[INFO] Starting GPT-5.4-mini analysis for: {safe_name}")
    pdf_text = extract_pdf_text(save_path)
    extracted_data = _run_llm_pipeline(pdf_text, db)

    print("\n" + "=" * 50)
    print("GPT-5.4-mini Earnings Analysis — PENDING REVIEW (not yet saved to the database)")
    print("=" * 50)
    print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
    print("=" * 50 + "\n")

    cur = db.execute(
        """INSERT INTO pending_reviews (
               filename, file_size, sha256, pages, title, author, creator, uploaded_at,
               extracted_data, attempt_count, extra_instructions, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            safe_name, file_size, sha256, meta["pages"],
            meta["title"], meta["author"], meta["creator"], uploaded_at,
            json.dumps(extracted_data), 1, "[]", uploaded_at, uploaded_at,
        ),
    )
    db.commit()
    pending_id = cur.lastrowid

    # Generate the draft PDF report the reviewer will download and read.
    report_available = False
    try:
        row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
        _build_pending_report(db, row)
        report_available = True
    except Exception as exc:
        print(f"[WARNING] Could not generate PDF report for pending entry #{pending_id}: {exc}")

    return jsonify({
        "id":                pending_id,
        "status":            "pending_review",
        "filename":          safe_name,
        "file_size":         file_size,
        "pages":             meta["pages"],
        "uploaded_at":       uploaded_at,
        "company_name":      extracted_data.get("company_name"),
        "fiscal_quarter":    extracted_data.get("fiscal_quarter"),
        "fiscal_year":       extracted_data.get("fiscal_year"),
        "confidence_score":  extracted_data.get("confidence_score"),
        "analysis_error":    extracted_data.get("analysis_error"),
        "attempt_count":     1,
        "extra_instructions": [],
        "report_available":  report_available,
        "report_url":        f"/pending/{pending_id}/report",
        "message": (
            "Review report generated. Download and read it, then approve or fail "
            "the evaluation — figures are not saved until approved."
        ),
    }), 201


@app.route("/pending", methods=["GET"])
def list_pending():
    """List uploads awaiting review. Only identification fields are exposed
    here — the reviewer is expected to open the PDF report to see the full
    extracted figures and commentary before deciding."""
    db = get_db()
    rows = db.execute("SELECT * FROM pending_reviews ORDER BY id DESC").fetchall()
    result = []
    for r in rows:
        extracted_data = json.loads(r["extracted_data"])
        result.append({
            "id":                 r["id"],
            "filename":           r["filename"],
            "pages":              r["pages"],
            "uploaded_at":        r["uploaded_at"],
            "company_name":       extracted_data.get("company_name"),
            "fiscal_quarter":     extracted_data.get("fiscal_quarter"),
            "fiscal_year":        extracted_data.get("fiscal_year"),
            "confidence_score":   extracted_data.get("confidence_score"),
            "analysis_error":     extracted_data.get("analysis_error"),
            "attempt_count":      r["attempt_count"],
            "extra_instructions": json.loads(r["extra_instructions"] or "[]"),
            "downloaded":         bool(r["downloaded_at"]),
            "report_available":   bool(r["report_path"]),
        })
    return jsonify(result)


@app.route("/pending/<int:pending_id>/report", methods=["GET"])
def download_pending_report(pending_id):
    db = get_db()
    row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    try:
        report_path = _build_pending_report(db, row)
    except Exception as exc:
        return jsonify({"error": f"Could not generate report: {exc}"}), 500

    # Record that the report has been downloaded — approve/reject require
    # this so a decision can't be made without the human opening the PDF.
    db.execute(
        "UPDATE pending_reviews SET downloaded_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(timespec="seconds") + "Z", pending_id),
    )
    db.commit()

    extracted_data = json.loads(row["extracted_data"])
    company = (extracted_data.get("company_name") or "pending").strip().replace(" ", "_") or "pending"
    period = "".join(filter(None, [extracted_data.get("fiscal_quarter"), str(extracted_data.get("fiscal_year") or "")]))
    attempt = row["attempt_count"]
    suffix = f"pending{pending_id}_attempt{attempt}_DRAFT_review.pdf"
    download_name = f"{company}_{period}_{suffix}" if period else f"{company}_{suffix}"

    response = send_file(report_path, as_attachment=True, download_name=download_name, max_age=0)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/pending/<int:pending_id>/approve", methods=["POST"])
def approve_pending(pending_id):
    """Approve a reviewed evaluation: only now do the extracted figures and
    text get written to pdf_metadata (the database of record)."""
    db = get_db()
    row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if not row["downloaded_at"]:
        return jsonify({
            "error": "not_reviewed",
            "message": "Download and review the report before approving.",
        }), 400

    extracted_data = _apply_comparison_data(json.loads(row["extracted_data"]), db)
    if not extracted_data.get("analysis_error"):
        warnings = validate_analysis(extracted_data)
        extracted_data["validation_warnings"] = warnings if warnings else None
    validation_warnings_json = (
        json.dumps(extracted_data["validation_warnings"])
        if extracted_data.get("validation_warnings") else None
    )

    cur = db.execute(
        """INSERT INTO pdf_metadata (
               filename, file_size, sha256, pages, title, author, creator, uploaded_at,
               company_name, quarter_end_date, fiscal_quarter, fiscal_year, currency,
               unit_raw,
               revenue_current, revenue_previous_quarter, revenue_same_quarter_last_year,
               pbt_current, pbt_previous_quarter, pbt_same_quarter_last_year,
               management_commentary, outlook_summary, confidence_score,
               analysis_error, validation_warnings,
               revenue_qoq, revenue_yoy, pbt_qoq, pbt_yoy
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["filename"], row["file_size"], row["sha256"], row["pages"],
            row["title"], row["author"], row["creator"], row["uploaded_at"],
            extracted_data.get("company_name"),
            extracted_data.get("quarter_end_date"),
            extracted_data.get("fiscal_quarter"),
            extracted_data.get("fiscal_year"),
            extracted_data.get("currency"),
            extracted_data.get("unit_raw"),
            extracted_data.get("revenue_current"),
            extracted_data.get("revenue_previous_quarter"),
            extracted_data.get("revenue_same_quarter_last_year"),
            extracted_data.get("pbt_current"),
            extracted_data.get("pbt_previous_quarter"),
            extracted_data.get("pbt_same_quarter_last_year"),
            extracted_data.get("management_commentary"),
            extracted_data.get("outlook_summary"),
            extracted_data.get("confidence_score"),
            extracted_data.get("analysis_error"),
            validation_warnings_json,
            extracted_data.get("revenue_qoq"),
            extracted_data.get("revenue_yoy"),
            extracted_data.get("pbt_qoq"),
            extracted_data.get("pbt_yoy"),
        ),
    )
    db.commit()
    new_id = cur.lastrowid
    _refresh_approved_comparisons(db)

    report_available = False
    try:
        new_row = db.execute("SELECT * FROM pdf_metadata WHERE id = ?", (new_id,)).fetchone()
        _build_report_for_row(db, new_row)
        report_available = True
    except Exception as exc:
        print(f"[WARNING] Could not generate final PDF report for entry #{new_id}: {exc}")

    # Clean up the draft report and the pending record now it's been promoted
    if row["report_path"] and os.path.exists(row["report_path"]):
        os.remove(row["report_path"])
    db.execute("DELETE FROM pending_reviews WHERE id = ?", (pending_id,))
    db.commit()

    return jsonify({
        "id":                             new_id,
        "status":                         "approved",
        "report_available":               report_available,
        "filename":                       row["filename"],
        "file_size":                      row["file_size"],
        "pages":                          row["pages"],
        "uploaded_at":                    row["uploaded_at"],
        "company_name":                   extracted_data.get("company_name"),
        "quarter_end_date":               extracted_data.get("quarter_end_date"),
        "fiscal_quarter":                 extracted_data.get("fiscal_quarter"),
        "fiscal_year":                    extracted_data.get("fiscal_year"),
        "currency":                       extracted_data.get("currency"),
        "unit_raw":                       extracted_data.get("unit_raw"),
        "revenue_current":                extracted_data.get("revenue_current"),
        "revenue_previous_quarter":       extracted_data.get("revenue_previous_quarter"),
        "revenue_same_quarter_last_year": extracted_data.get("revenue_same_quarter_last_year"),
        "pbt_current":                    extracted_data.get("pbt_current"),
        "pbt_previous_quarter":           extracted_data.get("pbt_previous_quarter"),
        "pbt_same_quarter_last_year":     extracted_data.get("pbt_same_quarter_last_year"),
        "management_commentary":          extracted_data.get("management_commentary"),
        "outlook_summary":                extracted_data.get("outlook_summary"),
        "confidence_score":               extracted_data.get("confidence_score"),
        "analysis_error":                 extracted_data.get("analysis_error"),
        "validation_warnings":            extracted_data.get("validation_warnings"),
        "revenue_qoq":                    extracted_data.get("revenue_qoq"),
        "revenue_yoy":                    extracted_data.get("revenue_yoy"),
        "pbt_qoq":                        extracted_data.get("pbt_qoq"),
        "pbt_yoy":                        extracted_data.get("pbt_yoy"),
    }), 201


@app.route("/pending/<int:pending_id>/reject", methods=["POST"])
def reject_pending(pending_id):
    """Fail an evaluation: rerun the LLM extraction — optionally steered by
    reviewer-supplied instructions — and regenerate the report. Nothing is
    saved to pdf_metadata; the entry stays pending awaiting another review."""
    db = get_db()
    row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if not row["downloaded_at"]:
        return jsonify({
            "error": "not_reviewed",
            "message": "Download and review the report before failing the evaluation.",
        }), 400

    body = request.get_json(silent=True) or {}
    new_instruction = (body.get("instructions") or "").strip()

    extra_instructions = json.loads(row["extra_instructions"] or "[]")
    if new_instruction:
        extra_instructions.append(new_instruction)

    save_path = os.path.join(UPLOAD_FOLDER, row["filename"])
    if not os.path.exists(save_path):
        return jsonify({"error": f"Source file '{row['filename']}' is missing on disk — cannot rerun."}), 500

    combined_instructions = "\n".join(f"- {s}" for s in extra_instructions) if extra_instructions else None

    next_attempt = row["attempt_count"] + 1
    print(f"\n[INFO] Rerunning GPT-5.4-mini analysis for pending #{pending_id} "
          f"(attempt {next_attempt}) with reviewer instructions: {extra_instructions}")
    pdf_text = extract_pdf_text(save_path)
    extracted_data = _run_llm_pipeline(pdf_text, db, extra_instructions=combined_instructions)

    print("\n" + "=" * 50)
    print(f"GPT-5.4-mini Earnings Analysis — rerun (attempt {next_attempt}, still pending review)")
    print("=" * 50)
    print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
    print("=" * 50 + "\n")

    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    db.execute(
        """UPDATE pending_reviews
           SET extracted_data = ?, attempt_count = ?,
               extra_instructions = ?, downloaded_at = NULL, updated_at = ?
           WHERE id = ?""",
        (json.dumps(extracted_data), next_attempt, json.dumps(extra_instructions), now, pending_id),
    )
    db.commit()

    row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
    report_available = False
    try:
        _build_pending_report(db, row)
        report_available = True
    except Exception as exc:
        print(f"[WARNING] Could not regenerate PDF report for pending entry #{pending_id}: {exc}")

    return jsonify({
        "id":                 pending_id,
        "status":             "pending_review",
        "attempt_count":      next_attempt,
        "extra_instructions": extra_instructions,
        "company_name":       extracted_data.get("company_name"),
        "fiscal_quarter":     extracted_data.get("fiscal_quarter"),
        "fiscal_year":        extracted_data.get("fiscal_year"),
        "confidence_score":   extracted_data.get("confidence_score"),
        "analysis_error":     extracted_data.get("analysis_error"),
        "report_available":   report_available,
        "report_url":         f"/pending/{pending_id}/report",
        "message": "Evaluation failed — re-run complete. Download and review the new report.",
    }), 200


@app.route("/pending/<int:pending_id>", methods=["DELETE"])
def delete_pending(pending_id):
    """Discard a pending upload entirely (e.g. wrong file) without ever
    saving anything to pdf_metadata."""
    db = get_db()
    row = db.execute(
        "SELECT filename, report_path FROM pending_reviews WHERE id = ?", (pending_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(UPLOAD_FOLDER, row["filename"])
    if os.path.exists(path):
        os.remove(path)
    if row["report_path"] and os.path.exists(row["report_path"]):
        os.remove(row["report_path"])
    db.execute("DELETE FROM pending_reviews WHERE id = ?", (pending_id,))
    db.commit()
    return jsonify({"deleted": pending_id})


@app.route("/pdfs", methods=["GET"])
def list_pdfs():
    db = get_db()
    rows = db.execute("SELECT * FROM pdf_metadata ORDER BY id DESC").fetchall()
    result = []
    for r in rows:
        row = dict(r)
        # Deserialise validation_warnings from JSON string back to a list
        raw_warnings = row.get("validation_warnings")
        row["validation_warnings"] = json.loads(raw_warnings) if raw_warnings else None
        row = _apply_comparison_data(row, db)
        # Don't leak the server-side filesystem path — expose availability instead
        report_path = row.pop("report_path", None)
        row["report_available"] = bool(report_path)
        result.append(row)
    return jsonify(result)


@app.route("/pdfs/<int:pdf_id>/report", methods=["GET"])
def download_report(pdf_id):
    db = get_db()
    row = db.execute("SELECT * FROM pdf_metadata WHERE id = ?", (pdf_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    try:
        report_path = _build_report_for_row(db, row)
    except Exception as exc:
        return jsonify({"error": f"Could not generate report: {exc}"}), 500

    company = (row["company_name"] or "earnings").strip().replace(" ", "_") or "earnings"
    period = "".join(filter(None, [row["fiscal_quarter"], str(row["fiscal_year"] or "")]))
    suffix = f"entry{pdf_id}_review.pdf"
    download_name = f"{company}_{period}_{suffix}" if period else f"{company}_{suffix}"

    response = send_file(report_path, as_attachment=True, download_name=download_name, max_age=0)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/pdfs/<int:pdf_id>", methods=["DELETE"])
def delete_pdf(pdf_id):
    db = get_db()
    row = db.execute(
        "SELECT filename, report_path FROM pdf_metadata WHERE id = ?", (pdf_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(UPLOAD_FOLDER, row["filename"])
    if os.path.exists(path):
        os.remove(path)
    if row["report_path"] and os.path.exists(row["report_path"]):
        os.remove(row["report_path"])
    db.execute("DELETE FROM pdf_metadata WHERE id = ?", (pdf_id,))
    db.commit()
    return jsonify({"deleted": pdf_id})


@app.route("/evaluate", methods=["POST"])
def evaluate():
    """Run the 5-case synthetic test suite in test_data/ against the live
    extraction pipeline and write a results file to eval_results/."""
    try:
        result = run_evaluation()
        return jsonify(result), 200
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"Evaluation failed: {exc}"}), 500


@app.route("/eval_results/<path:filename>", methods=["GET"])
def download_eval_result(filename):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(EVAL_RESULTS_FOLDER, safe_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Not found"}), 404
    return send_file(file_path, as_attachment=True, download_name=safe_name)


# ---------- Main ----------

if __name__ == "__main__":
    init_db()
    print("Starting Earnings Report Assistant at http://localhost:5000")
    if not os.environ.get("OPENAI_API_KEY"):
        print("[WARNING] OPENAI_API_KEY is not set — GPT-5.4-mini analysis will be skipped.")
    app.run(debug=True, host="0.0.0.0", port=5000)

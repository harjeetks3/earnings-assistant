import hashlib
import json
import math
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
# Moved to pdftext.py. Re-exported here so `app.extract_pdf_text` and friends
# keep working for callers and tests that already reference them.
from pdftext import (  # noqa: E402,F401
    PAGE_SEPARATOR,
    extract_pdf_pages,
    extract_pdf_text,
    get_pdf_metadata,
    page_for_offset,
    pypdf,
)

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

    # ---- Source traceability ----
    # The whole point of recording evidence is that the reviewer can check a
    # figure against the document without hunting for it, so it belongs on the
    # page they actually read — not just in the database.
    trace = data.get("evidence") or []
    if trace:
        story.append(Paragraph("Source Traceability", h2_style))
        rows = [["Figure", "Page", "As printed in the source"]]
        for item in trace:
            label = item.get("metric", "").replace("_", " ")
            current = data.get(item.get("metric"))
            recorded = item.get("value_millions")
            stale = (
                isinstance(current, (int, float)) and isinstance(recorded, (int, float))
                and abs(current - recorded) > 1e-9
            )

            if stale:
                # The figure was rewritten after this trace was taken — comparison
                # columns are recomputed from the database whenever a sibling
                # quarter is approved. Showing the old citation next to the new
                # number would be a false attribution, so say so instead.
                page, shown = "—", "figure has changed since this trace was taken — re-verify"
            elif item.get("match_method") == MATCH_PRIOR_ENTRY:
                page, shown = "—", "carried from a previously approved entry, not this document"
            elif item.get("verified"):
                page = f"p.{item['page_number']}" if item.get("page_number") else "—"
                printed = (item.get("printed_form") or "").strip()
                shown = printed if len(printed) <= 70 else printed[:67] + "…"
            else:
                page, shown = "—", "NOT FOUND IN DOCUMENT — verify manually"
            rows.append([label, page, shown])
        table = Table(rows, colWidths=[150, 40, 300], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#64748b")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            "Figures are located in the extracted text of the source PDF. "
            "A blank page number means the figure could not be traced and must be "
            "checked against the original report by hand.", na_style))

    # ---- Validation warnings ----
    warnings = data.get("validation_warnings") or []
    if warnings:
        story.append(Paragraph(f"Validation Warnings ({len(warnings)})", h2_style))
        for w in warnings:
            story.append(Paragraph(f"⚠ {_esc(w)}", warn_style))

    add_footer()
    doc.build(story)


# ---------- Pydantic validation ----------
# Moved to validation.py, which the bursa package can import without pulling in
# Flask. Re-exported so `app.validate_analysis` and friends keep resolving.
from validation import (  # noqa: E402,F401
    DATE_RE,
    MONETARY_FIELDS,
    VALID_CURRENCY_RE,
    VALID_QUARTERS,
    EarningsReport,
    ValidationError,
    _identifying_tokens,
    _NON_IDENTIFYING_TOKENS,
    _pydantic_available,
    _TOKEN_SPLIT_RE,
    validate_analysis,
)


# ---------- Unit-scale audit ----------
# Moved to audit.py, which bursa/verify.py can import without pulling in Flask.
from audit import (  # noqa: E402,F401
    _ANCHOR_QUORUM,
    _AUDIT_WARNING_PREFIXES,
    _MIN_ANCHOR_MAGNITUDE,
    _SCALE_CANDIDATES,
    _SELF_REPORT_MARKER,
    _SOURCE_ONLY_WARNING_RE,
    _UNIT_PATTERNS,
    _anchored,
    _audit_unit_scale,
    _count_anchors,
    _printed_forms,
    _scale_factor,
    _self_generated_report_warning,
)


# ---------- Shared PDF verification ----------
# The monitor's pre-flight check on a downloaded file (bursa/pipeline.py), reused
# on the ingest path so an uploaded file and a downloaded one have to satisfy the
# same definition of "a usable filing". bursa/ imports nothing from here, so this
# direction of the dependency is the safe one.
from bursa.verify import VERIFIED, verify_pdf_bytes  # noqa: E402


# ---------- Evidence: model proposes, code confirms ----------
#
# "Every financial figure must remain source-traceable" cannot rest on the model
# saying where a number came from — a citation is exactly the kind of thing an
# LLM will produce fluently and wrongly. So the model supplies a quote and this
# code checks it appears verbatim in the extracted text, recording the character
# offset and page. A quote that cannot be located is stored as UNVERIFIED rather
# than dropped: a missing trace is information the reviewer needs.

MATCH_LLM_VERIFIED = "llm_verified"    # model quote found verbatim, containing the figure
MATCH_DETERMINISTIC = "deterministic"  # no usable quote; code located the figure itself
MATCH_UNVERIFIED = "unverified"        # neither worked — provenance is genuinely missing
MATCH_PRIOR_ENTRY = "prior_entry"      # value came from an approved filing, not this document

_SNIPPET_PAD = 60


def _normalise_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def locate_quote(quote: str, pdf_text: str) -> tuple[int, int] | None:
    """Character span of `quote` within `pdf_text`, or None.

    Tries an exact match first, then a whitespace-insensitive one — PDF text
    extraction routinely collapses or inserts whitespace, and rejecting an
    otherwise perfect quote over a double space would report false gaps in
    provenance. Anything looser would start accepting paraphrase, which is the
    thing being guarded against.
    """
    if not quote or not pdf_text:
        return None
    stripped = quote.strip()
    if len(stripped) < 4:
        return None

    index = pdf_text.find(stripped)
    if index != -1:
        return index, index + len(stripped)

    # Whitespace-insensitive: build a regex from the quote's non-space runs.
    parts = [re.escape(p) for p in stripped.split()]
    if not parts:
        return None
    match = re.search(r"\s+".join(parts), pdf_text, re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def _span_contains_figure(span, value: float, factor: float, pdf_text: str) -> bool:
    """Does the cited passage actually contain the number it is cited for?

    Locating a quote only proves the sentence exists. A genuine but mis-keyed
    quote — boilerplate, a cumulative-period row, another metric's line — would
    otherwise be stamped verified and printed under "As printed in the source"
    with a page number, which is worse than admitting we could not trace it.

    Both ways a document states a figure count: the table row carries it as
    printed ("48,200" under a US$'000 heading) while the narrative carries it
    already scaled ("US$48.2 million"). Either is a fair citation.
    """
    start, end = span
    passage = pdf_text[start:end]
    forms = _printed_forms(abs(value) / factor) + _printed_forms(abs(value))
    return any(_anchored(form, passage) for form in forms)


def _digit_count(form: str) -> int:
    return sum(c.isdigit() for c in form)


# Below this many digits a match is coincidence, not evidence: "6.9" finds a
# growth percentage. Mirrors _MIN_ANCHOR_MAGNITUDE in the unit-scale audit.
_MIN_ANCHOR_DIGITS = 3

# How far back to look for the line's label. A statement row puts the caption
# immediately before the figures; a narrative sentence names the metric a few
# words earlier.
_LABEL_WINDOW = 90

# A figure appears several times in a real report — once in the narrative, once
# in the statement, often again in the MD&A — so "appears more than once" is not
# ambiguity. The danger is a DIFFERENT line item sharing the value: 121 is both
# "Profit before tax 148 132 121" and "Selling expenses (121)". Matching the
# caption is what tells those apart.
_METRIC_LABELS = {
    "revenue": re.compile(r"revenue|net\s+sales|turnover|total\s+income", re.I),
    "pbt": re.compile(
        r"(?:profit|loss|earnings)[^.\n]{0,30}?before\s+(?:tax|taxation)"
        r"|before\s+(?:tax|taxation)"
        r"|ordinary\s+(?:profit|income)"
        r"|pre-?tax",
        re.I,
    ),
}


def _locate_figure(value: float, factor: float, pdf_text: str, metric: str = ""):
    """Find the figure's own printed form, preferring a match under the right
    caption.

    A wrong match here is worse than no match: the audit only counts votes,
    whereas this is shown to the reviewer as the source line, with a page
    number, under the heading "As printed in the source".
    """
    family = "revenue" if metric.startswith("revenue") else "pbt" if metric.startswith("pbt") else ""
    label = _METRIC_LABELS.get(family)

    for form in _printed_forms(abs(value) / factor):
        if _digit_count(form) < _MIN_ANCHOR_DIGITS:
            continue
        hits = [
            m for m in re.finditer(rf"(?<![\d,.]){re.escape(form)}(?![\d,.])", pdf_text)
            # A percentage is never the source of a monetary figure.
            if not pdf_text[m.end():m.end() + 1].strip().startswith("%")
        ]
        if not hits:
            continue

        if label:
            captioned = [
                m for m in hits
                if label.search(pdf_text[max(0, m.start() - _LABEL_WINDOW):m.start()])
            ]
            if captioned:
                return captioned[0].start(), captioned[0].end()

        # No caption matched. Accept only an unambiguous single occurrence —
        # picking one of several unlabelled candidates would be a guess.
        if len(hits) == 1:
            return hits[0].start(), hits[0].end()
    return None


def build_evidence(analysis: dict, pdf_text: str, pages: list,
                   document_sourced: set | None = None) -> list[dict]:
    """One evidence record per monetary field that has a value.

    Returns dicts ready for the `evidence` table. Fields with a value always
    produce a record, even when nothing can be traced — that record just carries
    match_method='unverified'.

    `document_sourced` names the fields whose value actually came from THIS
    document. Comparison figures are overwritten during packaging with values
    from a previously approved filing, and citing a line of the uploaded PDF for
    a number that came from somewhere else is a fabricated citation, however
    real the quoted line is. Those are recorded as `prior_entry` instead.
    """
    claimed = analysis.get("evidence") or {}
    if not isinstance(claimed, dict):
        claimed = {}

    records = []
    for field in MONETARY_FIELDS:
        value = analysis.get(field)
        if not isinstance(value, (int, float)):
            continue

        if document_sourced is not None and field not in document_sourced:
            records.append({
                "metric": field, "page_number": None, "char_start": None,
                "char_end": None,
                "snippet": "Carried from a previously approved entry for this "
                           "company, not read from this document.",
                "printed_form": None,
                "match_method": MATCH_PRIOR_ENTRY, "verified": 0,
            })
            continue

        factor = _scale_factor(analysis.get("unit_raw")) or 1.0
        span = locate_quote(str(claimed.get(field) or ""), pdf_text)
        method = MATCH_LLM_VERIFIED if span else None

        # A quote that does not contain its own figure is not evidence for it.
        if span and not _span_contains_figure(span, value, factor, pdf_text):
            span, method = None, None

        if span is None:
            span = _locate_figure(value, factor, pdf_text, metric=field)
            method = MATCH_DETERMINISTIC if span else None

        if span is None:
            records.append({
                "metric": field, "page_number": None, "char_start": None,
                "char_end": None, "snippet": None, "printed_form": None,
                "match_method": MATCH_UNVERIFIED, "verified": 0,
            })
            continue

        start, end = span
        records.append({
            "metric": field,
            "page_number": page_for_offset(start, pages),
            "char_start": start,
            "char_end": end,
            "snippet": pdf_text[max(0, start - _SNIPPET_PAD):end + _SNIPPET_PAD].strip(),
            "printed_form": pdf_text[start:end].strip()[:200],
            "match_method": method,
            "verified": 1,
        })
    return records


def record_evidence(db, analysis: dict, evidence_records: list, *,
                    pending_review_id: int | None = None,
                    pdf_metadata_id: int | None = None,
                    source_attachment_id: int | None = None) -> int:
    """Persist metric observations and their evidence. Best-effort: provenance
    failing to save must not block the review it describes."""
    if not evidence_records:
        return 0
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    written = 0
    try:
        for record in evidence_records:
            cur = db.execute(
                """INSERT INTO metric_observations (
                       pdf_metadata_id, pending_review_id, source_attachment_id,
                       metric, value_millions, currency, unit_raw,
                       scale_factor_applied, observed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (pdf_metadata_id, pending_review_id, source_attachment_id,
                 record["metric"], analysis.get(record["metric"]),
                 analysis.get("currency"), analysis.get("unit_raw"),
                 _scale_factor(analysis.get("unit_raw")), now),
            )
            db.execute(
                """INSERT INTO evidence (
                       metric_observation_id, page_number, char_start, char_end,
                       snippet, printed_form, match_method, verified, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (cur.lastrowid, record["page_number"], record["char_start"],
                 record["char_end"], record["snippet"], record["printed_form"],
                 record["match_method"], record["verified"], now),
            )
            written += 1
        db.commit()
    except sqlite3.Error as exc:
        print(f"[WARNING] Could not record evidence: {exc}")
    return written


def evidence_summary_warning(evidence_records: list) -> list[str]:
    """Tell the reviewer, on the report they actually read, which figures could
    not be traced back to the document."""
    untraced = [r["metric"] for r in evidence_records
                if r["match_method"] == MATCH_UNVERIFIED]
    if not untraced:
        return []
    return [
        f"{len(untraced)} of {len(evidence_records)} figures could not be traced to a "
        f"line in the document ({', '.join(untraced)}) — verify these against the "
        f"original report before relying on them"
    ]


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
  "confidence_score":                <float 0.0-1.0: your overall confidence in the accuracy of the extracted fields>,
  "evidence":                        {"<field name>": "<the exact line of source text the figure came from>", ...}
}

Evidence rules (important):
- For each of the six monetary fields you fill in, add an entry to "evidence" whose value is copied VERBATIM from the report text — the exact line or table row containing that figure, character for character.
- Copy, do not paraphrase, summarise, reformat or re-type from memory. The quote is checked against the source automatically, and a quote that cannot be found is recorded as unverified.
- Quote the line as printed, with its original digits and separators (e.g. "Revenue 49,800 46,200"), not your normalised value.
- If you cannot point to a specific line for a field, omit that field from "evidence" rather than inventing a quote.

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

        choice = response.choices[0]
        content = choice.message.content

        # Diagnose the two ways this comes back unusable before json.loads turns
        # them into a confusing decode error. The evidence block made responses
        # longer, so hitting the model's own output limit is more likely now.
        if choice.finish_reason == "length":
            return {"analysis_error": (
                "The model's response was cut off before the JSON was complete "
                "(finish_reason=length). The document may be too large, or the "
                "requested evidence quotes too long."
            )}
        if not content or not content.strip():
            return {"analysis_error": (
                f"The model returned an empty response (finish_reason="
                f"{choice.finish_reason})."
            )}

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            return {"analysis_error": (
                f"The model's response was not valid JSON ({exc}). "
                f"First 200 characters: {content[:200]!r}"
            )}

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


def _run_llm_pipeline(
    pdf_text: str,
    db,
    extra_instructions: str | None = None,
    pdf_meta: dict | None = None,
    pages: list | None = None,
) -> tuple[dict, list]:
    """Run the LLM extraction + validation + growth calculation.

    Returns (packaged data, evidence records). Evidence is returned rather than
    written here because the caller owns the row it belongs to — a pending
    review on ingest, an approved entry later. `pages` enables page numbers in
    evidence; without it the trace still records character offsets."""
    analysis = analyse_earnings(pdf_text, extra_instructions=extra_instructions)
    analysis_error = analysis.get("analysis_error")
    # Audit the unit normalisation before anything derives from these figures,
    # so QoQ/YoY growth is computed from corrected values.
    scale_warnings: list[str] = []
    evidence_records: list = []
    if not analysis_error:
        analysis, scale_warnings = _audit_unit_scale(analysis, pdf_text)
        scale_warnings = _self_generated_report_warning(pdf_text) + scale_warnings
    growth = compute_qoq_yoy(analysis, db)
    data = _package_extracted_data(analysis, analysis_error, [], growth)
    if not analysis_error:
        # Trace the figures the RECORD holds, not the model's raw output.
        # Packaging replaces the comparison fields with values from previously
        # approved filings, so tracing the raw analysis would cite a line of
        # THIS document for a number that came from a different one.
        #
        # A field is document-sourced only when packaging left it as the model
        # read it. Anything else is carried from a prior entry and is recorded
        # as such rather than given a page citation here.
        #
        # Built after the unit audit too, so a corrected figure is traced to the
        # line it was corrected against, not to the model's original wrong value.
        document_sourced = {
            field for field in MONETARY_FIELDS
            if data.get(field) is not None and data.get(field) == analysis.get(field)
        }
        evidence_records = build_evidence(
            {**data, "evidence": analysis.get("evidence")}, pdf_text, pages or [],
            document_sourced=document_sourced)
        warnings = (scale_warnings
                    + evidence_summary_warning(evidence_records)
                    + validate_analysis(data, pdf_meta=pdf_meta))
        data["validation_warnings"] = warnings if warnings else None
    return data, evidence_records


# ---------- Flask app ----------

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
REPORTS_FOLDER = os.path.join(BASE_DIR, "reports")
TEST_DATA_FOLDER = os.path.join(BASE_DIR, "test_data")
EVAL_RESULTS_FOLDER = os.path.join(BASE_DIR, "eval_results")
# Overridable so a scheduled poll or a test run can be pointed at a scratch
# database instead of the reviewer's working one.
DB_PATH = os.environ.get("EARNINGS_DB_PATH") or os.path.join(BASE_DIR, "pdfs.db")
# Phase 2: PDFs fetched by the monitor land here, kept apart from uploads/ so
# it stays obvious which files a human chose to bring in.
ATTACHMENTS_FOLDER = os.path.join(BASE_DIR, "attachments")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORTS_FOLDER, exist_ok=True)
os.makedirs(EVAL_RESULTS_FOLDER, exist_ok=True)
os.makedirs(ATTACHMENTS_FOLDER, exist_ok=True)


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
    pages = extract_pdf_pages(path)
    pdf_text = PAGE_SEPARATOR.join(pages)
    analysis = analyse_earnings(pdf_text)
    if analysis.get("analysis_error"):
        case["passed"] = False
        case["error"] = f"LLM analysis failed: {analysis['analysis_error']}"
        return case

    # Same audit the upload path runs, so the suite exercises the real pipeline.
    analysis, scale_warnings = _audit_unit_scale(analysis, pdf_text)
    scale_warnings = _self_generated_report_warning(pdf_text) + scale_warnings

    # Evidence coverage is reported but never failed on: it measures how well
    # the model quoted its sources on this run, which varies, and the figures
    # themselves are already asserted strictly below.
    evidence_records = build_evidence(analysis, pdf_text, pages)
    traced = sum(1 for r in evidence_records if r["verified"])
    record("evidence_coverage", True,
           f"{traced}/{len(evidence_records)} figures traced to the document",
           "informational", info_only=True)
    # The count above cannot answer the question the traceability claim rests on:
    # LLM_VERIFIED and DETERMINISTIC both set verified=1, so it does not say how
    # often the model's OWN quote checked out versus the code finding the figure
    # unaided. That is only observable on a live run — the stored result carries
    # no model output to replay — so record it while the run is happening.
    # Every method is listed, including ones this path cannot produce, so the
    # shape is fixed and a zero is distinguishable from a key that was not
    # written when the counts are summed across cases.
    by_method = dict.fromkeys(
        (MATCH_LLM_VERIFIED, MATCH_DETERMINISTIC, MATCH_UNVERIFIED, MATCH_PRIOR_ENTRY), 0)
    for r in evidence_records:
        method = r["match_method"]
        by_method[method] = by_method.get(method, 0) + 1
    record("evidence_match_methods", True, by_method, "informational", info_only=True)
    scale_warnings += evidence_summary_warning(evidence_records)

    for field, expected_val in expected.get("expected_extraction", {}).items():
        if field == "confidence_score":
            # LLM self-assessed and varies run to run — report only, don't fail on it.
            record(field, True, analysis.get(field), expected_val, info_only=True)
            continue
        ok, act, exp = _eval_compare_field(field, analysis.get(field), expected_val, filename)
        record(field, ok, act, exp)

    # ---- Validation warnings ----
    # Two lists, because not every warning is deterministic. `expected_` must all
    # appear. `conditional_` may appear or not: the unit-scale correction only
    # fires when the model actually got the arithmetic wrong, and it does not do
    # that on every run. Asserting it strictly would make the suite demand a model
    # mistake — case 04 passed live with correct figures and failed on that alone.
    # Anything outside both lists is still a failure, so a new warning is caught.
    warnings = scale_warnings + validate_analysis(analysis, pdf_meta=meta)
    expected_warnings = expected.get("expected_validation_warnings", [])
    conditional_warnings = expected.get("conditional_validation_warnings", [])
    norm_actual = {_eval_normalize_warning(w) for w in warnings}
    norm_expected = {_eval_normalize_warning(w) for w in expected_warnings}
    norm_conditional = {_eval_normalize_warning(w) for w in conditional_warnings}

    missing = norm_expected - norm_actual
    unexpected = norm_actual - norm_expected - norm_conditional
    record(
        "validation_warnings",
        not missing and not unexpected,
        warnings,
        {"required": expected_warnings, "optional": conditional_warnings},
    )

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


# ---------- Review report assembly ----------
#
# Turning a stored row — approved or still pending — into the PDF a human reads,
# including the evidence that makes each figure traceable back to its document.

def load_evidence(db, *, pdf_metadata_id: int | None = None,
                  pending_review_id: int | None = None) -> list[dict]:
    """Evidence rows for a record, ordered as the figures appear in the report."""
    if pdf_metadata_id is not None:
        where, param = "o.pdf_metadata_id = ?", pdf_metadata_id
    elif pending_review_id is not None:
        where, param = "o.pending_review_id = ?", pending_review_id
    else:
        return []
    try:
        rows = db.execute(
            f"""SELECT o.metric, o.value_millions,
                       e.page_number, e.printed_form, e.match_method, e.verified
                  FROM metric_observations o
                  JOIN evidence e ON e.metric_observation_id = o.id
                 WHERE {where}""",
            (param,),
        ).fetchall()
    except sqlite3.Error:
        return []
    order = {name: index for index, name in enumerate(MONETARY_FIELDS)}
    return sorted((dict(r) for r in rows), key=lambda r: order.get(r["metric"], 99))


def _row_to_report_data(row: sqlite3.Row, db=None) -> dict:
    """Convert a pdf_metadata DB row into the plain dict shape expected by
    generate_report_pdf (validation_warnings deserialised into a list)."""
    d = dict(row)
    raw_warnings = d.get("validation_warnings")
    d["validation_warnings"] = json.loads(raw_warnings) if raw_warnings else None
    if db is not None:
        d = _apply_comparison_data(d, db)
        d["evidence"] = load_evidence(db, pdf_metadata_id=row["id"])
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
    payload["evidence"] = load_evidence(db, pending_review_id=row["id"])
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


# ---------- Phase 2: monitoring, provenance and review history ----------
#
# All additive. Nothing here changes how a manual upload behaves — the two
# source_attachment_id columns are nullable precisely so an uploaded PDF, which
# has no announcement behind it, stays valid.

PHASE2_TABLES = (
    # Watchlist of record. is_active lets a company stop being polled without
    # losing the announcements already discovered for it.
    """
    CREATE TABLE IF NOT EXISTS companies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code  TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL,
        short_name  TEXT,
        is_active   INTEGER NOT NULL DEFAULT 1,
        source      TEXT NOT NULL DEFAULT 'seed',
        notes       TEXT,
        added_at    TEXT NOT NULL
    )
    """,
    # dedup_key is what makes polling idempotent: re-running over the same
    # window inserts nothing the second time. raw_json keeps the original
    # payload so a parser bug stays diagnosable after the fact.
    """
    CREATE TABLE IF NOT EXISTS announcements (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id             INTEGER REFERENCES companies(id),
        bursa_announcement_id  TEXT,
        dedup_key              TEXT NOT NULL UNIQUE,
        title                  TEXT NOT NULL,
        announcement_type      TEXT,
        announced_at           TEXT,
        url                    TEXT,
        raw_json               TEXT,
        status                 TEXT NOT NULL DEFAULT 'discovered',
        discovered_at          TEXT NOT NULL
    )
    """,
    # sha256 + file_size mirrors the existing manual-upload duplicate rule, so a
    # monitored PDF already uploaded by hand is recognised rather than re-processed.
    # A verified row with no pending review IS the discovery queue — no extra table.
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        announcement_id     INTEGER NOT NULL REFERENCES announcements(id),
        filename            TEXT,
        url                 TEXT,
        sha256              TEXT,
        file_size           INTEGER,
        local_path          TEXT,
        verification_status TEXT NOT NULL DEFAULT 'pending',
        verification_detail TEXT,
        downloaded_at       TEXT,
        UNIQUE (announcement_id, sha256)
    )
    """,
    # The six monetary figures are columns on a single row, which leaves nowhere
    # to hang provenance. Normalising them here is what makes evidence possible.
    """
    CREATE TABLE IF NOT EXISTS metric_observations (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        pdf_metadata_id      INTEGER REFERENCES pdf_metadata(id),
        pending_review_id    INTEGER,
        source_attachment_id INTEGER REFERENCES attachments(id),
        metric               TEXT NOT NULL,
        value_millions       REAL,
        currency             TEXT,
        unit_raw             TEXT,
        scale_factor_applied REAL,
        observed_at          TEXT NOT NULL
    )
    """,
    # match_method records HOW a figure was traced: a model-quoted span that code
    # confirmed verbatim, a deterministic match, or nothing. 'unverified' is a
    # real, recordable outcome — better than implying provenance that isn't there.
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_observation_id INTEGER NOT NULL REFERENCES metric_observations(id),
        page_number           INTEGER,
        char_start            INTEGER,
        char_end              INTEGER,
        snippet               TEXT,
        printed_form          TEXT,
        match_method          TEXT NOT NULL DEFAULT 'unverified',
        verified              INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL
    )
    """,
    # approve_pending() deletes the pending row, destroying the review trail at
    # the exact moment it becomes the record of account. This preserves it.
    """
    CREATE TABLE IF NOT EXISTS review_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_type TEXT NOT NULL,
        subject_id   INTEGER NOT NULL,
        event        TEXT NOT NULL,
        actor        TEXT,
        note         TEXT,
        created_at   TEXT NOT NULL
    )
    """,
)

PHASE2_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_announcements_company ON announcements(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_announcement ON attachments(announcement_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256)",
    "CREATE INDEX IF NOT EXISTS idx_metric_obs_pdf ON metric_observations(pdf_metadata_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_observation ON evidence(metric_observation_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_events_subject ON review_events(subject_type, subject_id)",
)

# Nullable, so every existing row and every manual upload stays valid.
PENDING_REVIEW_COLUMNS = [("source_attachment_id", "INTEGER")]


def _add_missing_columns(db, table: str, columns: list) -> None:
    """Additive ALTER TABLE migration — the pattern the DB has always used, so
    an existing pdfs.db upgrades itself on startup with no data loss."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for col_name, col_type in columns:
        if col_name in existing:
            continue
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            db.commit()
            print(f"[DB] Added column: {table}.{col_name}")
        except sqlite3.OperationalError as e:
            print(f"[DB] Could not add column {table}.{col_name}: {e}")


def record_review_event(db, subject_type: str, subject_id: int, event: str,
                        *, actor: str = "reviewer", note: str | None = None) -> None:
    """Append to the review trail. Deliberately best-effort: an audit-log failure
    must never block or roll back the review action it is describing."""
    try:
        db.execute(
            """INSERT INTO review_events (subject_type, subject_id, event, actor, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (subject_type, subject_id, event, actor, note,
             datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )
        db.commit()
    except sqlite3.Error as exc:
        print(f"[WARNING] Could not record review event {event} for {subject_type}#{subject_id}: {exc}")


def seed_companies_from_file(db, path: str | None = None) -> int:
    """Load watchlist.json into `companies`, reconciling the rows it already has.

    The file is the operator's watchlist, so editing it has to mean something.
    Seeding used to be insert-only, which made that false: setting
    `"is_active": false` on a company already in the database changed nothing and
    the monitor kept polling it. `is_active`, `name` and `short_name` are
    therefore brought into line with the file for every stock code it lists.
    Returns the number of companies actually inserted.

    Nothing is ever deleted. A company dropped from the file still owns the
    announcements discovered for it, and removing the row would orphan them — so
    it is reported and left alone. Deactivating is how you stop polling one.

    A missing or malformed file is not fatal — monitoring is optional, and the
    review tool must still start without it."""
    path = path or WATCHLIST_PATH
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARNING] Could not read watchlist {path}: {exc}")
        return 0
    if not isinstance(entries, list):
        print(f"[WARNING] Watchlist {path} should contain a JSON array — ignoring")
        return 0

    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    inserted = 0
    listed = set()
    changes = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        stock_code = str(entry.get("stock_code") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not stock_code or not name:
            print(f"[WARNING] Watchlist entry missing stock_code or name — skipping: {entry!r}")
            continue
        listed.add(stock_code)
        wanted = {
            "name": name,
            "short_name": entry.get("short_name"),
            "is_active": 1 if entry.get("is_active", True) else 0,
        }
        cur = db.execute(
            """INSERT OR IGNORE INTO companies
                   (stock_code, name, short_name, is_active, source, notes, added_at)
               VALUES (?,?,?,?,'seed',?,?)""",
            (
                stock_code, wanted["name"], wanted["short_name"], wanted["is_active"],
                entry.get("notes"), now,
            ),
        )
        if cur.rowcount:
            inserted += 1
            continue
        # Already known: bring it into line with the file rather than ignoring it.
        existing = db.execute(
            "SELECT id, name, short_name, is_active FROM companies WHERE stock_code = ?",
            (stock_code,),
        ).fetchone()
        if not existing:
            continue
        differing = {f: v for f, v in wanted.items() if existing[f] != v}
        if not differing:
            continue
        db.execute(
            "UPDATE companies SET name = ?, short_name = ?, is_active = ? WHERE id = ?",
            (wanted["name"], wanted["short_name"], wanted["is_active"], existing["id"]),
        )
        changes.append(f"{stock_code} " + ", ".join(
            f"{field} {existing[field]!r} -> {value!r}"
            for field, value in differing.items()))
    db.commit()

    if changes:
        print(f"[DB] Reconciled {len(changes)} company/companies with "
              f"{os.path.basename(path)}: " + "; ".join(changes))
    # Reported, never deleted: the row is what an already-discovered announcement
    # points at as its issuer.
    dropped = [r["stock_code"] for r in
               db.execute("SELECT stock_code FROM companies WHERE source = 'seed'")
               if r["stock_code"] not in listed]
    if dropped:
        print(f"[DB] {len(dropped)} seeded company/companies are no longer listed in "
              f"{os.path.basename(path)} — left in place so their announcements keep "
              f"an issuer; deactivate rather than delete to stop polling: "
              f"{', '.join(dropped)}")
    return inserted


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

        # Phase 2: monitoring, provenance and review history.
        for ddl in PHASE2_TABLES:
            db.execute(ddl)
        for ddl in PHASE2_INDEXES:
            db.execute(ddl)
        db.commit()

        # Migrate existing databases that are missing newer columns
        _add_missing_columns(
            db, "pdf_metadata",
            [("sha256", "TEXT NOT NULL DEFAULT ''")]
            + ANALYSIS_COLUMNS
            + [("source_attachment_id", "INTEGER")],
        )
        _add_missing_columns(db, "pending_reviews", PENDING_REVIEW_COLUMNS)

        # One record of account per document. approve_pending() serialises
        # approvals, but two simultaneous uploads of the same PDF can both pass
        # the check-then-insert duplicate test in ingest_pdf_bytes() and stage
        # two pending reviews; this is what stops both becoming records.
        # Partial, because rows that predate the sha256 column migrated in with
        # DEFAULT '' and there can legitimately be many of those.
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pdf_metadata_sha256 "
                       "ON pdf_metadata(sha256) WHERE sha256 <> ''")
            db.commit()
        except sqlite3.DatabaseError as exc:
            # A database that already holds two rows for one document — exactly
            # what this index prevents — cannot build it. Say so and carry on:
            # approval is still serialised without it.
            print(f"[DB] Could not create unique index on pdf_metadata.sha256: {exc}")

        seeded = seed_companies_from_file(db)
        if seeded:
            print(f"[DB] Seeded {seeded} company/companies from {os.path.basename(WATCHLIST_PATH)}")

        # The comparison backfill deliberately does NOT run here. init_db() is
        # called on every scheduled poll (poll_bursa.py), and
        # _refresh_approved_comparisons() issues UPDATEs against pdf_metadata —
        # the record of account, which no automated path may write to. Approving
        # a review and deleting an approved entry are the only two events that
        # change what a comparison resolves to, and both refresh there, with a
        # person present.

        db.close()


# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


def ingest_pdf_bytes(
    db,
    file_bytes: bytes,
    filename: str,
    *,
    save_folder: str | None = None,
    source_attachment_id: int | None = None,
    existing_path: str | None = None,
) -> dict:
    """Hash, deduplicate, save, extract and stage a PDF for human review.

    This is the ONLY way a PDF enters the system. Both the manual upload route
    and the monitored-attachment route call it, so a discovered PDF cannot end
    up on a second, weaker path that skips the unit-scale audit or the review
    gate. Nothing here writes to pdf_metadata — that still requires a human
    approving via POST /pending/<id>/approve.

    Returns a plain dict, not an HTTP response, so callers map it themselves:
      {"status": "duplicate", "scope": "approved"|"pending", "existing_id": N, ...}
      {"status": "rejected", "reason": "..."}
      {"status": "pending_review", "id": N, ...}
    """
    file_size = len(file_bytes)
    sha256 = hashlib.sha256(file_bytes).hexdigest()

    # Duplicate check — against approved entries...
    existing = db.execute(
        "SELECT id, filename FROM pdf_metadata WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if existing:
        return {
            "status": "duplicate",
            "scope": "approved",
            "existing_id": existing["id"],
            "existing_filename": existing["filename"],
        }

    # ...and against reviews still awaiting a decision
    pending_existing = db.execute(
        "SELECT id, filename FROM pending_reviews WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if pending_existing:
        return {
            "status": "duplicate",
            "scope": "pending",
            "existing_id": pending_existing["id"],
            "existing_filename": pending_existing["filename"],
        }

    # Nothing reaches disk until the bytes are known to be a usable filing.
    # POST /upload gates on the file EXTENSION only, so an HTML page renamed
    # .pdf was written to uploads/ and then blew up inside pypdf — an unhandled
    # 500, with the bytes left behind. This is the same check the monitor already
    # applies to a download (bursa/pipeline.py), reused rather than restated, so
    # the two entry points cannot disagree about what a filing is. It also covers
    # the discovered path, where the file on disk may no longer be the file that
    # was verified when it arrived.
    status, detail = verify_pdf_bytes(file_bytes)
    if status != VERIFIED:
        return {"status": "rejected", "reason": detail}

    # Save file — unless it is already on disk. The monitor has already written
    # the PDF to attachments/, so writing it again would leave two identical
    # copies with the second one timestamp-suffixed.
    if existing_path and os.path.exists(existing_path):
        save_path = existing_path
        safe_name = os.path.basename(existing_path)
    else:
        folder = save_folder or UPLOAD_FOLDER
        safe_name = os.path.basename(filename)
        save_path = os.path.join(folder, safe_name)
        if os.path.exists(save_path):
            base, ext = os.path.splitext(safe_name)
            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(folder, safe_name)
        with open(save_path, "wb") as f:
            f.write(file_bytes)

    # Basic PDF metadata. verify_pdf_bytes() opened these same bytes a moment
    # ago, so a failure here means what landed on disk is not what was checked.
    # Take back the copy we wrote rather than orphaning it — but never a file we
    # were handed: existing_path is the monitor's download, not ours to delete.
    try:
        meta = get_pdf_metadata(save_path)
        pages = extract_pdf_pages(save_path)
    except Exception as exc:
        if save_path != existing_path:
            _remove_quietly(save_path, "unreadable upload")
        return {"status": "rejected",
                "reason": f"The saved PDF could not be read: {type(exc).__name__}: {exc}"}
    uploaded_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # GPT-5.4-mini earnings analysis
    print(f"\n[INFO] Starting GPT-5.4-mini analysis for: {safe_name}")
    pdf_text = PAGE_SEPARATOR.join(pages)
    extracted_data, evidence_records = _run_llm_pipeline(
        pdf_text, db, pdf_meta=meta, pages=pages)

    print("\n" + "=" * 50)
    print("GPT-5.4-mini Earnings Analysis — PENDING REVIEW (not yet saved to the database)")
    print("=" * 50)
    print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
    print("=" * 50 + "\n")

    cur = db.execute(
        """INSERT INTO pending_reviews (
               filename, file_size, sha256, pages, title, author, creator, uploaded_at,
               extracted_data, attempt_count, extra_instructions, created_at, updated_at,
               source_attachment_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            safe_name, file_size, sha256, meta["pages"],
            meta["title"], meta["author"], meta["creator"], uploaded_at,
            json.dumps(extracted_data), 1, "[]", uploaded_at, uploaded_at,
            source_attachment_id,
        ),
    )
    db.commit()
    pending_id = cur.lastrowid
    record_evidence(db, extracted_data, evidence_records,
                    pending_review_id=pending_id,
                    source_attachment_id=source_attachment_id)
    record_review_event(
        db, "pending", pending_id, "extracted",
        note=f"Ingested {safe_name}"
        + (f" from attachment #{source_attachment_id}" if source_attachment_id else " by upload"),
    )

    # Generate the draft PDF report the reviewer will download and read.
    report_available = False
    try:
        row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
        _build_pending_report(db, row)
        report_available = True
    except Exception as exc:
        print(f"[WARNING] Could not generate PDF report for pending entry #{pending_id}: {exc}")

    return {
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
    }


def locate_pending_file(filename: str, db=None,
                        source_attachment_id: int | None = None) -> str | None:
    """Resolve a pending review's stored PDF, by provenance where possible.

    Searching by bare filename is not safe on its own: uploads/ and attachments/
    de-collide only within themselves, so a monitored filing and an unrelated
    manual upload can share a basename. Resolving by name alone then re-extracts
    the wrong document on a rerun, and — worse — deletes the wrong file on
    discard. When the review came from the monitor, its attachment row records
    exactly which file is meant, so use that.
    """
    if db is not None and source_attachment_id:
        try:
            row = db.execute(
                "SELECT local_path FROM attachments WHERE id = ?",
                (source_attachment_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row and row["local_path"] and os.path.exists(row["local_path"]):
            return row["local_path"]

    # No provenance recorded: a manual upload, which lives in uploads/.
    for folder in (UPLOAD_FOLDER, ATTACHMENTS_FOLDER):
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            return path
    return None


def _remove_quietly(path: str | None, what: str = "file") -> bool:
    """Delete a file, reporting rather than raising if it cannot be removed.

    Every caller deletes files as housekeeping *after* the database change that
    matters has already committed. Letting a failed unlink propagate turned a
    successful approval into a 500 and skipped the pending-row cleanup that
    followed it — so the entry was approved and still showed as awaiting review.
    Windows raises exactly this when a reader still holds the file open.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError as exc:
        print(f"[WARNING] Could not remove {what} {path}: {exc}")
        return False


def _remove_source_pdf(db, row, subject: str) -> None:
    """Delete the stored PDF a record was made from — but only if the file on
    disk really is that PDF.

    A basename does not identify a file here: uploads/ and attachments/
    de-collide only within themselves, so a monitored filing and an unrelated
    manual upload can share one. Resolve by provenance, then confirm the bytes
    against the hash the record was made from. Anything that cannot be confirmed
    stays on disk — a stray PDF is a housekeeping annoyance, an unrelated
    document deleted by name is unrecoverable.

    `row` needs filename, sha256 and source_attachment_id; pending_reviews and
    pdf_metadata both carry all three.
    """
    path = locate_pending_file(row["filename"], db, row["source_attachment_id"])
    if not path:
        return
    if not row["sha256"]:
        # Rows that predate the sha256 column migrated in with DEFAULT ''.
        print(f"[WARNING] Not deleting {path}: {subject} records no hash to "
              f"confirm it by. Leaving it in place.")
        return
    try:
        with open(path, "rb") as f:
            on_disk = hashlib.sha256(f.read()).hexdigest()
    except OSError as exc:
        print(f"[WARNING] Not deleting {path}: could not read it to confirm it "
              f"belongs to {subject}: {exc}")
        return
    if on_disk != row["sha256"]:
        print(f"[WARNING] Not deleting {path}: its contents do not match "
              f"{subject}. Leaving it in place.")
        return
    _remove_quietly(path, "source PDF")


def _duplicate_response(result: dict):
    """Map a duplicate result from ingest_pdf_bytes() onto the 409 body the
    frontend already knows how to render."""
    if result["scope"] == "approved":
        return jsonify({
            "error": "duplicate",
            "message": (
                f'This file has already been uploaded and approved as "{result["existing_filename"]}"'
                f" (entry #{result['existing_id']})."
            ),
            "existing_id": result["existing_id"],
        }), 409
    return jsonify({
        "error": "duplicate",
        "message": (
            f'This file is already awaiting review as "{result["existing_filename"]}"'
            f" (pending #{result['existing_id']})."
        ),
        "existing_pending_id": result["existing_id"],
    }), 409


@app.route("/upload", methods=["POST"])
def upload():
    """HTTP wrapper around ingest_pdf_bytes(). Validates the request, then hands
    off to the shared ingestion path."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    # Read into memory so we can hash before touching disk
    result = ingest_pdf_bytes(get_db(), file.read(), file.filename)
    if result["status"] == "duplicate":
        return _duplicate_response(result)
    if result["status"] == "rejected":
        # The upload panel renders `error` verbatim for any non-409, so the
        # reason the reviewer needs has to be in that field.
        return jsonify({"error": f"Not a usable PDF: {result['reason']}"}), 400
    return jsonify(result), 201


# ---------- Discovered (Bursa monitoring) ----------
#
# The discovery queue is not a table: it is the attachments the monitor verified
# but nobody has extracted yet. Extraction is explicitly human-triggered, so no
# OpenAI call is ever spent by the monitor itself.
#
# announcements.status is the queue's memory of what has already been decided:
#
#     discovered --Extract--> extracted --approve--> approved
#          ^                      |                      |
#          +------discard---------+---delete approved----+
#
# 'discovered' is written by bursa/dedup.py when the monitor first sees a filing;
# every other transition is a human action in this file. Anything other than
# 'discovered' means somebody has already spent an extraction on it, so the queue
# must not offer it again. A rerun (POST /pending/<id>/reject) has no transition
# on purpose: the pending row survives, so 'extracted' is still true.
#
# The two return edges are the point. Without them a discard retired the filing
# permanently — the queue said "Already extracted", no button, and no later poll
# re-downloaded it because the attachment row still had its sha256.
ANNOUNCEMENT_SPENT = ("extracted", "approved")


def _set_announcement_status(db, source_attachment_id, status: str) -> None:
    """Move the queue state of the announcement a review came from.

    The only link back is pending_reviews.source_attachment_id ->
    attachments.announcement_id, so a manual upload — which has no attachment —
    is a no-op rather than an error.

    Best-effort, like record_review_event(): this runs after the decision it
    describes has already committed, and a queue bookkeeping failure must never
    turn a successful approval into a 500.
    """
    if not source_attachment_id:
        return
    try:
        db.execute(
            """UPDATE announcements SET status = ?
                WHERE id = (SELECT announcement_id FROM attachments WHERE id = ?)""",
            (status, source_attachment_id),
        )
        db.commit()
    except sqlite3.Error as exc:
        print(f"[WARNING] Could not set announcement status to {status!r} for "
              f"attachment #{source_attachment_id}: {exc}")


def _return_to_discovery_queue(db, source_attachment_id) -> None:
    """Hand a monitored filing back to the queue after a discard or a deletion.

    "Not this" must not mean "never again". The downloaded PDF is deliberately
    left on disk: it belongs to the attachment row, not to the review that was
    just thrown away (extract_discovered() re-uses it via existing_path), so
    Extract can be offered again immediately without another Bursa request.

    If the file has already gone, clear the download state as well. pipeline.py
    treats an attachment that has a sha256 as finished, so a hashed row with no
    file can never come back on any poll — the exact dead end this exists to
    prevent. Clearing sha256 puts the row back on the resume path, which UPDATEs
    this same row rather than inserting another, so its id (and anything
    referencing it) survives.
    """
    if not source_attachment_id:
        return
    _set_announcement_status(db, source_attachment_id, "discovered")
    row = db.execute(
        "SELECT local_path FROM attachments WHERE id = ?", (source_attachment_id,)
    ).fetchone()
    if row and not (row["local_path"] and os.path.exists(row["local_path"])):
        db.execute(
            """UPDATE attachments
                  SET sha256 = NULL, local_path = NULL, downloaded_at = NULL
                WHERE id = ?""",
            (source_attachment_id,),
        )
        db.commit()


@app.route("/discovered", methods=["GET"])
def list_discovered():
    """Verified attachments awaiting a decision, newest announcement first."""
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.filename, a.file_size, a.verification_status,
                  a.verification_detail, a.downloaded_at, a.local_path,
                  n.title, n.announced_at, n.url AS announcement_url,
                  n.status AS announcement_status,
                  c.stock_code, c.name AS company_name,
                  (SELECT COUNT(*) FROM pending_reviews p
                    WHERE p.source_attachment_id = a.id) AS pending_count
             FROM attachments a
             JOIN announcements n ON n.id = a.announcement_id
             LEFT JOIN companies c ON c.id = n.company_id
            ORDER BY n.announced_at DESC, a.id DESC""",
    ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        # A pending row is deleted on approval, so its absence cannot mean "still
        # needs extracting" — that made a finished filing reappear as Verified /
        # Extract (uses API credit). announcements.status is set when the button
        # is pressed and survives the handover, so the two signals together cover
        # in-flight and completed work alike. It reads a SET of statuses, not one
        # literal: approval moves the row on to 'approved', and a discard moves it
        # back to 'discovered' so the filing is offered again. Both are popped
        # unconditionally: short-circuiting would leak the raw column.
        item["in_pending"] = bool(item.pop("pending_count"))
        was_extracted = item.pop("announcement_status", None) in ANNOUNCEMENT_SPENT
        item["extracted"] = item["in_pending"] or was_extracted
        item["available"] = (
            item["verification_status"] == "verified"
            and not item["extracted"]
            and bool(item["local_path"]) and os.path.exists(item["local_path"])
        )
        item.pop("local_path", None)
        out.append(item)
    return jsonify(out)


@app.route("/discovered/<int:attachment_id>/extract", methods=["POST"])
def extract_discovered(attachment_id):
    """Run extraction on a discovered filing. This is the only place a monitored
    PDF costs money, and it takes a human pressing the button.

    Hands off to ingest_pdf_bytes(), the same function POST /upload uses, so a
    monitored filing gets the identical audit, validation and review gate."""
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if row["verification_status"] != "verified":
        return jsonify({
            "error": "not_verified",
            "message": (
                f"This attachment was not verified as a usable filing "
                f"({row['verification_status']}): {row['verification_detail']}"
            ),
        }), 400

    if not row["local_path"] or not os.path.exists(row["local_path"]):
        return jsonify({
            "error": "missing_file",
            "message": "The downloaded PDF is no longer on disk — re-run poll_bursa.py.",
        }), 410

    with open(row["local_path"], "rb") as f:
        file_bytes = f.read()

    result = ingest_pdf_bytes(
        db, file_bytes, os.path.basename(row["local_path"]),
        save_folder=ATTACHMENTS_FOLDER, source_attachment_id=attachment_id,
        existing_path=row["local_path"],
    )
    if result["status"] == "duplicate":
        return _duplicate_response(result)
    if result["status"] == "rejected":
        # It verified when it was downloaded, so the copy on disk has changed
        # since. The attachment row is left exactly as it is on purpose: marking
        # it unusable here would strand the filing the same way discarding one
        # used to. Every retry is deterministic and free — no API call is
        # reached — so nothing is lost by letting the queue keep offering it.
        return jsonify({
            "error": "not_a_usable_pdf",
            "message": f"The stored PDF is no longer usable: {result['reason']}",
        }), 400

    db.execute("UPDATE announcements SET status = 'extracted' WHERE id = ?",
               (row["announcement_id"],))
    db.commit()
    return jsonify(result), 201


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
    record_review_event(db, "pending", pending_id, "downloaded")

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

    # One approval at a time. Nothing serialised this route, so a double-clicked
    # Approve ran two requests that both read the same pending row and both
    # INSERTed: two records of account from one human decision. BEGIN IMMEDIATE
    # takes SQLite's write lock before the row is read, so the second request
    # only gets to look once the first has committed — by which point the
    # pending row it would promote is gone and it is refused.
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return jsonify({
            "error": "busy",
            "message": f"The database is busy — try approving again in a moment ({exc}).",
        }), 409

    # Everything from here to the commit is one transaction: the INSERT, the
    # re-pointed provenance and the deletion of the pending row land together or
    # not at all. A half-done approval that lost the pending row would destroy
    # the reviewer's work, which is worse than no approval at all.
    #
    # Nothing that commits may be called inside it. _refresh_approved_comparisons,
    # _build_report_for_row, record_review_event and _set_announcement_status all
    # commit internally, so moving any of them in here would end the claim early
    # and silently reopen the duplicate window.
    try:
        row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone()
        if not row:
            db.rollback()
            return jsonify({"error": "Not found"}), 404

        if not row["downloaded_at"]:
            db.rollback()
            return jsonify({
                "error": "not_reviewed",
                "message": "Download and review the report before approving.",
            }), 400

        extracted_data = _apply_comparison_data(json.loads(row["extracted_data"]), db)
        if not extracted_data.get("analysis_error"):
            # The unit-scale audit ran at upload time against the PDF text, which is
            # no longer to hand — carry its findings forward rather than losing them
            # from the record the reviewer actually approved.
            prior = extracted_data.get("validation_warnings") or []
            carried = [w for w in prior if _SOURCE_ONLY_WARNING_RE.match(w)]
            warnings = carried + validate_analysis(
                extracted_data, pdf_meta={"title": row["title"], "author": row["author"]}
            )
            extracted_data["validation_warnings"] = warnings if warnings else None
        validation_warnings_json = (
            json.dumps(extracted_data["validation_warnings"])
            if extracted_data.get("validation_warnings") else None
        )

        # source_attachment_id carries the announcement this filing came from.
        # Omitting it left every approved row NULL, so the record of account
        # could not be traced back to the document that produced it.
        cur = db.execute(
            """INSERT INTO pdf_metadata (
                   filename, file_size, sha256, pages, title, author, creator, uploaded_at,
                   source_attachment_id,
                   company_name, quarter_end_date, fiscal_quarter, fiscal_year, currency,
                   unit_raw,
                   revenue_current, revenue_previous_quarter, revenue_same_quarter_last_year,
                   pbt_current, pbt_previous_quarter, pbt_same_quarter_last_year,
                   management_commentary, outlook_summary, confidence_score,
                   analysis_error, validation_warnings,
                   revenue_qoq, revenue_yoy, pbt_qoq, pbt_yoy
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["filename"], row["file_size"], row["sha256"], row["pages"],
                row["title"], row["author"], row["creator"], row["uploaded_at"],
                row["source_attachment_id"],
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
        new_id = cur.lastrowid

        # Re-point the provenance at the approved entry BEFORE building the
        # report (which now happens after the commit). The observations are
        # separate rows, so they survive the pending row being deleted — but
        # load_evidence() keys on pdf_metadata_id, so building first produced an
        # approved report with no Source Traceability section at all, and
        # persisted report_path pointing at it.
        db.execute(
            "UPDATE metric_observations SET pdf_metadata_id = ? WHERE pending_review_id = ?",
            (new_id, pending_id),
        )
        # Deleting the pending row is the claim on this approval: it is in the
        # same transaction as the INSERT, so whoever commits first is the only
        # one who can have promoted it.
        db.execute("DELETE FROM pending_reviews WHERE id = ?", (pending_id,))
        db.commit()
    except sqlite3.IntegrityError:
        # The unique index on pdf_metadata.sha256: this document is already a
        # record of account. Leave the pending review in place for the human.
        db.rollback()
        existing = db.execute(
            "SELECT id, filename FROM pdf_metadata WHERE sha256 = ?", (row["sha256"],)
        ).fetchone()
        return jsonify({
            "error": "duplicate",
            "message": (
                f'This document is already saved as "{existing["filename"]}" (entry '
                f"#{existing['id']}). The pending review has been left in place."
                if existing else
                "This document is already saved as an approved entry. "
                "The pending review has been left in place."
            ),
            "existing_id": existing["id"] if existing else None,
        }), 409
    except Exception:
        # Nothing partial reaches the record of account, and the pending review
        # survives to be approved again.
        db.rollback()
        raise

    _refresh_approved_comparisons(db)

    # It is in the record of account now, so the queue must stop calling it work
    # in progress. 'approved' rather than leaving it at 'extracted': the two mean
    # different things to a person reading the trail, and it makes 'extracted'
    # mean exactly "a pending review exists".
    _set_announcement_status(db, row["source_attachment_id"], "approved")

    report_available = False
    try:
        new_row = db.execute("SELECT * FROM pdf_metadata WHERE id = ?", (new_id,)).fetchone()
        _build_report_for_row(db, new_row)
        report_available = True
    except Exception as exc:
        print(f"[WARNING] Could not generate final PDF report for entry #{new_id}: {exc}")

    # The pending row is gone, so record the trail against both subjects: the
    # pending id that has disappeared and the entry it became. Logging is
    # best-effort by design and stays outside the transaction above — it must
    # never be able to roll back the approval it is describing.
    record_review_event(db, "pending", pending_id, "approved",
                        note=f"Promoted to approved entry #{new_id}")
    record_review_event(db, "approved", new_id, "approved",
                        note=f"Promoted from pending #{pending_id}")

    # Clean up the draft report now the review has been promoted.
    #
    # Deleting the draft is housekeeping and must never fail the approval. The
    # row is already committed to pdf_metadata by this point, so an exception
    # here returned a 500 for an approval that had in fact succeeded — and left
    # the pending row behind, so the same filing showed up as still awaiting
    # review. Windows raises exactly this when the draft is still held open by a
    # reader that has not released it yet.
    _remove_quietly(row["report_path"], "draft report")

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

    save_path = locate_pending_file(
        row["filename"], db, row["source_attachment_id"])
    if not save_path:
        return jsonify({"error": f"Source file '{row['filename']}' is missing on disk — cannot rerun."}), 500

    combined_instructions = "\n".join(f"- {s}" for s in extra_instructions) if extra_instructions else None

    next_attempt = row["attempt_count"] + 1
    print(f"\n[INFO] Rerunning GPT-5.4-mini analysis for pending #{pending_id} "
          f"(attempt {next_attempt}) with reviewer instructions: {extra_instructions}")
    pages = extract_pdf_pages(save_path)
    pdf_text = PAGE_SEPARATOR.join(pages)
    extracted_data, evidence_records = _run_llm_pipeline(
        pdf_text, db,
        extra_instructions=combined_instructions,
        pdf_meta=get_pdf_metadata(save_path),
        pages=pages,
    )

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
    # The rerun produced new figures, so the old trace no longer describes them.
    db.execute(
        """DELETE FROM evidence WHERE metric_observation_id IN
               (SELECT id FROM metric_observations WHERE pending_review_id = ?)""",
        (pending_id,),
    )
    db.execute("DELETE FROM metric_observations WHERE pending_review_id = ?", (pending_id,))
    db.commit()
    record_evidence(db, extracted_data, evidence_records,
                    pending_review_id=pending_id,
                    source_attachment_id=row["source_attachment_id"])
    record_review_event(
        db, "pending", pending_id, "rejected",
        note=(f"Re-run as attempt {next_attempt}"
              + (f'; reviewer instruction: "{new_instruction}"' if new_instruction else "")),
    )

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
        "SELECT filename, report_path, sha256, source_attachment_id "
        "FROM pending_reviews WHERE id = ?", (pending_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if row["source_attachment_id"]:
        # A monitored filing: the PDF in attachments/ is the attachment's, not
        # this review's. Discarding is the reviewer rejecting the extraction, not
        # the filing — so hand it back to the queue and leave the download alone.
        # Deleting it used to retire the announcement permanently, since no later
        # poll re-downloads a row that already has its sha256.
        _return_to_discovery_queue(db, row["source_attachment_id"])
    else:
        # An upload owns its file. Deleting is irreversible, so confirm the file
        # on disk is the one this review was made from before removing it. A
        # basename collision between uploads/ and attachments/ would otherwise
        # destroy an unrelated document.
        _remove_source_pdf(db, row, f"pending review #{pending_id}")
    _remove_quietly(row["report_path"], "draft report")
    record_review_event(
        db, "pending", pending_id, "discarded",
        note=(f"Discarded {row['filename']} without saving"
              + ("; returned to the discovery queue" if row["source_attachment_id"] else "")),
    )
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
        "SELECT filename, report_path, sha256, source_attachment_id "
        "FROM pdf_metadata WHERE id = ?", (pdf_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    if row["source_attachment_id"]:
        # Deleting the record of account for a monitored filing is the reviewer
        # asking to do this one again, so put it back in the queue and keep the
        # download — destroying it would strand the announcement exactly as
        # discarding one used to.
        _return_to_discovery_queue(db, row["source_attachment_id"])
    else:
        # Same rule as discarding a pending review: confirm the file's hash
        # before deleting it. Resolving by basename alone scanned uploads/ then
        # attachments/ and took the first hit, so deleting an approved entry
        # could destroy an unrelated document that happened to share a filename.
        _remove_source_pdf(db, row, f"approved entry #{pdf_id}")
    _remove_quietly(row["report_path"], "report")
    db.execute("DELETE FROM pdf_metadata WHERE id = ?", (pdf_id,))
    db.commit()
    # This entry may have been another quarter's comparison basis. Approval is
    # the event that fills those in; deletion is the event that invalidates them,
    # and both are a person clicking something.
    _refresh_approved_comparisons(db)
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
    if not os.environ.get("OPENAI_API_KEY"):
        print("[WARNING] OPENAI_API_KEY is not set — GPT-5.4-mini analysis will be skipped.")

    # Loopback only, and no debugger.
    #
    # There is no authentication on any route, so anything that can reach this
    # port can POST /pending/<id>/approve — writing to the database of record
    # without a human — or POST /evaluate in a loop, which is five paid API
    # calls per request. Binding 0.0.0.0 put both on every interface of
    # whatever network the machine was on, and debug=True served the Werkzeug
    # console alongside them.
    #
    # EARNINGS_BIND and EARNINGS_DEBUG exist for the deliberate cases (a reverse
    # proxy that adds auth; a developer debugging locally). Both warn, because on
    # an untrusted network either one voids the mandatory human review this whole
    # tool is built around.
    host = os.environ.get("EARNINGS_BIND", "127.0.0.1")
    if host != "127.0.0.1":
        print(f"[WARNING] Binding {host} — every route is unauthenticated, including "
              f"approval and evaluation. Only do this behind something that authenticates.")
    debug = os.environ.get("EARNINGS_DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        # Say it as loudly as the bind warning. The Werkzeug debugger is an
        # interactive Python console on the traceback page: anything that can
        # reach it runs code as this user, on the machine holding pdfs.db, the
        # source PDFs and .env.
        print("[WARNING] EARNINGS_DEBUG is set — the Werkzeug debugger is a remote "
              "code execution console on any error page. Development only.")
        if host != "127.0.0.1":
            print("[DANGER] Debugger enabled AND bound to "
                  f"{host}: that console is reachable from the network, "
                  "unauthenticated. Do not run this. Unset EARNINGS_DEBUG.")

    print(f"Starting Earnings Report Assistant at http://{host}:5000")
    app.run(debug=debug, host=host, port=5000)

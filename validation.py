"""Deterministic checks on what the model returned.

Two things live here: the Pydantic shape check, and the cross-field rules that
catch an extraction which typechecks but cannot be true. Warnings are advisory —
nothing here blocks a review, because the human is the gate.

Split out of app.py so the monitoring package can reuse `_identifying_tokens()`
without importing Flask.
"""
from __future__ import annotations

import re
from datetime import datetime

try:
    from typing import Optional

    from pydantic import BaseModel

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

    from pydantic import ValidationError
    _pydantic_available = True

except ImportError:
    _pydantic_available = False
    ValidationError = Exception


VALID_QUARTERS = {"Q1", "Q2", "Q3", "Q4"}
VALID_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MONETARY_FIELDS = (
    "revenue_current", "revenue_previous_quarter", "revenue_same_quarter_last_year",
    "pbt_current", "pbt_previous_quarter", "pbt_same_quarter_last_year",
)

# Corporate forms, filing boilerplate and generic report vocabulary — none of it
# identifies a company, so it is ignored when comparing a PDF's metadata against
# the company name the model extracted from the document body.
_NON_IDENTIFYING_TOKENS = {
    "inc", "ltd", "limited", "llc", "plc", "corp", "corporation", "co", "company",
    "holding", "holdings", "group", "ab", "publ", "kk", "pte", "bhd", "berhad",
    "nv", "sa", "gmbh", "kgaa", "asa", "oyj", "spa", "pty",
    "annual", "interim", "quarterly", "quarter", "report", "reports", "results",
    "statement", "statements", "financial", "financials", "earnings", "release",
    "final", "draft", "unaudited", "audited", "condensed", "consolidated",
    "full", "year", "half", "fy", "cy", "q1", "q2", "q3", "q4", "tanshin",
    "document", "services", "the", "and", "of", "for",
}

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _identifying_tokens(text) -> set[str]:
    """Words from `text` that could plausibly name a company."""
    if not isinstance(text, str):
        return set()
    return {
        tok for tok in _TOKEN_SPLIT_RE.split(text.lower())
        if len(tok) > 1 and not tok.isdigit() and tok not in _NON_IDENTIFYING_TOKENS
    }


def validate_analysis(raw: dict, pdf_meta: dict | None = None) -> list[str]:
    """Run Pydantic type checks and cross-field consistency checks on the
    LLM output dict.  Returns a (possibly empty) list of warning strings.
    The LLM dict is NOT mutated; warnings are purely informational.

    `pdf_meta`, when supplied, additionally cross-checks the PDF's embedded
    metadata against the company the model read out of the document body."""

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
                f"— unusual, though legitimate with one-off gains such as asset "
                f"disposals or revaluations; confirm against the report"
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
    if all(r.get(f) is None for f in MONETARY_FIELDS):
        warnings.append(
            "No financial values were extracted — the PDF may not contain "
            "machine-readable financial tables"
        )

    # ---- 10. PDF metadata contradicts the document body ----
    # A repurposed template, a mislabelled export or a deliberately misleading
    # file shows up here: the embedded title/author names one company while the
    # body reports another. Deterministic, unlike the model's self-reported
    # confidence, which stays high even on the adversarial fixture.
    if pdf_meta:
        company_tokens = _identifying_tokens(r.get("company_name"))
        meta_text = " ".join(
            str(pdf_meta.get(key) or "") for key in ("title", "author")
        )
        meta_tokens = _identifying_tokens(meta_text)
        if company_tokens and meta_tokens and not (company_tokens & meta_tokens):
            warnings.append(
                f"Document metadata names a different entity than the report body "
                f"(metadata: '{meta_text.strip()}', extracted: "
                f"'{r.get('company_name')}') — verify the file is the report it "
                f"claims to be"
            )

    return warnings

"""Regression tests for the unit-scale audit. Plain asserts, no test runner
required and no API calls:

    python test_unit_scale.py

The audit rewrites financial figures, so a false positive silently corrupts the
numbers a journalist may publish. These cases pin both directions: it must
correct a real 1000x slip, and it must leave everything else alone.
"""
import sys

from app import _audit_unit_scale, _self_generated_report_warning

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


# --- The bug: our own review report fed back in as a source document ----------
# Its table prints values already normalised to millions, while the header still
# quotes "Unit as reported $ '000". Because that unit's factor is 0.001, the
# 0.001 candidate predicts a printed form identical to the model's own output --
# so the check confirmed itself and rescaled correct figures to ~0.
SELF_REPORT = """Quarterly Earnings - Review Report
Trans-Pacific Global Holdings Ltd - Q2 2026
Reporting currency SGD
Unit as reported $ '000
Financial Summary
Metric Current Qtr Prev Qtr Same Qtr LY QoQ % YoY %
Revenue (SGDM) 0.3 - 0.4 - -25.0%
PBT (SGDM) 0.5 - 0.0 - -
All monetary values normalised to millions of the reporting currency.
Auto-generated from a GPT-5.4-mini extraction of entry #30.
"""

raw = {
    "unit_raw": "$ '000",
    "revenue_current": 0.3,
    "revenue_previous_quarter": None,
    "revenue_same_quarter_last_year": 0.4,
    "pbt_current": 0.5,
    "pbt_previous_quarter": None,
    "pbt_same_quarter_last_year": 0.0,
}
out, warns = _audit_unit_scale(dict(raw), SELF_REPORT)

print("self-generated review report re-uploaded:")
check("figures left untouched",
      out["revenue_current"] == 0.3 and out["pbt_current"] == 0.5,
      f"got revenue={out['revenue_current']!r} pbt={out['pbt_current']!r}")
check("no rescale claimed",
      not any(w.startswith("Unit scale corrected:") for w in warns),
      f"got {warns}")
check("self-generated report is flagged",
      _self_generated_report_warning(SELF_REPORT),
      "marker not detected")

# --- Must still catch a genuine 1000x slip -----------------------------------
# Yamato prints "49,800" under a "¥ million" header; the model returned 49.8.
YAMATO = """Yamato Robotics Corporation
Condensed Consolidated Results (¥ million)
Revenue 49,800 46,200 44,100
Profit before tax 5,900 5,400 5,100
"""
raw = {
    "unit_raw": "¥ million",
    "revenue_current": 49.8,
    "revenue_previous_quarter": 46.2,
    "revenue_same_quarter_last_year": 44.1,
    "pbt_current": 5.9,
    "pbt_previous_quarter": 5.4,
    "pbt_same_quarter_last_year": 5.1,
}
out, warns = _audit_unit_scale(dict(raw), YAMATO)

print("\ngenuine 1000x understatement:")
check("figures corrected",
      out["revenue_current"] == 49800.0 and out["pbt_current"] == 5900.0,
      f"got revenue={out['revenue_current']!r} pbt={out['pbt_current']!r}")
check("correction is reported",
      any(w.startswith("Unit scale corrected:") for w in warns),
      f"got {warns}")
check("not flagged as self-generated",
      not _self_generated_report_warning(YAMATO))

# --- Correct extraction must be left alone -----------------------------------
CLEAN = """NorthPeak Analytics Inc
Quarterly Results (USD million)
Revenue 128.4 121.9 115.2
Profit before tax 31.7 29.8 27.4
"""
raw = {
    "unit_raw": "USD million",
    "revenue_current": 128.4,
    "revenue_previous_quarter": 121.9,
    "revenue_same_quarter_last_year": 115.2,
    "pbt_current": 31.7,
    "pbt_previous_quarter": 29.8,
    "pbt_same_quarter_last_year": 27.4,
}
out, warns = _audit_unit_scale(dict(raw), CLEAN)

print("\nalready-correct extraction:")
check("figures untouched", out["revenue_current"] == 128.4,
      f"got {out['revenue_current']!r}")
check("no rescale claimed",
      not any(w.startswith("Unit scale corrected:") for w in warns),
      f"got {warns}")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

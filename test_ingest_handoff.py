"""Guards the ingest_pdf_bytes() refactor. Plain asserts, no runner, no API calls:

    python test_ingest_handoff.py

The point of the refactor is that a monitored PDF and a hand-uploaded PDF travel
the SAME path, so a discovered file cannot end up on a second, weaker route that
skips the unit-scale audit or the review gate. These tests pin that: identical
pending rows from both entry points, duplicate detection shared between them, and
nothing reaching pdf_metadata without a human.

Runs against a temporary database and stubs the LLM, so it costs nothing and
never touches the real pdfs.db.
"""
import json
import os
import shutil
import sys
import tempfile

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


# --- Point the app at a throwaway workspace BEFORE importing it -------------
workspace = tempfile.mkdtemp(prefix="ingest_test_")
os.environ.pop("OPENAI_API_KEY", None)  # ensure no accidental live call

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

app.BASE_DIR = workspace
app.UPLOAD_FOLDER = os.path.join(workspace, "uploads")
app.ATTACHMENTS_FOLDER = os.path.join(workspace, "attachments")
app.REPORTS_FOLDER = os.path.join(workspace, "reports")
app.DB_PATH = os.path.join(workspace, "test.db")
app.WATCHLIST_PATH = os.path.join(workspace, "watchlist.json")  # absent: seeding is a no-op
for folder in (app.UPLOAD_FOLDER, app.ATTACHMENTS_FOLDER, app.REPORTS_FOLDER):
    os.makedirs(folder, exist_ok=True)
app.init_db()

STUB_ANALYSIS = {
    "company_name": "NorthPeak Analytics, Inc.",
    "quarter_end_date": "2026-03-31",
    "fiscal_quarter": "Q1",
    "fiscal_year": 2026,
    "currency": "USD",
    "unit_raw": "US$ '000",
    "revenue_current": 48.2,
    "revenue_previous_quarter": 45.1,
    "revenue_same_quarter_last_year": 38.5,
    "pbt_current": 9.6,
    "pbt_previous_quarter": 8.2,
    "pbt_same_quarter_last_year": 6.1,
    "management_commentary": "Stub.",
    "outlook_summary": "Stub.",
    "confidence_score": 0.95,
}
app.analyse_earnings = lambda text, **kw: dict(STUB_ANALYSIS)

SOURCE_PDF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_data", "01_golden_NorthPeak_Analytics_Q1_FY2026.pdf",
)
pdf_bytes = open(SOURCE_PDF, "rb").read()

# A second, genuinely different PDF so the two ingests aren't caught by dedup.
OTHER_PDF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_data", "02_golden_Lindqvist_Industrial_Q4_FY2025.pdf",
)
other_bytes = open(OTHER_PDF, "rb").read()


def pending_row(db, pending_id):
    return dict(db.execute(
        "SELECT * FROM pending_reviews WHERE id = ?", (pending_id,)).fetchone())


with app.app.app_context():
    db = app.get_db()

    # --- Upload path vs monitored path -------------------------------------
    uploaded = app.ingest_pdf_bytes(db, pdf_bytes, "manual.pdf")
    monitored = app.ingest_pdf_bytes(
        db, other_bytes, "monitored.pdf",
        save_folder=app.ATTACHMENTS_FOLDER, source_attachment_id=42,
    )

    print("both entry points stage a review:")
    check("upload staged", uploaded["status"] == "pending_review", str(uploaded)[:120])
    check("monitored staged", monitored["status"] == "pending_review", str(monitored)[:120])

    up_row = pending_row(db, uploaded["id"])
    mon_row = pending_row(db, monitored["id"])

    print("\nrows are structurally identical:")
    check("same columns", set(up_row) == set(mon_row))
    populated_up = {k for k, v in up_row.items() if v is not None}
    populated_mon = {k for k, v in mon_row.items() if v is not None}
    # source_attachment_id is the ONLY field expected to differ in populated-ness.
    check("same fields populated apart from source_attachment_id",
          populated_up ^ populated_mon == {"source_attachment_id"},
          f"differs on {populated_up ^ populated_mon}")

    print("\nprovenance is recorded, and only for the monitored one:")
    check("monitored carries source_attachment_id", mon_row["source_attachment_id"] == 42,
          repr(mon_row["source_attachment_id"]))
    check("upload has none", up_row["source_attachment_id"] is None,
          repr(up_row["source_attachment_id"]))

    print("\nthe monitored file lands in attachments/, not uploads/:")
    check("saved to attachments/",
          os.path.exists(os.path.join(app.ATTACHMENTS_FOLDER, mon_row["filename"])))
    check("not in uploads/",
          not os.path.exists(os.path.join(app.UPLOAD_FOLDER, mon_row["filename"])))
    check("locate_pending_file finds it",
          app.locate_pending_file(mon_row["filename"]) is not None)

    print("\nthe full pipeline ran on both (audit, validation, growth):")
    for label, row in (("upload", up_row), ("monitored", mon_row)):
        data = json.loads(row["extracted_data"])
        check(f"{label}: figures extracted", data.get("revenue_current") == 48.2,
              repr(data.get("revenue_current")))
        # YoY falls back to the report's own comparative, so it computes.
        check(f"{label}: YoY computed from report comparative",
              data.get("revenue_yoy") == 25.19, repr(data.get("revenue_yoy")))
        # QoQ is DB-only by design (app.py compute_qoq_yoy docstring): a report's
        # "Individual Quarter" column is the same quarter LAST YEAR, not the prior
        # quarter, so it must never be used as one. Empty DB => correctly null.
        check(f"{label}: QoQ stays null on an empty DB",
              data.get("revenue_qoq") is None and data.get("revenue_previous_quarter") is None,
              repr(data.get("revenue_qoq")))
        check(f"{label}: warnings key present", "validation_warnings" in data)

    print("\nreview gate holds — nothing approved automatically:")
    check("pdf_metadata still empty",
          db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0] == 0)
    check("two pending rows",
          db.execute("SELECT COUNT(*) FROM pending_reviews").fetchone()[0] == 2)

    print("\ndeduplication is shared across both entry points:")
    dup = app.ingest_pdf_bytes(db, other_bytes, "same-file-by-hand.pdf")
    check("monitored PDF re-uploaded by hand is caught",
          dup["status"] == "duplicate" and dup["scope"] == "pending", str(dup)[:120])
    check("points at the existing review", dup["existing_id"] == monitored["id"])

    print("\nreview events were recorded:")
    events = db.execute(
        "SELECT subject_type, subject_id, event FROM review_events ORDER BY id").fetchall()
    check("one 'extracted' event per ingest",
          sum(1 for e in events if e["event"] == "extracted") == 2,
          f"got {[tuple(e) for e in events]}")

shutil.rmtree(workspace, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

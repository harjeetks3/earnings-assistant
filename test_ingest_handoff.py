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
import io
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

    print("\nstarting the app never rewrites the record of account:")
    # init_db() runs on every scheduled poll (poll_bursa.py). It used to finish
    # by backfilling comparison columns, which is an UPDATE against pdf_metadata
    # — the record of account, which no automated path may write to. Two
    # approved quarters out of order are exactly what used to trigger it.
    for name, sha, quarter, revenue, pbt in (
        ("q2.pdf", "q2sha", "Q2", 250.0, 25.0),
        ("q3.pdf", "q3sha", "Q3", 300.0, 30.0),
    ):
        db.execute(
            """INSERT INTO pdf_metadata (filename, file_size, sha256, uploaded_at,
                                         company_name, fiscal_quarter, fiscal_year,
                                         revenue_current, pbt_current)
               VALUES (?,1,?,'2026-01-01T00:00:00Z','Backfill Test Bhd',?,2026,?,?)""",
            (name, sha, quarter, revenue, pbt),
        )
    db.commit()  # init_db opens its own connection; do not leave a write lock held

    app.init_db()
    q3 = db.execute("SELECT * FROM pdf_metadata WHERE filename = 'q3.pdf'").fetchone()
    check("init_db left the approved rows exactly as the reviewer approved them",
          q3["revenue_previous_quarter"] is None and q3["revenue_qoq"] is None,
          f"backfilled {q3['revenue_previous_quarter']} / {q3['revenue_qoq']}")

    print("\nbut a human-triggered backfill still works:")
    check("the refresh updates the out-of-order quarter",
          app._refresh_approved_comparisons(db) == 1)
    q3 = db.execute("SELECT * FROM pdf_metadata WHERE filename = 'q3.pdf'").fetchone()
    check("Q3 now carries Q2 as its previous quarter",
          q3["revenue_previous_quarter"] == 250.0 and q3["revenue_qoq"] == 20.0,
          f"{q3['revenue_previous_quarter']} / {q3['revenue_qoq']}")

    print("\nnot one column of an approved row moves on startup, even a stale one:")
    # Stronger than the two columns above: every column of every row. A figure a
    # reviewer approved stays exactly as approved even when the code would now
    # compute a comparison differently — changing it is a human's decision, and
    # startup is not a human.
    db.execute("""UPDATE pdf_metadata
                     SET revenue_previous_quarter = 999.0, revenue_qoq = 111.0
                   WHERE filename = 'q3.pdf'""")
    db.commit()  # init_db opens its own connection; do not leave a write lock held
    before_rows = [dict(r) for r in db.execute("SELECT * FROM pdf_metadata ORDER BY id")]

    app.init_db()

    after_rows = [dict(r) for r in db.execute("SELECT * FROM pdf_metadata ORDER BY id")]
    check("every approved row survives startup byte for byte",
          after_rows == before_rows,
          str([(b, a) for b, a in zip(before_rows, after_rows) if b != a])[:300])

    check("and the human-triggered path does still correct the stale row",
          app._refresh_approved_comparisons(db) == 1)
    q3 = db.execute("SELECT * FROM pdf_metadata WHERE filename = 'q3.pdf'").fetchone()
    check("Q3 carries Q2 again",
          q3["revenue_previous_quarter"] == 250.0 and q3["revenue_qoq"] == 20.0,
          f"{q3['revenue_previous_quarter']} / {q3['revenue_qoq']}")

    print("\nbytes that are not a filing never reach disk:")
    not_a_pdf = b"<html><body>not a filing</body></html>" * 100
    before_files = set(os.listdir(app.UPLOAD_FOLDER))
    pending_before = db.execute("SELECT COUNT(*) FROM pending_reviews").fetchone()[0]
    rejected = app.ingest_pdf_bytes(db, not_a_pdf, "evil.pdf")
    check("ingest refuses it", rejected["status"] == "rejected", str(rejected)[:120])
    check("and says why", "Not a PDF" in rejected["reason"], rejected.get("reason", ""))
    check("no orphan left in uploads/",
          set(os.listdir(app.UPLOAD_FOLDER)) == before_files,
          f"orphaned {set(os.listdir(app.UPLOAD_FOLDER)) - before_files}")
    check("nothing staged for review",
          db.execute("SELECT COUNT(*) FROM pending_reviews").fetchone()[0] == pending_before)

    db.commit()  # the upload route opens its own connection
    upload_client = app.app.test_client()
    resp = upload_client.post(
        "/upload",
        data={"file": (io.BytesIO(not_a_pdf), "evil.pdf")},
        content_type="multipart/form-data",
    )
    check("POST /upload answers 400, not an unhandled 500",
          resp.status_code == 400, str(resp.status_code))
    body = resp.get_json()
    check("the body is JSON the panel can render", isinstance(body, dict), str(body)[:120])
    check("and the reason is in the field the panel shows",
          "Not a usable PDF" in (body or {}).get("error", ""), str(body)[:160])

    print("\nediting watchlist.json changes the companies it already seeded:")
    # Seeding was insert-only, so "is_active": false on a company already in the
    # database did nothing and the monitor kept polling it.
    watchlist = os.path.join(workspace, "watchlist.json")
    with open(watchlist, "w", encoding="utf-8") as f:
        json.dump([{"stock_code": "9999", "name": "Reconcile Bhd",
                    "short_name": "RCB", "is_active": True}], f)
    check("first load inserts it", app.seed_companies_from_file(db, watchlist) == 1)

    with open(watchlist, "w", encoding="utf-8") as f:
        json.dump([{"stock_code": "9999", "name": "Reconcile Holdings Bhd",
                    "short_name": "RCH", "is_active": False}], f)
    check("re-loading inserts nothing", app.seed_companies_from_file(db, watchlist) == 0)
    company = db.execute("SELECT * FROM companies WHERE stock_code = '9999'").fetchone()
    check("deactivating in the file deactivates the row", company["is_active"] == 0,
          repr(company["is_active"]))
    check("and the name and short name follow the file",
          (company["name"], company["short_name"]) == ("Reconcile Holdings Bhd", "RCH"),
          f"{company['name']} / {company['short_name']}")

    with open(watchlist, "w", encoding="utf-8") as f:
        json.dump([], f)
    app.seed_companies_from_file(db, watchlist)
    check("a company dropped from the file is kept, not deleted",
          db.execute("SELECT COUNT(*) FROM companies WHERE stock_code = '9999'"
                     ).fetchone()[0] == 1,
          "deleting it would orphan every announcement pointing at it")

    print("\napproval is all or nothing — a failure part-way never eats the review:")
    # The INSERT into pdf_metadata, the re-pointed provenance and the deletion of
    # the pending row are now one transaction. They used to be separate commits,
    # so a failure after the INSERT left a record of account whose review was
    # still queued: the same filing both approved and awaiting approval, and a
    # second approval waiting to write it again. A trigger refusing the DELETE
    # puts the failure at exactly that point — after the record has been written,
    # before the review has been given up.
    db.commit()  # the route takes SQLite's write lock; do not hold one open here
    gate = app.app.test_client()
    check("[setup] the draft report is downloaded, arming the gate",
          gate.get(f"/pending/{uploaded['id']}/report").status_code == 200)
    entries_before = db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0]
    review_before = pending_row(db, uploaded["id"])

    db.execute("""CREATE TRIGGER refuse_pending_delete BEFORE DELETE ON pending_reviews
                  BEGIN SELECT RAISE(ABORT, 'simulated failure at the last step'); END""")
    db.commit()
    try:
        failed = gate.post(f"/pending/{uploaded['id']}/approve")
    finally:
        db.execute("DROP TRIGGER refuse_pending_delete")
        db.commit()

    check("the approval is refused rather than half-applied",
          400 <= failed.status_code < 600, str(failed.status_code))
    check("nothing reached the record of account",
          db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0] == entries_before,
          "an INSERT that commits before the pending row is deleted leaves both")
    check("the reviewer's work is still there, unchanged",
          pending_row(db, uploaded["id"]) == review_before,
          str({k: v for k, v in pending_row(db, uploaded["id"]).items()
               if review_before.get(k) != v}))
    check("and its evidence still points at the review, not at an entry that never existed",
          db.execute("""SELECT COUNT(*) FROM metric_observations
                         WHERE pending_review_id = ? AND pdf_metadata_id IS NOT NULL""",
                     (uploaded["id"],)).fetchone()[0] == 0)

    print("\nand the same review approves normally once the fault is gone:")
    approved = gate.post(f"/pending/{uploaded['id']}/approve")
    check("approval succeeds", approved.status_code == 201,
          f"{approved.status_code} {approved.get_data(as_text=True)[:160]}")
    check("exactly one record of account was written",
          db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0] == entries_before + 1)
    check("and only now is the pending row given up",
          db.execute("SELECT COUNT(*) FROM pending_reviews WHERE id = ?",
                     (uploaded["id"],)).fetchone()[0] == 0)

shutil.rmtree(workspace, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

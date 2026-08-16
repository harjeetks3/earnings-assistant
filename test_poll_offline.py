"""End-to-end test of the monitoring chain with no network at all:

    python test_poll_offline.py

Covers discovery -> watchlist filter -> dedup -> download -> hash -> verify ->
queue, then the human-triggered extract handoff into the existing review
workflow. Sockets are disabled for the whole run, so a regression that
introduces a live request fails here rather than in production.
"""
import io
import os
import shutil
import socket
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# --- No network, for the entire run ----------------------------------------
_opened = []
_real = socket.socket


class _NoNet(_real):
    def connect(self, *a, **kw):
        _opened.append(a)
        raise AssertionError("Network connection attempted")

    connect_ex = connect


socket.socket = _NoNet

os.environ.pop("OPENAI_API_KEY", None)

import app  # noqa: E402
from bursa import pipeline  # noqa: E402
from bursa.client import BursaClientError, FixtureClient  # noqa: E402
from bursa.verify import REJECTED, VERIFIED, verify_pdf_bytes  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


ws = tempfile.mkdtemp(prefix="poll_test_")
app.UPLOAD_FOLDER = os.path.join(ws, "uploads")
app.ATTACHMENTS_FOLDER = os.path.join(ws, "attachments")
app.REPORTS_FOLDER = os.path.join(ws, "reports")
app.DB_PATH = os.path.join(ws, "t.db")
app.WATCHLIST_PATH = os.path.join(BASE, "watchlist.json")
for d in (app.UPLOAD_FOLDER, app.ATTACHMENTS_FOLDER, app.REPORTS_FOLDER):
    os.makedirs(d, exist_ok=True)
app.init_db()

app.analyse_earnings = lambda text, **kw: {
    "company_name": "NorthPeak Analytics, Inc.", "quarter_end_date": "2026-03-31",
    "fiscal_quarter": "Q1", "fiscal_year": 2026, "currency": "USD",
    "unit_raw": "US$ '000", "revenue_current": 48.2, "revenue_previous_quarter": 45.1,
    "revenue_same_quarter_last_year": 38.5, "pbt_current": 9.6,
    "pbt_previous_quarter": 8.2, "pbt_same_quarter_last_year": 6.1,
    "management_commentary": "s", "outlook_summary": "s", "confidence_score": 0.95,
}

FIXTURES = os.path.join(BASE, "tests", "fixtures", "bursa")
SAMPLE = os.path.join(BASE, "test_data", "01_golden_NorthPeak_Analytics_Q1_FY2026.pdf")


def new_client(listing="announcements_objects.json"):
    return FixtureClient(FIXTURES, listing=listing, sample_pdf=SAMPLE)


# ============================ verification ==================================
print("verification rejects what would waste an API call:")
status, detail = verify_pdf_bytes(open(SAMPLE, "rb").read())
check("a real filing verifies", status == VERIFIED, detail)

status, detail = verify_pdf_bytes(b"<html><body>Error 500</body></html>" * 100)
check("an HTML error page is rejected", status == REJECTED, detail)
check("  and says why", "Not a PDF" in detail, detail)

status, detail = verify_pdf_bytes(open(SAMPLE, "rb").read()[:800])
check("a truncated download is rejected", status == REJECTED, detail)

status, detail = verify_pdf_bytes(open(SAMPLE, "rb").read(), min_text_chars=10 ** 7)
check("a text-poor (scanned) PDF is rejected", status == REJECTED, detail)
check("  and warns it would invent figures", "invent" in detail, detail)

check("empty is rejected", verify_pdf_bytes(b"")[0] == REJECTED)

# ============================ poll ==========================================
with app.app.app_context():
    db = app.get_db()

    print("\nfirst poll:")
    s1 = pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER)
    check("parsed the listing", s1["parsed"] == 4, str(dict(s1)))
    check("all four are results filings", s1["results_filings"] == 4, str(s1["results_filings"]))
    # watchlist.json seeds 1155, 1023, 5347 active and 6012 inactive.
    check("only watchlisted, active companies matched", s1["watchlisted"] == 2,
          f"{s1['watchlisted']} — expected Maybank + CIMB; 9999 untracked, 6012 inactive")
    check("two announcements recorded", s1["new_announcements"] == 2, str(s1["new_announcements"]))
    check("two attachments verified", s1["verified"] == 2, str(s1["verified"]))
    check("nothing rejected", s1["rejected"] == 0, str(s1["messages"]))
    check("no errors", s1["errors"] == 0, str(s1["messages"]))

    print("\nnothing was extracted or approved automatically:")
    check("no pending reviews", db.execute("SELECT COUNT(*) FROM pending_reviews").fetchone()[0] == 0)
    check("no approved rows", db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0] == 0)

    print("\nsecond poll over the same window is a no-op:")
    s2 = pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER)
    check("no new announcements", s2["new_announcements"] == 0, str(s2["new_announcements"]))
    check("skipped as existing", s2["skipped_existing"] == 2, str(s2["skipped_existing"]))
    check("nothing re-downloaded", s2["downloaded"] == 0, str(s2["downloaded"]))
    check("still two announcement rows",
          db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 2)
    check("still two attachment rows",
          db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2)

    print("\nthe REAL listing shape works end to end (files come from the detail page):")
    # The live listing carries only date, company and title. The PDF has to be
    # fetched from the announcement's own page, so this exercises the extra hop
    # the real endpoint requires.
    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()
    db.execute("INSERT OR IGNORE INTO companies (stock_code, name, is_active, source, added_at) "
               "VALUES ('4634','Pos Malaysia Berhad',1,'test','2026-08-16T00:00:00Z')")
    db.commit()

    live_client = FixtureClient(FIXTURES, listing="announcements_live.json",
                                sample_pdf=SAMPLE)
    sl = pipeline.run_poll(db, live_client, attachments_folder=app.ATTACHMENTS_FOLDER)
    check("all six live rows parsed", sl["parsed"] == 6, str(sl["parsed"]))
    check("only the quarterly report is a results filing", sl["results_filings"] == 1,
          str(sl["results_filings"]))
    check("and only the watchlisted company matched", sl["watchlisted"] == 1,
          str(sl["watchlisted"]))
    check("the detail page was fetched for it", sl["detail_pages_fetched"] == 1,
          str(sl["detail_pages_fetched"]))
    check("the PDF was downloaded and verified", sl["verified"] == 1, str(dict(sl)))
    check("no errors", sl["errors"] == 0, str(sl["messages"]))
    check("detail pages were NOT fetched for the five non-matching rows",
          sl["detail_pages_fetched"] == 1,
          "fetching a page per announcement would multiply the request count")

    print("\nan announcement with no PDF is recorded and not retried forever:")
    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()
    bare = FixtureClient(FIXTURES, listing="announcements_live.json",
                         sample_pdf=SAMPLE, detail_pdf=False)
    sn = pipeline.run_poll(db, bare, attachments_folder=app.ATTACHMENTS_FOLDER)
    check("it is counted, not treated as an error",
          sn["no_attachment"] == 1 and sn["errors"] == 0, str(dict(sn)))
    check("nothing was queued", sn["verified"] == 0, str(sn["verified"]))
    check("the announcement is still recorded",
          db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1)

    print("\na download that failed once is retried, not stranded:")
    # The announcement row is written before its attachments are fetched, so a
    # transient failure would otherwise leave the filing permanently invisible:
    # every later poll skips it as "already seen" and its PDF never arrives.
    class _FlakyClient(FixtureClient):
        def __init__(self, *a, fail_pdfs=True, **kw):
            super().__init__(*a, **kw)
            self.fail_pdfs = fail_pdfs

        def fetch(self, path_or_url, params=None, *, cache_name=None):
            if self.fail_pdfs and path_or_url.lower().split("?")[0].endswith(".pdf"):
                raise BursaClientError("simulated timeout")
            return super().fetch(path_or_url, params, cache_name=cache_name)

    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()

    flaky = _FlakyClient(FIXTURES, listing="announcements_objects.json", sample_pdf=SAMPLE)
    sf = pipeline.run_poll(db, flaky, attachments_folder=app.ATTACHMENTS_FOLDER)
    check("announcements were recorded", sf["new_announcements"] == 2, str(sf["new_announcements"]))
    check("but no attachment survived the failure",
          db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0)
    check("the failure was reported", sf["errors"] == 2, str(sf["messages"]))

    recovered = pipeline.run_poll(
        db, FixtureClient(FIXTURES, listing="announcements_objects.json", sample_pdf=SAMPLE),
        attachments_folder=app.ATTACHMENTS_FOLDER)
    check("the next poll retries the missing attachments",
          recovered["downloaded"] == 2, str(recovered["downloaded"]))
    check("and they are now stored and verified",
          db.execute("SELECT COUNT(*) FROM attachments WHERE verification_status='verified'"
                     ).fetchone()[0] == 2)
    check("no duplicate announcements were created",
          db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 2)

    settled = pipeline.run_poll(
        db, FixtureClient(FIXTURES, listing="announcements_objects.json", sample_pdf=SAMPLE),
        attachments_folder=app.ATTACHMENTS_FOLDER)
    check("once complete it goes back to downloading nothing",
          settled["downloaded"] == 0, str(settled["downloaded"]))

    print("\nthe HTML fallback produces the same result:")
    s3 = pipeline.run_poll(db, new_client("announcements_listing.html"),
                           attachments_folder=app.ATTACHMENTS_FOLDER, use_html=True)
    check("matched the same two", s3["watchlisted"] == 2, str(s3["watchlisted"]))
    check("recognised as already seen", s3["skipped_existing"] == 2, str(s3["skipped_existing"]))

    print("\na changed endpoint shape is reported, not silently empty:")
    s4 = pipeline.run_poll(db, new_client("announcements_malformed.json"),
                           attachments_folder=app.ATTACHMENTS_FOLDER)
    check("run reports an error", s4["errors"] == 1, str(dict(s4)))
    check("message names the parser", any("PARSER" in m for m in s4["messages"]),
          str(s4["messages"]))
    check("message tells you what to do",
          any("FIELD_ALIASES" in m for m in s4["messages"]), str(s4["messages"]))

    print("\nan empty window is not an error:")
    s5 = pipeline.run_poll(db, new_client("announcements_empty.json"),
                           attachments_folder=app.ATTACHMENTS_FOLDER)
    check("no errors", s5["errors"] == 0, str(s5["messages"]))
    check("nothing parsed", s5["parsed"] == 0)

    print("\ndry-run writes nothing:")
    before = db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    s6 = pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER,
                           dry_run=True)
    check("announcement count unchanged",
          db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == before)
    check("dry-run still reports what it would do", s6["watchlisted"] == 2)
    check("nothing downloaded", s6["downloaded"] == 0)

# ============================ the human gate ================================
print("\nthe discovery queue and human-triggered extract:")
client = app.app.test_client()
listed = client.get("/discovered").get_json()
check("both filings are listed", len(listed) == 2, str(len(listed)))
check("both are available to extract", all(i["available"] for i in listed), str(listed)[:200])
check("neither is extracted yet", not any(i["extracted"] for i in listed))
check("company is resolved", all(i["stock_code"] in ("1155", "1023") for i in listed),
      str([i["stock_code"] for i in listed]))

first_id = listed[0]["id"]
before_files = sorted(os.listdir(app.ATTACHMENTS_FOLDER))
resp = client.post(f"/discovered/{first_id}/extract")
after_files = sorted(os.listdir(app.ATTACHMENTS_FOLDER))
check("extracting reuses the downloaded PDF instead of writing a second copy",
      before_files == after_files,
      f"added {set(after_files) - set(before_files)}")
check("extract returns 201", resp.status_code == 201, str(resp.status_code))
body = resp.get_json()
check("it staged a pending review", body.get("status") == "pending_review", str(body)[:160])

with app.app.app_context():
    db = app.get_db()
    row = db.execute("SELECT * FROM pending_reviews WHERE id = ?", (body["id"],)).fetchone()
    check("provenance recorded on the review",
          row["source_attachment_id"] == first_id, repr(row["source_attachment_id"]))
    check("still nothing approved automatically",
          db.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0] == 0)
    check("review event written",
          db.execute("SELECT COUNT(*) FROM review_events WHERE event='extracted'"
                     ).fetchone()[0] == 1)

listed_after = client.get("/discovered").get_json()
extracted = [i for i in listed_after if i["id"] == first_id][0]
check("queue marks it extracted", extracted["extracted"] is True)
check("and no longer offers extraction", extracted["available"] is False)

again = client.post(f"/discovered/{first_id}/extract")
check("extracting the same file twice is refused as a duplicate",
      again.status_code == 409, str(again.status_code))

missing = client.post("/discovered/9999/extract")
check("unknown attachment gives 404", missing.status_code == 404)

shutil.rmtree(ws, ignore_errors=True)

print("\noffline guarantee")
check("no socket was opened during the suite", not _opened, str(_opened))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

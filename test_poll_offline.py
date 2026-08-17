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
import threading
import time

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
from bursa.client import BlockedError, BursaClientError, FixtureClient  # noqa: E402
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
    # The attachment row is written before the download precisely so a failure
    # leaves a durable record of outstanding work. An unhashed row means
    # "discovered, not yet downloaded".
    check("the outstanding files are recorded, unhashed",
          db.execute("SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL"
                     ).fetchone()[0] == 2,
          str(db.execute("SELECT id, sha256 FROM attachments").fetchall()))
    check("none is treated as downloaded",
          db.execute("SELECT COUNT(*) FROM attachments WHERE sha256 IS NOT NULL"
                     ).fetchone()[0] == 0)
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

    print("\na multi-PDF announcement whose second file fails is resumed, not stranded:")
    # Adversarial review found the residual gap in the retry fix above: it only
    # covered the all-or-nothing case. With two PDFs and one failure, "any row
    # stored" read as handled and the missing file was never fetched again.
    class _SecondPdfFails(FixtureClient):
        DETAIL_TEMPLATE = (
            "<html><body>"
            "<a href='/misc/announcement/attachment/main_{n}.pdf'>main</a>"
            "<a href='/misc/announcement/attachment/extra_{n}.pdf'>extra</a>"
            "</body></html>"
        )

        # A genuinely different second document, so the two attachments are not
        # collapsed by content hash and the resume is really tested.
        OTHER = os.path.join(BASE, "test_data",
                             "02_golden_Lindqvist_Industrial_Q4_FY2025.pdf")

        def __init__(self, *a, break_extra=True, **kw):
            super().__init__(*a, **kw)
            self.break_extra = break_extra

        def fetch(self, path_or_url, params=None, *, cache_name=None):
            if "announcement_details" in path_or_url:
                n = path_or_url.rsplit("=", 1)[-1]
                return self.DETAIL_TEMPLATE.format(n=n).encode("utf-8")
            if "extra_" in path_or_url:
                if self.break_extra:
                    raise BursaClientError("simulated timeout on the second file")
                with open(self.OTHER, "rb") as f:
                    return f.read()
            return super().fetch(path_or_url, params, cache_name=cache_name)

    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()

    p1 = pipeline.run_poll(db, _SecondPdfFails(FIXTURES, listing="announcements_live.json",
                                               sample_pdf=SAMPLE),
                           attachments_folder=app.ATTACHMENTS_FOLDER)
    check("the first file downloaded", p1["downloaded"] == 1, str(p1["downloaded"]))
    check("the second failure was reported", p1["errors"] == 1, str(p1["messages"]))
    check("but it did not abandon the first", p1["verified"] == 1, str(p1["verified"]))
    check("the missing file is recorded as outstanding",
          db.execute("SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL"
                     ).fetchone()[0] == 1)

    p2 = pipeline.run_poll(db, _SecondPdfFails(FIXTURES, listing="announcements_live.json",
                                               sample_pdf=SAMPLE, break_extra=False),
                           attachments_folder=app.ATTACHMENTS_FOLDER)
    check("the next poll resumes the outstanding file", p2["downloaded"] == 1,
          str(dict(p2)))
    check("nothing is left outstanding",
          db.execute("SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL"
                     ).fetchone()[0] == 0)
    check("and no duplicate rows were created",
          db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2,
          str(db.execute("SELECT id, url, sha256 FROM attachments").fetchall()))

    p3 = pipeline.run_poll(db, _SecondPdfFails(FIXTURES, listing="announcements_live.json",
                                               sample_pdf=SAMPLE, break_extra=False),
                           attachments_folder=app.ATTACHMENTS_FOLDER)
    check("once settled it stops fetching detail pages", p3["detail_pages_fetched"] == 0,
          str(p3["detail_pages_fetched"]))

    print("\n--since is normalised, never compared lexically as typed:")
    # "2026-8-1" against ISO "2026-08-05" compares False at index 5 ('0' < '8'),
    # so an unpadded date used to discard every announcement silently.
    unpadded = pipeline.run_poll(db, FixtureClient(FIXTURES, listing="announcements_live.json",
                                                   sample_pdf=SAMPLE),
                                 attachments_folder=app.ATTACHMENTS_FOLDER,
                                 since="2026-8-1")
    check("an unpadded date is normalised, not applied as typed",
          unpadded["errors"] == 0 and unpadded["parsed"] == 6, str(dict(unpadded)))
    check("  and it still filters (all six are after 2026-08-01)",
          unpadded["results_filings"] == 1, str(unpadded["results_filings"]))

    future = pipeline.run_poll(db, FixtureClient(FIXTURES, listing="announcements_live.json",
                                                 sample_pdf=SAMPLE),
                               attachments_folder=app.ATTACHMENTS_FOLDER,
                               since="2027-1-1")
    check("a later boundary really does exclude them",
          future["results_filings"] == 0, str(future["results_filings"]))

    bad = pipeline.run_poll(db, FixtureClient(FIXTURES, sample_pdf=SAMPLE),
                            attachments_folder=app.ATTACHMENTS_FOLDER,
                            since="last week")
    check("an uninterpretable value is refused, not silently applied",
          bad["errors"] == 1 and any("--since" in m for m in bad["messages"]),
          str(bad["messages"]))
    good = pipeline.run_poll(db, FixtureClient(FIXTURES, listing="announcements_live.json",
                                               sample_pdf=SAMPLE),
                             attachments_folder=app.ATTACHMENTS_FOLDER,
                             since="14 Aug 2026")
    check("a human-formatted date is normalised and applied",
          good["errors"] == 0 and good["parsed"] == 6, str(dict(good)))

    print("\nbeing refused is flagged distinctly from an ordinary error:")
    class _Blocked(FixtureClient):
        def fetch(self, path_or_url, params=None, *, cache_name=None):
            raise BlockedError("HTTP 429")

    blocked = pipeline.run_poll(db, _Blocked(FIXTURES, sample_pdf=SAMPLE),
                                attachments_folder=app.ATTACHMENTS_FOLDER)
    check("the block is recorded as such", blocked["blocked"] is True, str(dict(blocked)))
    check("an ordinary run is not", p3["blocked"] is False, str(p3["blocked"]))

    # Restore the objects-fixture state the next block asserts against; the
    # multi-PDF and --since blocks above deliberately churned it.
    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()
    pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER)

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
# Reset to a known state: the blocks above deliberately left partial and
# failed rows behind, and the queue assertions below are about exactly which
# filings are offered for extraction.
with app.app.app_context():
    db = app.get_db()
    db.execute("DELETE FROM attachments")
    db.execute("DELETE FROM announcements")
    db.commit()
    pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER)

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

print("\nthe queue survives the pending row being deleted on approval:")
# `extracted` was derived from a pending_reviews count, and approval deletes that
# row -- so a finished filing reverted to Verified / Extract (uses API credit).
# No credit was actually burned, because the duplicate check 409s first, but the
# panel whose entire job is saying what still needs attention said the opposite.
pending_id = body["id"]
report = client.get(f"/pending/{pending_id}/report")
check("draft report downloads, arming the approval gate",
      report.status_code == 200, str(report.status_code))
approved = client.post(f"/pending/{pending_id}/approve")
check("approval succeeds", approved.status_code in (200, 201),
      f"{approved.status_code} {approved.get_data(as_text=True)[:160]}")

with app.app.app_context():
    db = app.get_db()
    check("the pending row is gone, as approval intends",
          db.execute("SELECT COUNT(*) FROM pending_reviews WHERE id = ?",
                     (pending_id,)).fetchone()[0] == 0)

done = [i for i in client.get("/discovered").get_json() if i["id"] == first_id][0]
check("an approved filing is still marked extracted", done["extracted"] is True)
check("and is not re-offered for a paid extraction", done["available"] is False)
check("and the panel reports it is no longer pending", done["in_pending"] is False)

print("\nthe approved record knows which filing it was made from:")
with app.app.app_context():
    db = app.get_db()
    entry = db.execute("SELECT id, source_attachment_id FROM pdf_metadata").fetchone()
    approved_id = entry["id"]
    check("provenance carried onto the record of account",
          entry["source_attachment_id"] == first_id, repr(entry["source_attachment_id"]))
    check("the announcement is recorded as approved, not merely extracted",
          db.execute("""SELECT n.status FROM announcements n JOIN attachments a
                          ON a.announcement_id = n.id WHERE a.id = ?""",
                     (first_id,)).fetchone()["status"] == "approved")
    local_path = db.execute("SELECT local_path FROM attachments WHERE id = ?",
                            (first_id,)).fetchone()["local_path"]

print("\ndeleting the approved entry hands the filing back to the queue:")
# Deleting the record of account is the reviewer asking to do this one again.
# The downloaded PDF belongs to the attachment, not to the record, so it stays:
# destroying it would strand the announcement, because no later poll re-downloads
# a row that already has its sha256.
check("delete succeeds", client.delete(f"/pdfs/{approved_id}").status_code == 200)
back = [i for i in client.get("/discovered").get_json() if i["id"] == first_id][0]
check("the filing is offered for extraction again",
      back["extracted"] is False and back["available"] is True, str(back)[:200])
check("and its downloaded PDF was not destroyed with the record",
      os.path.exists(local_path), local_path)

print("\ndiscarding a review returns the filing rather than retiring it:")
again = client.post(f"/discovered/{first_id}/extract")
check("extraction is offered again and works", again.status_code == 201,
      f"{again.status_code} {again.get_data(as_text=True)[:160]}")
pending2 = again.get_json()["id"]
check("discard succeeds", client.delete(f"/pending/{pending2}").status_code == 200)
requeued = [i for i in client.get("/discovered").get_json() if i["id"] == first_id][0]
check("the queue offers it once more",
      requeued["extracted"] is False and requeued["available"] is True, str(requeued)[:200])
check("the download survived the discarded review", os.path.exists(local_path))
third = client.post(f"/discovered/{first_id}/extract")
check("and extracting really does work — the discard was not a dead end",
      third.status_code == 201, str(third.status_code))

print("\na requeued filing whose PDF has gone is re-fetched by the next poll:")
os.remove(local_path)
check("discard succeeds", client.delete(f"/pending/{third.get_json()['id']}").status_code == 200)
with app.app.app_context():
    db = app.get_db()
    row = db.execute("SELECT sha256, local_path FROM attachments WHERE id = ?",
                     (first_id,)).fetchone()
    check("the download state is cleared, putting the row back on the resume path",
          row["sha256"] is None and row["local_path"] is None, str(tuple(row)))
    resumed = pipeline.run_poll(db, new_client(), attachments_folder=app.ATTACHMENTS_FOLDER)
    check("the next poll fetches it again", resumed["downloaded"] >= 1, str(dict(resumed)))
    row = db.execute("SELECT id, sha256, local_path FROM attachments WHERE id = ?",
                     (first_id,)).fetchone()
    check("the same attachment row is completed, not a second one",
          row is not None and row["sha256"] and row["local_path"]
          and os.path.exists(row["local_path"]), str(tuple(row) if row else row))
    check("and no duplicate attachment rows were created",
          db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2,
          str(db.execute("SELECT id, sha256 FROM attachments").fetchall()))

print("\napproving twice writes one record of account, not two:")
# Nothing serialised this route, so a double-clicked Approve ran two requests
# that both read the same pending row and both INSERTed: two records of account
# from one human decision. Calling it twice in sequence cannot reproduce that —
# the second request simply finds the row gone — so both are released together.
staged = client.post(f"/discovered/{first_id}/extract")
check("[setup] the filing is staged for review", staged.status_code == 201,
      f"{staged.status_code} {staged.get_data(as_text=True)[:160]}")
race_pending = staged.get_json()["id"]
check("[setup] the draft report is downloaded, arming the gate",
      client.get(f"/pending/{race_pending}/report").status_code == 200)

race_results = []
race_barrier = threading.Barrier(2)
race_entered = threading.Event()
_real_validate = app.validate_analysis


def _slow_validate(*a, **kw):
    """Hold the first request open just inside the approval.

    Releasing the two requests at the same instant is not enough on its own —
    whether they overlap is then down to thread scheduling. This makes the
    overlap certain: whoever gets in first stays inside the route while the
    other reaches it. Without serialisation both requests have read the same
    pending row by now and both go on to INSERT.
    """
    if not race_entered.is_set():
        race_entered.set()
        time.sleep(0.5)
    return _real_validate(*a, **kw)


def _approve_racing():
    # A client per thread: each request gets its own app context, so each gets
    # its own SQLite connection — two genuinely competing writers.
    thread_client = app.app.test_client()
    race_barrier.wait()
    resp = thread_client.post(f"/pending/{race_pending}/approve")
    race_results.append((resp.status_code, resp.get_json()))


app.validate_analysis = _slow_validate
race_threads = [threading.Thread(target=_approve_racing) for _ in range(2)]
for t in race_threads:
    t.start()
for t in race_threads:
    t.join()
app.validate_analysis = _real_validate

race_codes = sorted(code for code, _ in race_results)
race_created = [body for code, body in race_results if code == 201]
check("exactly one request created an entry", len(race_created) == 1, str(race_codes))
check("the other was refused, not crashed", race_codes[1] < 500, str(race_codes))

with app.app.app_context():
    db = app.get_db()
    entries = db.execute("SELECT id, sha256, source_attachment_id FROM pdf_metadata").fetchall()
    check("one record of account for the filing, not two", len(entries) == 1,
          str([tuple(e) for e in entries]))
    race_entry = entries[0]["id"]
    check("the pending review was consumed exactly once",
          db.execute("SELECT COUNT(*) FROM pending_reviews").fetchone()[0] == 0,
          str(db.execute("SELECT id FROM pending_reviews").fetchall()))
    check("provenance still reaches the announcement it was made from",
          entries[0]["source_attachment_id"] == first_id,
          repr(entries[0]["source_attachment_id"]))
    check("its evidence points at that one entry",
          [r["pdf_metadata_id"] for r in db.execute(
              "SELECT DISTINCT pdf_metadata_id FROM metric_observations "
              "WHERE pending_review_id = ?", (race_pending,))] == [race_entry],
          str(db.execute("SELECT DISTINCT pdf_metadata_id FROM metric_observations "
                         "WHERE pending_review_id = ?", (race_pending,)).fetchall()))

settled_queue = [i for i in client.get("/discovered").get_json() if i["id"] == first_id][0]
check("and the queue stops offering a paid extraction for it",
      settled_queue["extracted"] is True and settled_queue["available"] is False,
      str(settled_queue)[:200])

print("\ndeleting an approved entry never destroys a document it cannot confirm:")
# uploads/ and attachments/ de-collide only within themselves, so a monitored
# filing and an unrelated hand upload can share a basename — and resolving the
# file to delete by name alone searched uploads/ first, so deleting the
# monitored entry destroyed the upload instead of its own PDF.
with app.app.app_context():
    db = app.get_db()
    monitored_name = db.execute("SELECT filename FROM pdf_metadata WHERE id = ?",
                                (race_entry,)).fetchone()["filename"]
    monitored_path = db.execute("SELECT local_path FROM attachments WHERE id = ?",
                                (first_id,)).fetchone()["local_path"]
decoy = os.path.join(app.UPLOAD_FOLDER, monitored_name)
with open(decoy, "wb") as f:
    f.write(b"%PDF-1.4 an unrelated upload that happens to share a name\n")
check("[setup] the same basename now exists in uploads/ and attachments/",
      os.path.exists(decoy) and os.path.exists(monitored_path)
      and os.path.basename(monitored_path) == monitored_name,
      f"{decoy} / {monitored_path}")

check("delete succeeds", client.delete(f"/pdfs/{race_entry}").status_code == 200)
check("the unrelated document sharing the basename survives", os.path.exists(decoy),
      "resolving the file by basename would have destroyed this")
check("and the monitored filing's own download is kept for a re-extraction",
      os.path.exists(monitored_path), monitored_path)

print("\nan upload's file is deleted only while it is still the file that was recorded:")
sample_bytes = open(SAMPLE, "rb").read()


def variant(tag: bytes) -> bytes:
    """A byte-distinct copy of the sample filing, so each case below is its own
    document as far as the duplicate check is concerned. Trailing bytes after
    %%EOF are ignored by readers, so it still parses as a PDF."""
    return sample_bytes + b"\n%% " + tag + b"\n"


def approve_upload(tag: bytes):
    """Hand-upload a document and approve it. Returns (entry id, stored path)."""
    with app.app.app_context():
        result = app.ingest_pdf_bytes(app.get_db(), variant(tag), f"{tag.decode()}.pdf")
    client.get(f"/pending/{result['id']}/report")  # stamps downloaded_at
    entry = client.post(f"/pending/{result['id']}/approve").get_json()
    return entry["id"], os.path.join(app.UPLOAD_FOLDER, result["filename"])


swapped_entry, swapped_path = approve_upload(b"swapped")
with open(swapped_path, "wb") as f:
    f.write(b"%PDF-1.4 a different document now occupies this name\n")
check("delete succeeds", client.delete(f"/pdfs/{swapped_entry}").status_code == 200)
check("a file whose contents are not the recorded ones is left for a human",
      os.path.exists(swapped_path), swapped_path)

owned_entry, owned_path = approve_upload(b"owned")
check("[setup] the upload's own file is on disk", os.path.exists(owned_path), owned_path)
check("delete succeeds", client.delete(f"/pdfs/{owned_entry}").status_code == 200)
check("its own file, hash confirmed, IS removed", not os.path.exists(owned_path),
      "otherwise the checks above would pass by never deleting anything")

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

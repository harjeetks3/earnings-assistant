"""Approval, provenance and deletion — the three places the record of account
can be damaged. Plain asserts, no runner, no API calls:

    python test_approval_gate.py

Approval is the only moment a figure becomes a record of account, and deletion
is the only moment a source document is destroyed. Both were unguarded: nothing
serialised approval, so a double-clicked button could write the same filing
twice, and deletion resolved the file to remove by basename alone, so an entry
could take an unrelated document with it.

Runs against a temporary database with the LLM stubbed, so it costs nothing and
never touches the real pdfs.db.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading

# --- No network, for the entire run ----------------------------------------
# Stubbing analyse_earnings and clearing the API key below is what keeps this
# suite offline in practice. This makes it a guarantee rather than a habit: if
# a future edit reaches a live source, the suite says so instead of quietly
# spending money. Same idiom as test_bursa_offline.py and test_poll_offline.py.
_opened = []
_real_socket = socket.socket


class _NoNet(_real_socket):
    def connect(self, *a, **kw):
        _opened.append(a)
        raise AssertionError("Network connection attempted")

    connect_ex = connect


socket.socket = _NoNet

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


# --- Point the app at a throwaway workspace BEFORE importing it -------------
BASE = os.path.dirname(os.path.abspath(__file__))
ws = tempfile.mkdtemp(prefix="approval_test_")
os.environ.pop("OPENAI_API_KEY", None)  # ensure no accidental live call

sys.path.insert(0, BASE)
import app  # noqa: E402

app.BASE_DIR = ws
app.UPLOAD_FOLDER = os.path.join(ws, "uploads")
app.ATTACHMENTS_FOLDER = os.path.join(ws, "attachments")
app.REPORTS_FOLDER = os.path.join(ws, "reports")
app.DB_PATH = os.path.join(ws, "test.db")
app.WATCHLIST_PATH = os.path.join(ws, "watchlist.json")  # absent: seeding is a no-op
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
    "revenue_same_quarter_last_year": 38.5,
    "pbt_current": 9.6,
    "pbt_same_quarter_last_year": 6.1,
    "management_commentary": "Stub.",
    "outlook_summary": "Stub.",
    "confidence_score": 0.95,
}
app.analyse_earnings = lambda text, **kw: dict(STUB_ANALYSIS)

SOURCE_PDF = os.path.join(BASE, "test_data", "01_golden_NorthPeak_Analytics_Q1_FY2026.pdf")
PDF_BYTES = open(SOURCE_PDF, "rb").read()
client = app.app.test_client()


def variant(tag: bytes) -> bytes:
    """A byte-distinct copy of the sample filing, so the ingest duplicate check
    treats each case here as its own document. Trailing bytes after %%EOF are
    ignored by readers, so the PDF still parses."""
    return PDF_BYTES + b"\n%% " + tag + b"\n"


def stage(tag, *, attachment_id=None, filename=None):
    """Ingest a document and arm its approval gate. Returns the pending id."""
    folder = app.ATTACHMENTS_FOLDER if attachment_id else app.UPLOAD_FOLDER
    with app.app.app_context():
        result = app.ingest_pdf_bytes(
            app.get_db(), variant(tag), filename or f"{tag.decode()}.pdf",
            save_folder=folder, source_attachment_id=attachment_id,
        )
    assert result["status"] == "pending_review", result
    check(f"[setup] {tag.decode()} staged for review", True)
    client.get(f"/pending/{result['id']}/report")  # stamps downloaded_at
    return result["id"]


def query(sql, params=()):
    with app.app.app_context():
        return app.get_db().execute(sql, params).fetchall()


def attach_local_path(attachment_id, pending_id):
    """Point the attachment row at the file the ingest actually wrote, the way
    the monitor would have."""
    with app.app.app_context():
        db = app.get_db()
        stored = db.execute("SELECT filename FROM pending_reviews WHERE id = ?",
                            (pending_id,)).fetchone()["filename"]
        db.execute("UPDATE attachments SET local_path = ? WHERE id = ?",
                   (os.path.join(app.ATTACHMENTS_FOLDER, stored), attachment_id))
        db.commit()
    return stored


# --- announcements to hang monitored provenance on -------------------------
with app.app.app_context():
    db = app.get_db()
    for key in ("t-1", "t-2"):
        cur = db.execute("""INSERT INTO announcements (dedup_key, title, discovered_at)
                            VALUES (?, 'Quarterly report', '2026-08-01T00:00:00Z')""", (key,))
        db.execute("""INSERT INTO attachments (announcement_id, filename, sha256,
                                               verification_status, downloaded_at)
                      VALUES (?, 'monitored.pdf', ?, 'verified', '2026-08-01T00:00:00Z')""",
                   (cur.lastrowid, f"unused-{key}"))
    db.commit()
    ATTACHMENT_ID, ATTACHMENT_ID_2 = [
        r["id"] for r in db.execute("SELECT id FROM attachments ORDER BY id")]

# =========================== provenance on approval =========================
print("\napproval carries provenance onto the record of account:")

monitored_pending = stage(b"monitored", attachment_id=ATTACHMENT_ID)
attach_local_path(ATTACHMENT_ID, monitored_pending)

approved = client.post(f"/pending/{monitored_pending}/approve")
check("monitored filing approves", approved.status_code == 201,
      f"{approved.status_code} {approved.get_data(as_text=True)[:160]}")
monitored_entry = approved.get_json()["id"]
row = query("SELECT * FROM pdf_metadata WHERE id = ?", (monitored_entry,))[0]
check("the approved row records the attachment it came from",
      row["source_attachment_id"] == ATTACHMENT_ID, repr(row["source_attachment_id"]))
check("and the hash of the document it was made from",
      len(row["sha256"] or "") == 64, repr(row["sha256"]))
check("the announcement is marked approved, not still in progress",
      query("""SELECT n.status FROM announcements n JOIN attachments a
                 ON a.announcement_id = n.id WHERE a.id = ?""",
            (ATTACHMENT_ID,))[0]["status"] == "approved",
      str(query("SELECT id, status FROM announcements")))

upload_pending = stage(b"byhand")
upload_entry = client.post(f"/pending/{upload_pending}/approve").get_json()["id"]
check("a hand-uploaded filing still records no attachment",
      query("SELECT source_attachment_id FROM pdf_metadata WHERE id = ?",
            (upload_entry,))[0]["source_attachment_id"] is None)

# ======================= one approval per pending review ====================
print("\na double-clicked Approve writes one record, not two:")
racing_pending = stage(b"racing")
results = []
barrier = threading.Barrier(2)


def approve_racing():
    c = app.app.test_client()
    barrier.wait()
    r = c.post(f"/pending/{racing_pending}/approve")
    results.append((r.status_code, r.get_json()))


threads = [threading.Thread(target=approve_racing) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()

codes = sorted(code for code, _ in results)
created = [body for code, body in results if code == 201]
check("exactly one request created an entry", len(created) == 1, str(codes))
check("the other was refused, not crashed", codes[0] < 500 and codes[1] < 500, str(codes))
rows = query("""SELECT id FROM pdf_metadata WHERE sha256 = (
                  SELECT sha256 FROM pdf_metadata WHERE id = ?)""",
             (created[0]["id"],))
check("one record of account for the filing, not two", len(rows) == 1,
      str([tuple(r) for r in rows]))
check("the pending review is gone",
      query("SELECT * FROM pending_reviews WHERE id = ?", (racing_pending,)) == [])
obs = query("SELECT DISTINCT pdf_metadata_id FROM metric_observations "
            "WHERE pending_review_id = ?", (racing_pending,))
check("its provenance points at the one entry",
      [r["pdf_metadata_id"] for r in obs] == [created[0]["id"]],
      str([tuple(r) for r in obs]))
check("the approval was recorded against both subjects",
      len(query("""SELECT id FROM review_events WHERE event = 'approved'
                    AND ((subject_type = 'pending'  AND subject_id = ?)
                      OR (subject_type = 'approved' AND subject_id = ?))""",
                (racing_pending, created[0]["id"]))) == 2)

# ===================== a failed approval keeps the review ===================
print("\na failure part-way through leaves the review intact:")
fragile_pending = stage(b"fragile")
before = len(query("SELECT id FROM pdf_metadata"))
real_validate = app.validate_analysis


def _boom(*a, **kw):
    raise RuntimeError("simulated failure inside the approval")


app.validate_analysis = _boom
resp = client.post(f"/pending/{fragile_pending}/approve")
app.validate_analysis = real_validate

check("the request fails loudly", resp.status_code == 500, str(resp.status_code))
check("nothing reached the record of account",
      len(query("SELECT id FROM pdf_metadata")) == before)
check("the reviewer's work is still there",
      len(query("SELECT id FROM pending_reviews WHERE id = ?", (fragile_pending,))) == 1)
check("and it can still be approved once the fault is fixed",
      client.post(f"/pending/{fragile_pending}/approve").status_code == 201)

# ================= the same document cannot be recorded twice ===============
print("\nthe same document cannot become two records of account:")
# Two simultaneous uploads of one PDF can both pass the check-then-insert
# duplicate test in ingest_pdf_bytes() and stage two reviews. Simulated here by
# staging a review and recording its document directly.
twin_pending = stage(b"twin")
twin_sha = query("SELECT sha256 FROM pending_reviews WHERE id = ?",
                 (twin_pending,))[0]["sha256"]
with app.app.app_context():
    db = app.get_db()
    db.execute("""INSERT INTO pdf_metadata (filename, file_size, sha256, uploaded_at)
                  VALUES ('twin_by_another_route.pdf', 1, ?, '2026-08-01T00:00:00Z')""",
               (twin_sha,))
    db.commit()

resp = client.post(f"/pending/{twin_pending}/approve")
check("the second approval is refused", resp.status_code == 409,
      f"{resp.status_code} {resp.get_data(as_text=True)[:160]}")
check("it names the entry that already holds the document",
      (resp.get_json() or {}).get("existing_id") is not None, str(resp.get_json())[:160])
check("still one record for the document",
      len(query("SELECT id FROM pdf_metadata WHERE sha256 = ?", (twin_sha,))) == 1)
check("and the pending review was NOT thrown away",
      len(query("SELECT id FROM pending_reviews WHERE id = ?", (twin_pending,))) == 1)

print("\nthe unique index tolerates rows that predate the sha256 column:")
with app.app.app_context():
    db = app.get_db()
    for name in ("legacy_a.pdf", "legacy_b.pdf"):
        db.execute("""INSERT INTO pdf_metadata (filename, file_size, uploaded_at)
                      VALUES (?, 1, '2025-01-01T00:00:00Z')""", (name,))
    db.commit()
    check("two rows with no recorded hash both survive migration", True)

# ================= an unusable document never reaches disk =================
print("\nbytes that are not a filing are refused before anything is written:")
before_files = set(os.listdir(app.UPLOAD_FOLDER))
with app.app.app_context():
    rejected = app.ingest_pdf_bytes(
        app.get_db(), b"<html><body>not a filing</body></html>" * 100, "evil.pdf")
check("ingest refuses it", rejected["status"] == "rejected", str(rejected)[:120])
check("and says why", "Not a PDF" in rejected["reason"], rejected.get("reason", ""))
check("nothing was left in uploads/", set(os.listdir(app.UPLOAD_FOLDER)) == before_files,
      f"orphaned {set(os.listdir(app.UPLOAD_FOLDER)) - before_files}")

# ========================== deleting an approved entry ======================
print("\ndeleting a monitored entry returns the filing to the queue, file intact:")
# The monitored entry's PDF is in attachments/ and belongs to the attachment row,
# not to the record that was made from it. An unrelated document of the same name
# sits in uploads/, which is where a basename search looks first.
monitored_name = query("SELECT filename FROM pdf_metadata WHERE id = ?",
                       (monitored_entry,))[0]["filename"]
decoy = os.path.join(app.UPLOAD_FOLDER, monitored_name)
with open(decoy, "wb") as f:
    f.write(b"%PDF-1.4 an unrelated document that happens to share a name\n")
real_source = os.path.join(app.ATTACHMENTS_FOLDER, monitored_name)
check("[setup] both files exist", os.path.exists(decoy) and os.path.exists(real_source))

resp = client.delete(f"/pdfs/{monitored_entry}")
check("delete succeeds", resp.status_code == 200, str(resp.status_code))
check("the downloaded filing is kept for a re-extraction", os.path.exists(real_source),
      "destroying it would strand the announcement — no poll re-downloads a hashed row")
check("the unrelated document with the same name survives", os.path.exists(decoy),
      "resolving by basename would have destroyed this")
check("the row is gone",
      query("SELECT id FROM pdf_metadata WHERE id = ?", (monitored_entry,)) == [])
check("and the announcement is offered again",
      query("""SELECT n.status FROM announcements n JOIN attachments a
                 ON a.announcement_id = n.id WHERE a.id = ?""",
            (ATTACHMENT_ID,))[0]["status"] == "discovered")

print("\na file that is not the one recorded is left alone:")
tampered_pending = stage(b"tampered")
tampered_entry = client.post(f"/pending/{tampered_pending}/approve").get_json()["id"]
tampered_name = query("SELECT filename FROM pdf_metadata WHERE id = ?",
                      (tampered_entry,))[0]["filename"]
tampered_path = os.path.join(app.UPLOAD_FOLDER, tampered_name)
with open(tampered_path, "wb") as f:
    f.write(b"%PDF-1.4 a different document now occupies this name\n")

check("delete still succeeds", client.delete(f"/pdfs/{tampered_entry}").status_code == 200)
check("the unconfirmed file stays on disk", os.path.exists(tampered_path))
check("the row is still removed",
      query("SELECT id FROM pdf_metadata WHERE id = ?", (tampered_entry,)) == [])

print("\nan entry with no recorded hash never deletes a file by name:")
legacy_path = os.path.join(app.UPLOAD_FOLDER, "legacy_source.pdf")
with open(legacy_path, "wb") as f:
    f.write(b"%PDF-1.4 approved before sha256 was recorded\n")
with app.app.app_context():
    db = app.get_db()
    db.execute("""INSERT INTO pdf_metadata (filename, file_size, sha256, uploaded_at)
                  VALUES ('legacy_source.pdf', 1, '', '2025-01-01T00:00:00Z')""")
    db.commit()
    legacy_id = db.execute("SELECT id FROM pdf_metadata WHERE filename = 'legacy_source.pdf'"
                           ).fetchone()["id"]

check("delete succeeds", client.delete(f"/pdfs/{legacy_id}").status_code == 200)
check("the unconfirmable file is left for a human", os.path.exists(legacy_path))

print("\nan uploaded entry's own file IS deleted, hash confirmed first:")
owned_pending = stage(b"owned")
owned_entry = client.post(f"/pending/{owned_pending}/approve").get_json()["id"]
owned_name = query("SELECT filename FROM pdf_metadata WHERE id = ?",
                   (owned_entry,))[0]["filename"]
owned_path = os.path.join(app.UPLOAD_FOLDER, owned_name)
check("[setup] the file is on disk", os.path.exists(owned_path))
check("delete succeeds", client.delete(f"/pdfs/{owned_entry}").status_code == 200)
check("its file is removed", not os.path.exists(owned_path))

# ======================== discarding a pending review =======================
print("\ndiscarding an uploaded review removes its file, hash-checked:")
discard_pending = stage(b"discard")
discard_name = query("SELECT filename FROM pending_reviews WHERE id = ?",
                     (discard_pending,))[0]["filename"]
discard_path = os.path.join(app.UPLOAD_FOLDER, discard_name)
check("[setup] the file is on disk", os.path.exists(discard_path))
check("discard succeeds", client.delete(f"/pending/{discard_pending}").status_code == 200)
check("its file is removed", not os.path.exists(discard_path))
check("nothing was written to the record of account",
      query("SELECT id FROM pdf_metadata WHERE filename = ?", (discard_name,)) == [])

print("\ndiscarding a monitored review keeps the download and requeues the filing:")
mon_discard = stage(b"mondiscard", attachment_id=ATTACHMENT_ID_2)
mon_name = attach_local_path(ATTACHMENT_ID_2, mon_discard)
mon_path = os.path.join(app.ATTACHMENTS_FOLDER, mon_name)
check("[setup] the download is on disk", os.path.exists(mon_path))
check("discard succeeds", client.delete(f"/pending/{mon_discard}").status_code == 200)
check("the download survives the review being thrown away", os.path.exists(mon_path),
      "'not this extraction' must not mean 'never this filing'")
check("the announcement is back in the queue",
      query("""SELECT n.status FROM announcements n JOIN attachments a
                 ON a.announcement_id = n.id WHERE a.id = ?""",
            (ATTACHMENT_ID_2,))[0]["status"] == "discovered")
check("the trail says it was requeued",
      any("discovery queue" in (r["note"] or "") for r in
          query("SELECT note FROM review_events WHERE subject_type = 'pending' "
                "AND subject_id = ? AND event = 'discarded'", (mon_discard,))))

print("\na discarded monitored filing whose file is gone is put back on the poll:")
gone_pending = stage(b"gone", attachment_id=ATTACHMENT_ID_2)
gone_name = attach_local_path(ATTACHMENT_ID_2, gone_pending)
os.remove(os.path.join(app.ATTACHMENTS_FOLDER, gone_name))
check("discard succeeds", client.delete(f"/pending/{gone_pending}").status_code == 200)
attachment = query("SELECT sha256, local_path, downloaded_at FROM attachments WHERE id = ?",
                   (ATTACHMENT_ID_2,))[0]
check("the download state is cleared so the next poll re-fetches it",
      attachment["sha256"] is None and attachment["local_path"] is None
      and attachment["downloaded_at"] is None,
      str(tuple(attachment)))

shutil.rmtree(ws, ignore_errors=True)

print("\noffline guarantee")
check("no socket was opened during the suite", not _opened, str(_opened))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

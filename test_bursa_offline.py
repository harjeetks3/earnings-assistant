"""Offline tests for the Bursa parser, watchlist matching and deduplication.
Plain asserts, no runner, and — by construction — no network:

    python test_bursa_offline.py

Everything under test is pure or DB-only. `bursa.client` is the sole module that
touches the network and is not imported here at all; a check at the end asserts
that no socket was opened.
"""
import json
import os
import socket
import sys
import tempfile
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(BASE, "tests", "fixtures", "bursa")
sys.path.insert(0, BASE)

# --- Make any real network use fail loudly for the whole run ----------------
_sockets_opened = []
_real_socket = socket.socket


class _NoNetworkSocket(_real_socket):
    def connect(self, *a, **kw):
        _sockets_opened.append(a)
        raise AssertionError("Test suite attempted a network connection")

    connect_ex = connect


socket.socket = _NoNetworkSocket

from bursa import dedup, watchlist  # noqa: E402
from bursa.models import Announcement, normalise_date  # noqa: E402
from bursa.parser import ParserError, parse_html, parse_json  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


def fixture(name, mode="rb"):
    with open(os.path.join(FIXTURES, name), mode) as f:
        return f.read()


# ============================ parser ========================================
print("parser: both payload shapes normalise identically")
objs = parse_json(fixture("announcements_objects.json"))
arrs = parse_json(fixture("announcements_arrays.json"))
htmls = parse_html(fixture("announcements_listing.html"))

check("objects shape parsed", len(objs) == 4, f"got {len(objs)}")
check("arrays shape parsed", len(arrs) == 4, f"got {len(arrs)}")
check("html listing parsed", len(htmls) == 4, f"got {len(htmls)}")


def comparable(a):
    """The fields every shape must agree on. bursa_id and url differ by shape —
    the object payload carries an explicit id, the positional one does not."""
    return (a.stock_code, a.company_name, a.title, a.announcement_type,
            a.announced_at, tuple(att.url for att in a.attachments))


check("objects and arrays agree field-for-field",
      [comparable(a) for a in objs] == [comparable(a) for a in arrs],
      f"\n    objs={[comparable(a) for a in objs][:1]}\n    arrs={[comparable(a) for a in arrs][:1]}")
check("html agrees with json too",
      [comparable(a) for a in objs] == [comparable(a) for a in htmls],
      f"\n    objs={[comparable(a) for a in objs][:1]}\n    html={[comparable(a) for a in htmls][:1]}")

first = objs[0]
print("\nparser: fields are normalised, not passed through raw")
check("stock code extracted", first.stock_code == "1155", repr(first.stock_code))
check("date normalised to ISO", first.announced_at == "2026-08-16", repr(first.announced_at))
check("html stripped from company name",
      "<" not in (first.company_name or ""), repr(first.company_name))
check("pdf attachment found", len(first.attachments) == 1, repr(first.attachments))
check("attachment url is the pdf",
      first.attachments[0].url.endswith("MAYBANK-Q2FY2026-Results.pdf"),
      first.attachments[0].url)
check("raw payload retained for diagnosis", bool(first.raw))

# The title cell holds the detail link AND the attachment link. Flattening it
# would append the PDF filename to the headline. Asserted absolutely, not just
# across shapes, so both shapes being wrong the same way still fails.
EXPECTED_TITLE = ("Quarterly rpt on consolidated results for the financial "
                  "period ended 30 Jun 2026")
check("title is the headline alone", first.title == EXPECTED_TITLE, repr(first.title))
check("array-shape title excludes the pdf filename",
      arrs[0].title == EXPECTED_TITLE, repr(arrs[0].title))
check("html-shape title excludes the pdf filename",
      htmls[0].title == EXPECTED_TITLE, repr(htmls[0].title))

print("\nparser: stock code recovered positionally when there is no explicit field")
check("array shape still finds the code", arrs[0].stock_code == "1155", repr(arrs[0].stock_code))

print("\nparser: detail link and pdf link are not confused")
check("detail url is not the pdf",
      arrs[0].url is not None and not arrs[0].url.endswith(".pdf"), repr(arrs[0].url))

print("\nparser: empty is not the same as broken")
check("empty payload returns []", parse_json(fixture("announcements_empty.json")) == [])

raised = None
try:
    parse_json(fixture("announcements_malformed.json"))
except ParserError as exc:
    raised = exc
check("shape change raises ParserError", isinstance(raised, ParserError), repr(raised))
check("error names the keys actually seen",
      raised is not None and "payload" in str(raised), str(raised)[:160])

raised = None
try:
    parse_json(b"<html>not json at all</html>")
except ParserError as exc:
    raised = exc
check("non-JSON raises ParserError", isinstance(raised, ParserError), repr(raised))

print("\nparser: date handling")
check("dd Mon yyyy", normalise_date("16 Aug 2026") == "2026-08-16")
check("dd/mm/yyyy", normalise_date("16/08/2026") == "2026-08-16")
check("already ISO", normalise_date("2026-08-16") == "2026-08-16")
check("unparseable returns None, does not guess", normalise_date("sometime soon") is None)
check("empty returns None", normalise_date("") is None)

# ============================ watchlist =====================================
print("\nwatchlist: matching")
COMPANIES = [
    watchlist.Company(1, "1155", "Malayan Banking Berhad", "MAYBANK"),
    watchlist.Company(2, "1023", "CIMB Group Holdings Berhad", "CIMB"),
    watchlist.Company(3, "5347", "Tenaga Nasional Berhad", "TENAGA"),
]

matched = list(watchlist.filter_to_watchlist(objs, COMPANIES))
check("only tracked companies survive", len(matched) == 2, f"got {len(matched)}")
check("matched the right two",
      sorted(c.stock_code for _, c in matched) == ["1023", "1155"],
      str([c.stock_code for _, c in matched]))
check("untracked code is dropped",
      all(a.stock_code != "9999" for a, _ in matched))
check("inactive company absent from the list is not matched",
      all(a.stock_code != "6012" for a, _ in matched))

by_name = Announcement(title="Quarterly results", company_name="Tenaga Nasional Berhad")
check("name match works when no code is present",
      watchlist.match_company(by_name, COMPANIES) is not None
      and watchlist.match_company(by_name, COMPANIES).stock_code == "5347")

unknown_code = Announcement(title="Quarterly results", stock_code="4321",
                            company_name="Malayan Banking Berhad")
check("an explicit unknown code does not fall back to a name guess",
      watchlist.match_company(unknown_code, COMPANIES) is None,
      "a wrong code matching by name would put a stranger's filing in the queue")

unrelated = Announcement(title="Quarterly results", company_name="Completely Different Sdn Bhd")
check("unrelated name does not match",
      watchlist.match_company(unrelated, COMPANIES) is None)

check("no company name and no code does not match",
      watchlist.match_company(Announcement(title="x"), COMPANIES) is None)

# ============================ dedup =========================================
print("\ndedup: key stability")
check("same announcement gives the same key",
      dedup.dedup_key(objs[0]) == dedup.dedup_key(arrs[0].__class__(
          title=objs[0].title, stock_code=objs[0].stock_code,
          company_name=objs[0].company_name, announced_at=objs[0].announced_at,
          bursa_id=objs[0].bursa_id)))
check("bursa id is preferred when present",
      dedup.dedup_key(objs[0]).startswith("bursa:"), dedup.dedup_key(objs[0]))
no_id = Announcement(title="Quarterly results", stock_code="1155",
                     company_name="Malayan Banking Berhad", announced_at="2026-08-16")
check("falls back to a content hash without an id",
      dedup.dedup_key(no_id).startswith("sha256:"), dedup.dedup_key(no_id))
check("cosmetic title whitespace does not change the key",
      dedup.dedup_key(no_id) == dedup.dedup_key(
          Announcement(title="  Quarterly   RESULTS ", stock_code="1155",
                       company_name="Malayan Banking Berhad", announced_at="2026-08-16")))
check("a different date is a different announcement",
      dedup.dedup_key(no_id) != dedup.dedup_key(
          Announcement(title="Quarterly results", stock_code="1155",
                       company_name="Malayan Banking Berhad", announced_at="2026-05-16")))
check("same headline, different company, different key",
      dedup.dedup_key(no_id) != dedup.dedup_key(
          Announcement(title="Quarterly results", stock_code="1023",
                       company_name="CIMB Group Holdings Berhad", announced_at="2026-08-16")))

print("\ndedup: inserts are idempotent")
ws = tempfile.mkdtemp(prefix="bursa_dedup_")
os.environ.pop("OPENAI_API_KEY", None)
import app  # noqa: E402

app.UPLOAD_FOLDER = os.path.join(ws, "uploads")
app.ATTACHMENTS_FOLDER = os.path.join(ws, "attachments")
app.REPORTS_FOLDER = os.path.join(ws, "reports")
app.DB_PATH = os.path.join(ws, "t.db")
app.WATCHLIST_PATH = os.path.join(ws, "absent.json")
for d in (app.UPLOAD_FOLDER, app.ATTACHMENTS_FOLDER, app.REPORTS_FOLDER):
    os.makedirs(d, exist_ok=True)
app.init_db()

with app.app.app_context():
    db = app.get_db()
    db.execute("INSERT INTO companies (stock_code, name, is_active, source, added_at) "
               "VALUES ('1155','Malayan Banking Berhad',1,'test','2026-08-16T00:00:00Z')")
    db.commit()
    company_id = db.execute("SELECT id FROM companies").fetchone()["id"]

    created_flags = []
    for _ in range(2):  # simulate polling the same window twice
        for announcement in objs:
            ann_id, created = dedup.upsert_announcement(db, announcement, company_id)
            created_flags.append(created)
            for att in announcement.attachments:
                dedup.upsert_attachment(db, ann_id, att)

    check("first poll inserted all four", sum(created_flags[:4]) == 4, str(created_flags[:4]))
    check("second poll inserted none", sum(created_flags[4:]) == 0, str(created_flags[4:]))
    check("four announcement rows total",
          db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 4)
    check("four attachment rows total",
          db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 4)
    check("raw payload was stored",
          json.loads(db.execute("SELECT raw_json FROM announcements LIMIT 1").fetchone()[0]) != {})

    print("\ndedup: recognises a file the reviewer already handled by hand")
    db.execute("INSERT INTO pdf_metadata (filename, file_size, sha256, uploaded_at) "
               "VALUES ('by-hand.pdf', 1234, 'abc123', '2026-08-16T00:00:00Z')")
    db.commit()
    hit = dedup.already_ingested(db, "abc123", 1234)
    check("approved file is recognised", hit is not None and hit["scope"] == "approved", repr(hit))
    check("unknown file is not", dedup.already_ingested(db, "nope", 1) is None)

shutil.rmtree(ws, ignore_errors=True)

# ============================ no network ====================================
print("\noffline guarantee")
check("no socket was opened during the suite", not _sockets_opened, str(_sockets_opened))
check("bursa.client was never imported", "bursa.client" not in sys.modules)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

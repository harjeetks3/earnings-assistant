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
    """Every field each shape must agree on, INCLUDING bursa_id.

    The id matters most: an object payload carries it explicitly while the
    positional and HTML shapes only have it inside the detail link. If it were
    not recovered from the URL, the same announcement would get a different
    dedup key per shape and falling back to HTML would re-queue everything
    already discovered.

    Attachments are excluded: the real listing carries no file links at all
    (they live on the announcement's detail page), so only the object shape
    has any. That is checked separately.
    """
    return (a.stock_code, a.company_name, a.title,
            a.announced_at, a.bursa_id)


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

print("\nparser: announcement id survives every shape, so dedup keys agree")
check("json id", objs[0].bursa_id == "3512001", repr(objs[0].bursa_id))
check("array id recovered from the detail link", arrs[0].bursa_id == "3512001",
      repr(arrs[0].bursa_id))
check("html id recovered from the detail link", htmls[0].bursa_id == "3512001",
      repr(htmls[0].bursa_id))
check("all three shapes produce the same dedup key",
      len({dedup.dedup_key(objs[0]), dedup.dedup_key(arrs[0]), dedup.dedup_key(htmls[0])}) == 1,
      "otherwise the HTML fallback would re-queue everything already discovered")

print("\nparser: detail link and pdf link are not confused")
check("detail url is not the pdf",
      arrs[0].url is not None and not arrs[0].url.endswith(".pdf"), repr(arrs[0].url))

print("\nparser: empty is not the same as broken")
check("empty payload returns []", parse_json(fixture("announcements_empty.json")) == [])

print("\nparser: malformed HTML degrades, it does not kill the poll")
# An unclosed <td> leaves a cell open past its </tr>; the next </td> then closed
# that cell into a row that no longer existed, raising AttributeError straight
# through parse_html. Only reachable via the opt-in --html fallback -- which is
# precisely the path that runs on a schedule with nobody watching it.
STRAY_END_TD = (
    "<table><tr>"
    "<td>1</td><td>16 Aug 2026</td><td>ACME BERHAD</td><td>Quarterly rpt"
    "</tr></td></table>"
)
stray, crashed = None, None
try:
    stray = parse_html(STRAY_END_TD)
except ParserError as exc:
    crashed = exc
except Exception as exc:  # the regression being guarded against
    crashed = exc
check("stray </td> after </tr> raises nothing unhandled",
      crashed is None or isinstance(crashed, ParserError), repr(crashed))
check("and the unclosed cell is kept rather than dropped",
      stray is not None and len(stray) == 1 and stray[0].title == "Quarterly rpt",
      repr(stray))

NESTED_TABLE = (
    "<table><tr>"
    "<td>1</td><td>16 Aug 2026</td>"
    "<td><table><tr><td>ACME BERHAD</td></tr></table></td>"
    "<td>Quarterly rpt</td>"
    "</tr></table>"
)
crashed = None
try:
    parse_html(NESTED_TABLE)
except ParserError:
    pass
except Exception as exc:
    crashed = exc
check("a nested table raises nothing unhandled", crashed is None, repr(crashed))

check("and a well-formed listing still parses unchanged",
      len(parse_html(fixture("announcements_listing.html"))) == 4)

print("\nparser: the REAL captured response, field by field")
# Captured from the live endpoint 2026-08-16 via the site's own network tab.
# This is the ground truth for COLUMN_ORDER: [row number, date, company, title],
# with no category column, the stock code in the company link's query string,
# and the announcement id in the title link's.
live = parse_json(fixture("announcements_live.json"))
check("all six rows parsed", len(live) == 6, str(len(live)))

by_code = {a.stock_code: a for a in live}
check("stock codes read from the company link",
      sorted(by_code) == ["0823EA", "4634", "5169", "5878", "7212", "7854"],
      str(sorted(by_code)))
check("alphanumeric codes survive (ETFs are not 4 digits)", "0823EA" in by_code)

pos = by_code.get("4634")
check("company name is clean text", pos and pos.company_name == "POS MALAYSIA BERHAD",
      repr(pos.company_name if pos else None))
check("date parsed despite being rendered twice for mobile and desktop",
      pos and pos.announced_at == "2026-08-14", repr(pos.announced_at if pos else None))
check("announcement id read from the title link",
      pos and pos.bursa_id == "3695004", repr(pos.bursa_id if pos else None))
check("url is the announcement, not the company profile",
      pos and "announcement_details" in (pos.url or ""), repr(pos.url if pos else None))
check("title is the headline",
      pos and pos.title == "Quarterly rpt on consolidated results for the "
                           "financial period ended 30/06/2026",
      repr(pos.title if pos else None))

hohup = by_code.get("5169")
check("a trailing <p> description is not folded into the title",
      hohup and hohup.title.endswith("SPECIAL ADMINISTRATOR"),
      repr(hohup.title if hohup else None))

check("the real listing carries no attachments (they are on the detail page)",
      all(not a.attachments for a in live))

print("\npipeline: the results filter picks the quarterly report out of the noise")
from bursa.pipeline import looks_like_results  # noqa: E402

results = [a for a in live if looks_like_results(a)]
check("exactly one results filing among the six", len(results) == 1, str(len(results)))
check("and it is the POS Malaysia quarterly report",
      results and results[0].stock_code == "4634",
      str([r.stock_code for r in results]))
check("boardroom changes are not mistaken for results",
      not any("Boardroom" in r.title for r in results))

print("\npipeline: the category filter narrows, but does not replace, the title gate")
# Real capture with cat=FA,FRCO — the site's own Financial Results filter, which
# takes the listing from 2,087,762 rows to 1,081. It still admits filings that
# are not results, so the title gate stays necessary.
results_live = parse_json(fixture("announcements_results_live.json"))
check("all six category-filtered rows parsed", len(results_live) == 6, str(len(results_live)))

kept = [a for a in results_live if looks_like_results(a)]
dropped = [a for a in results_live if not looks_like_results(a)]
check("five of six are results filings", len(kept) == 5, str(len(kept)))
check("'Change in Financial Year End' is dropped despite being in the category",
      len(dropped) == 1 and dropped[0].title == "Change in Financial Year End",
      str([d.title for d in dropped]))

# The regression this fixture exists for: Key Asic's filing says "Consolidated
# results…" with no "Quarterly rpt" prefix, and was silently dropped before.
key_asic = [a for a in kept if a.title.startswith("Consolidated results")]
check("a results filing without the word 'quarterly' is kept",
      len(key_asic) == 1, str([a.title[:50] for a in kept]))
check("  and it is Key Asic, code 0143",
      key_asic and key_asic[0].stock_code == "0143",
      repr(key_asic[0].stock_code if key_asic else None))

amp = [a for a in results_live if a.stock_code == "7204"]
check("an ampersand in a company name survives parsing",
      amp and amp[0].company_name == "D & O GREEN TECHNOLOGIES BERHAD",
      repr(amp[0].company_name if amp else None))

check("dd/mm/yyyy inside a title does not become the announcement date",
      all(a.announced_at and a.announced_at.startswith("2026-") for a in results_live),
      str([a.announced_at for a in results_live]))

print("\nparser: attachments come from the announcement detail page")
from bursa.parser import parse_attachment_page  # noqa: E402

DETAIL = """<html><body>
  <h1>Quarterly rpt on consolidated results</h1>
  <a href="/market_information/announcements/company_announcement/x">Back</a>
  <a href="/misc/announcement/attachment/POS-Q2FY2026.pdf">POS-Q2FY2026.pdf</a>
  <iframe src="/misc/announcement/attachment/POS-Q2FY2026.pdf"></iframe>
</body></html>"""
found = parse_attachment_page(DETAIL)
check("the PDF is found", len(found) == 1, str(found))
check("and the same file linked twice is not queued twice", len(found) == 1)
check("non-PDF links are ignored",
      found and found[0].url.endswith("POS-Q2FY2026.pdf"), str(found))
check("a page with no PDF returns empty rather than raising",
      parse_attachment_page("<html><body>text only</body></html>") == ())
check("junk input does not raise", parse_attachment_page(None) == ())

print("\nparser: the REAL empty response (envelope confirmed live)")
# Captured 2026-08-16 from the live endpoint. It came back empty, so this
# confirms the envelope -- 'data' really is the announcement array -- but not
# the row shape. See tests/fixtures/bursa/README.md.
real = fixture("announcements_empty_live.json")
check("real response parses without error", parse_json(real) == [], repr(real[:80]))
check("and is not mistaken for a broken endpoint",
      json.loads(real).get("data") == [] and "recordsTotal" in json.loads(real),
      "an empty window must stay distinguishable from a parser failure")

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

# ============================ client conduct ================================
# Imported last, deliberately, so the checks above prove the parsing and dedup
# layers never pull in the networking module. Nothing here makes a request:
# robots reading is stubbed to fail, which is the path being tested.
from bursa.client import (BlockedError, BursaClient, BursaClientError,  # noqa: E402
                          HostBudgetExceeded, RequestBudgetExceeded,
                          _safe_cache_name)
import io  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

REAL_ROBOTS = open(os.path.join(FIXTURES, "robots.txt"), "rb").read()


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _stub_urlopen(result):
    """Stand in for urllib.request.urlopen, recording the request it was given."""
    seen = []

    def opener(request, timeout=None):
        seen.append(request)
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    return opener, seen


print("\nrobots.txt is requested with OUR user-agent, not urllib's default")
# This is the bug that made a live --dry-run report the whole site as
# disallowed. RobotFileParser.read() fetches robots.txt with Python's default
# User-Agent; a site that rejects unidentified clients answers 403, and read()
# turns that into disallow_all. Bursa's robots.txt actually permits the
# endpoints we use, so the monitor was banning itself.
_real_urlopen = urllib.request.urlopen
opener, seen = _stub_urlopen(REAL_ROBOTS)
urllib.request.urlopen = opener
try:
    client = BursaClient()
    allowed = client._robots_allows("https://www.bursamalaysia.com/api/v1/announcements/search")
    check("the real Bursa rules permit the announcements endpoint", allowed,
          "robots.txt only disallows /locomotive/* and a subscribe path")
    check("robots.txt was requested once", len(seen) == 1, str(len(seen)))
    ua = seen[0].get_header("User-agent") or seen[0].get_header("User-Agent")
    check("and it carried our identifying user-agent",
          ua and "EarningsBrief" in ua, repr(ua))
    check("a disallowed path is still refused",
          not client._robots_allows("https://www.bursamalaysia.com/locomotive/x"))
finally:
    urllib.request.urlopen = _real_urlopen

print("\na 403 on robots.txt itself is treated as a closed door")
opener, seen = _stub_urlopen(
    urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
urllib.request.urlopen = opener
try:
    client = BursaClient()
    blocked = False
    try:
        client._robots_allows("https://www.bursamalaysia.com/anything")
    except BlockedError:
        blocked = True
    check("BlockedError is raised, so the run stops", blocked)
finally:
    urllib.request.urlopen = _real_urlopen

print("\nno robots.txt published means no restrictions")
opener, seen = _stub_urlopen(urllib.error.HTTPError("u", 404, "Not Found", {}, None))
urllib.request.urlopen = opener
try:
    check("a 404 allows fetching",
          BursaClient()._robots_allows("https://www.bursamalaysia.com/anything"))
finally:
    urllib.request.urlopen = _real_urlopen

print("\na stated crawl-delay overrides our own pacing when it is slower")
opener, seen = _stub_urlopen(b"User-agent: *\nCrawl-delay: 9\n")
urllib.request.urlopen = opener
try:
    client = BursaClient()
    client._robots_allows("https://www.bursamalaysia.com/x")
    check("we slow down to the site's stated delay", client.min_delay == 9.0,
          str(client.min_delay))
finally:
    urllib.request.urlopen = _real_urlopen

print("\nclient refuses to fetch when robots.txt cannot be read, and only asks once")

opener, seen = _stub_urlopen(urllib.error.URLError("network down"))
urllib.request.urlopen = opener
try:
    client = BursaClient(cache_dir=None)
    refusals = 0
    for _ in range(5):
        try:
            client.fetch("/api/v1/announcements/search")
        except BursaClientError:
            refusals += 1
    check("every attempt is refused", refusals == 5, str(refusals))
    check("robots.txt is read once, not once per attempt",
          len(seen) == 1, f"{len(seen)} reads — a network blip would hammer robots.txt")
    check("no request was counted against the budget", client.requests_made == 0,
          str(client.requests_made))
finally:
    urllib.request.urlopen = _real_urlopen

print("\nclient will not be configured to be rude")
check("delay floor cannot be lowered", BursaClient(min_delay=0).min_delay >= 2.0,
      str(BursaClient(min_delay=0).min_delay))
check("request cap cannot be raised", BursaClient(max_requests=10_000).max_requests <= 30,
      str(BursaClient(max_requests=10_000).max_requests))
check("user agent identifies the tool and a contact",
      "EarningsBrief" in BursaClient().user_agent and "@" in BursaClient().user_agent,
      BursaClient().user_agent)

# ---- one client, more than one host ----------------------------------------
# robots.txt is defined per host, but the client held ONE parser, one crawl
# delay and one clock. Python's RobotFileParser cannot notice: can_fetch()
# rebuilds the URL as urlunparse(('', '', path, params, query, fragment)) — the
# scheme and hostname are discarded — so a parser loaded from Bursa answers
# confidently, and permissively, about anybody else's site.
#
# Every host below is under .example (RFC 2606: never resolvable) and urlopen is
# stubbed, so nothing here can reach a network even if the guard were removed.
ALPHA = "https://ir.alpha.example"
BETA = "https://ir.beta.example"


def _stub_router(routes):
    """urlopen stand-in that answers by URL. Anything not declared 404s, so a
    test can only pass by asking for exactly what it said it would."""
    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        result = routes.get(request.full_url)
        if result is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    return opener, seen


print("\neach host is judged by its OWN robots.txt")
opener, seen = _stub_router({
    ALPHA + "/robots.txt": b"User-agent: *\nDisallow: /private/\nCrawl-delay: 5\n",
    BETA + "/robots.txt": b"User-agent: *\nDisallow: /reports/\n",
})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    check("alpha permits /reports/, and alpha's rules are what decide it",
          client._robots_allows(ALPHA + "/reports/q2.pdf"))
    check("beta's own Disallow is honoured on beta",
          not client._robots_allows(BETA + "/reports/q2.pdf"),
          "the regression: alpha's parser answered for beta, and answered yes")
    check("alpha's Disallow does not travel to beta",
          client._robots_allows(BETA + "/private/q2.pdf"))
    check("  while it still binds on alpha",
          not client._robots_allows(ALPHA + "/private/q2.pdf"))
    check("each host's rules were read from that host, exactly once",
          seen == [ALPHA + "/robots.txt", BETA + "/robots.txt"], str(seen))
finally:
    urllib.request.urlopen = _real_urlopen

print("\na robots failure on one host does not silence the others")
opener, seen = _stub_router({
    ALPHA + "/robots.txt": urllib.error.URLError("alpha is down"),
    BETA + "/robots.txt": b"User-agent: *\nDisallow: /private/\n",
})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    refusals = 0
    for _ in range(4):
        try:
            client._robots_allows(ALPHA + "/x.pdf")
        except BursaClientError:
            refusals += 1
    check("alpha is refused while its rules are unreadable", refusals == 4, str(refusals))
    check("and the refusal is sticky for alpha — robots.txt is read once",
          seen.count(ALPHA + "/robots.txt") == 1, str(seen))
    check("but beta is still judged on its own rules",
          client._robots_allows(BETA + "/reports/q2.pdf"))
    check("  including beta's own Disallow",
          not client._robots_allows(BETA + "/private/q2.pdf"))
finally:
    urllib.request.urlopen = _real_urlopen

print("\ncrawl-delay is per host, and can only slow us down")
opener, seen = _stub_router({
    ALPHA + "/robots.txt": b"User-agent: *\nCrawl-delay: 5\n",
    BETA + "/robots.txt": b"User-agent: *\nCrawl-delay: 0.1\n",
})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    client._robots_allows(ALPHA + "/x")
    client._robots_allows(BETA + "/x")
    check("alpha's stated 5s is taken", client.delay_for(ALPHA + "/x") == 5.0,
          str(client.delay_for(ALPHA + "/x")))
    check("beta cannot talk us below our own 2s floor",
          client.delay_for(BETA + "/x") == 2.0, str(client.delay_for(BETA + "/x")))
    check("and alpha's slower pace did not leak onto beta either",
          client.delay_for(BETA + "/x") != 5.0, str(client.delay_for(BETA + "/x")))
    check("min_delay still reports the base host's pace, as poll_bursa prints it",
          client.min_delay == 5.0, str(client.min_delay))
finally:
    urllib.request.urlopen = _real_urlopen

print("\nan unreasonable crawl-delay skips the host rather than being shortened")
opener, seen = _stub_router({ALPHA + "/robots.txt": b"User-agent: *\nCrawl-delay: 3600\n"})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    raised = None
    try:
        client._robots_allows(ALPHA + "/x")
    except BursaClientError as exc:
        raised = exc
    check("the host is refused, and the message says why",
          raised is not None and "3600" in str(raised), repr(raised))
    check("we never answer an inconvenient request by going faster",
          client.delay_for(ALPHA + "/x") >= 2.0, str(client.delay_for(ALPHA + "/x")))
finally:
    urllib.request.urlopen = _real_urlopen

print("\nwaiting on one host does not delay a request to another")
opener, seen = _stub_router({
    ALPHA + "/robots.txt": b"User-agent: *\nCrawl-delay: 5\n",
    BETA + "/robots.txt": b"User-agent: *\n",
    ALPHA + "/a1": b"a1", ALPHA + "/a2": b"a2", BETA + "/b1": b"b1",
})
slept = []
_real_sleep = time.sleep
urllib.request.urlopen = opener
time.sleep = slept.append          # record the gap instead of living through it
try:
    client = BursaClient(base_url=ALPHA)
    client.fetch(ALPHA + "/a1")
    check("the first alpha fetch waits alpha's 5s (its robots.txt was a request too)",
          len(slept) == 1 and 4.0 < slept[0] <= 5.0, str(slept))
    client.fetch(BETA + "/b1")
    check("beta then waits its OWN 2s floor, not alpha's 5s",
          len(slept) == 2 and 1.0 < slept[1] <= 2.0, str(slept))
    client.fetch(ALPHA + "/a2")
    check("and alpha's next turn is alpha's 5s again",
          len(slept) == 3 and 4.0 < slept[2] <= 5.0, str(slept))
    check("no request ever waited on a host other than its own",
          len(slept) == 3, str(slept))

    # Isolate pacing from the robots read, so the "first request waits for
    # nobody" rule is visible on its own.
    slept.clear()
    quick = BursaClient(base_url=ALPHA, respect_robots=False)
    quick.fetch(ALPHA + "/a1")
    check("a host's very first request waits for nobody", not slept, str(slept))
    quick.fetch(BETA + "/b1")
    check("and a gap owed to alpha does not hold beta up at all", not slept, str(slept))
    quick.fetch(ALPHA + "/a2")
    check("while alpha's own second request does wait", len(slept) == 1, str(slept))
finally:
    time.sleep = _real_sleep
    urllib.request.urlopen = _real_urlopen

print("\nthe request budget is accounted per host, under a run-wide ceiling")
opener, seen = _stub_router({
    ALPHA + "/robots.txt": b"User-agent: *\n",
    BETA + "/robots.txt": b"User-agent: *\n",
    ALPHA + "/a1": b"a1", ALPHA + "/a2": b"a2", BETA + "/b1": b"b1",
})
_real_sleep = time.sleep
urllib.request.urlopen = opener
time.sleep = lambda seconds: None
try:
    client = BursaClient(base_url=ALPHA, max_requests_per_host=1)
    client.fetch(ALPHA + "/a1")
    raised = None
    try:
        client.fetch(ALPHA + "/a2")
    except RequestBudgetExceeded as exc:
        raised = exc
    check("a host that spends its share is cut off",
          isinstance(raised, HostBudgetExceeded), repr(raised))
    check("but the other host is still served — no starvation",
          client.fetch(BETA + "/b1") == b"b1")
    check("the narrow type is still a RequestBudgetExceeded, so pipeline.py "
          "keeps catching it", issubclass(HostBudgetExceeded, RequestBudgetExceeded))
    check("the run-wide counter still counts every host",
          client.requests_made == 2, str(client.requests_made))
    check("  and the spend is attributable per host",
          client.requests_made_by_host == {ALPHA: 1, BETA: 1},
          str(client.requests_made_by_host))

    capped = BursaClient(base_url=ALPHA, max_requests=1)
    capped.fetch(ALPHA + "/a1")
    raised = None
    try:
        capped.fetch(BETA + "/b1")
    except RequestBudgetExceeded as exc:
        raised = exc
    check("the run-wide ceiling still stops a run that spreads across hosts",
          isinstance(raised, RequestBudgetExceeded), repr(raised))
    check("  and it is the global cap that fired, not a per-host one",
          not isinstance(raised, HostBudgetExceeded), repr(raised))
finally:
    time.sleep = _real_sleep
    urllib.request.urlopen = _real_urlopen

print("\nsaved responses cannot overwrite each other across hosts")
# The old name sanitised the whole URL and kept the last 120 characters — and
# the host is at the front, i.e. the first thing that truncation dropped.
LONG_PATH = "/market_information/announcements/company_announcement/" + "q" * 140 + ".pdf"
check("two hosts serving the same long path get different names",
      _safe_cache_name(ALPHA + LONG_PATH) != _safe_cache_name(BETA + LONG_PATH),
      _safe_cache_name(ALPHA + LONG_PATH))
check("the host comes first, so the folder stays greppable",
      _safe_cache_name(ALPHA + LONG_PATH).startswith("ir.alpha.example"),
      _safe_cache_name(ALPHA + LONG_PATH))
check("the name stays a usable filename length",
      len(_safe_cache_name(ALPHA + LONG_PATH)) <= 135,
      str(len(_safe_cache_name(ALPHA + LONG_PATH))))
check("the same URL always names the same file, so a re-run overwrites its own",
      _safe_cache_name(ALPHA + LONG_PATH) == _safe_cache_name(ALPHA + LONG_PATH))

print("\nthe host key is normalised, and a non-http link is refused outright")
opener, seen = _stub_router({ALPHA + "/robots.txt": b"User-agent: *\nDisallow: /private/\n"})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    client._robots_allows(ALPHA + "/x")
    client._robots_allows("https://IR.ALPHA.EXAMPLE/x")
    client._robots_allows("https://ir.alpha.example:443/x")
    check("case and the default port do not fork the state or double the traffic",
          len(seen) == 1, str(seen))
finally:
    urllib.request.urlopen = _real_urlopen

GAMMA_HTTPS = "https://ir.gamma.example"
GAMMA_HTTP = "http://ir.gamma.example"
opener, seen = _stub_router({
    GAMMA_HTTPS + "/robots.txt": b"User-agent: *\n",
    GAMMA_HTTP + "/robots.txt": b"User-agent: *\nDisallow: /\n",
})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=GAMMA_HTTPS)
    check("https is judged by the https robots.txt",
          client._robots_allows(GAMMA_HTTPS + "/reports/q.pdf"))
    check("and http by its own, stricter document — the scheme is part of the key",
          not client._robots_allows(GAMMA_HTTP + "/reports/q.pdf"))
    check("so both documents were fetched, one each",
          sorted(seen) == sorted([GAMMA_HTTPS + "/robots.txt", GAMMA_HTTP + "/robots.txt"]),
          str(seen))
finally:
    urllib.request.urlopen = _real_urlopen

opener, seen = _stub_router({})
urllib.request.urlopen = opener
try:
    client = BursaClient(base_url=ALPHA)
    refusals = 0
    for hostile in ("file:///C:/Windows/win.ini", "ftp://ir.alpha.example/x.pdf"):
        try:
            client.fetch(hostile)
        except BursaClientError:
            refusals += 1
    check("a link that is not http(s) is refused, never handed to urlopen",
          refusals == 2, str(refusals))
    check("  and nothing was requested at all", seen == [], str(seen))
finally:
    urllib.request.urlopen = _real_urlopen

check("still no socket opened", not _sockets_opened, str(_sockets_opened))

# ============================ prohibitions ==================================
# These are product constraints, not style preferences, so they are asserted
# rather than trusted. See CLAUDE.md.
print("\nprohibited sources and techniques stay absent from the code")
import re as _re  # noqa: E402

FORBIDDEN = {
    "KLSE Screener as a source": _re.compile(r"klse[\s_.-]*screener", _re.I),
    "proxy rotation": _re.compile(r"proxies\s*=|proxy_pool|rotating_proxy", _re.I),
    "user-agent rotation": _re.compile(r"USER_AGENTS\s*=|random\.choice\([^)]*agent", _re.I),
    "stealth browser automation": _re.compile(r"undetected[_-]?chrome|cloudscraper|selenium", _re.I),
    "captcha solving": _re.compile(r"captcha[_-]?solv|2captcha|anticaptcha", _re.I),
}

sources = []
for folder in (BASE, os.path.join(BASE, "bursa")):
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            sources.append(os.path.join(folder, name))

for label, pattern in FORBIDDEN.items():
    hits = []
    for path in sources:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                # The prohibition may be *named* in a comment; only flag code.
                if pattern.search(line) and not line.lstrip().startswith(("#", '"', "'")):
                    hits.append(f"{os.path.basename(path)}:{lineno}")
    check(f"no {label}", not hits, str(hits))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

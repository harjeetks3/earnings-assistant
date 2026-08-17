"""Evidence capture: the model proposes a source quote, the code confirms it.

    python test_evidence.py

Plain asserts, no runner, no API calls. The case that matters most is the
fabricated quote — a citation is exactly the kind of thing an LLM produces
fluently and wrongly, so a quote that is not in the document must be recorded
as unverified rather than accepted.
"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.pop("OPENAI_API_KEY", None)

import app  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


SAMPLE = os.path.join(BASE, "test_data", "01_golden_NorthPeak_Analytics_Q1_FY2026.pdf")
pages = app.extract_pdf_pages(SAMPLE)
pdf_text = app.PAGE_SEPARATOR.join(pages)

BASE_ANALYSIS = {
    "currency": "USD", "unit_raw": "US$ '000",
    "revenue_current": 48.2, "revenue_previous_quarter": 45.1,
    "revenue_same_quarter_last_year": 38.5, "pbt_current": 9.6,
    "pbt_previous_quarter": 8.2, "pbt_same_quarter_last_year": 6.1,
}


def evidence_for(analysis):
    return {r["metric"]: r for r in app.build_evidence(analysis, pdf_text, pages)}


# --- a genuine quote is accepted ------------------------------------------
print("a verbatim quote is verified and located:")
# Take a real line out of the document so the quote is guaranteed genuine.
# Note the table cell itself extracts as a bare " 48,200" on its own line —
# pypdf splits table columns — so pick the longest genuine line mentioning the
# figure, which is what a model quoting its source would realistically return.
real_line = max((ln.strip() for ln in pdf_text.splitlines()
                 if "48,200" in ln or "48.2" in ln), key=len)
assert len(real_line) > 20, f"fixture line too short to be a meaningful quote: {real_line!r}"
records = evidence_for({**BASE_ANALYSIS, "evidence": {"revenue_current": real_line}})
rec = records["revenue_current"]
check("marked llm_verified", rec["match_method"] == app.MATCH_LLM_VERIFIED, rec["match_method"])
check("verified flag set", rec["verified"] == 1)
check("character span recorded", rec["char_start"] is not None and rec["char_end"] > rec["char_start"])
check("span actually points at the quote",
      pdf_text[rec["char_start"]:rec["char_end"]].strip() == real_line, rec["printed_form"])
check("page number resolved", rec["page_number"] in (1, 2), repr(rec["page_number"]))
check("snippet carries surrounding context",
      rec["snippet"] and len(rec["snippet"]) >= len(real_line), repr(rec["snippet"])[:80])

print("\nwhitespace differences are tolerated, paraphrase is not:")
spaced = real_line.replace(" ", "   ")
rec = evidence_for({**BASE_ANALYSIS, "evidence": {"revenue_current": spaced}})["revenue_current"]
check("re-spaced quote still verifies", rec["match_method"] == app.MATCH_LLM_VERIFIED,
      rec["match_method"])

# --- the important one: a fabricated quote ---------------------------------
print("\na fabricated quote is NOT accepted:")
fake = "Total group revenue for the quarter amounted to 48.2 million dollars"
check("the fabricated line is genuinely absent from the document",
      fake not in pdf_text)
rec = evidence_for({**BASE_ANALYSIS, "evidence": {"revenue_current": fake}})["revenue_current"]
check("not marked llm_verified", rec["match_method"] != app.MATCH_LLM_VERIFIED,
      rec["match_method"])
check("the fabricated text is not stored as provenance",
      (rec["printed_form"] or "") != fake, repr(rec["printed_form"]))

print("\nwith no quote at all, the code locates the figure itself:")
rec = evidence_for({**BASE_ANALYSIS, "evidence": {}})["revenue_current"]
check("falls back to a deterministic match",
      rec["match_method"] == app.MATCH_DETERMINISTIC, rec["match_method"])
check("found the printed form, not the normalised value",
      rec["printed_form"] == "48,200", repr(rec["printed_form"]))
check("still records a page", rec["page_number"] is not None)

print("\nan untraceable figure is recorded as unverified, not dropped:")
records = evidence_for({**BASE_ANALYSIS, "revenue_current": 987654.0,
                        "evidence": {"revenue_current": "no such line anywhere"}})
rec = records["revenue_current"]
check("a record still exists for the field", rec is not None)
check("marked unverified", rec["match_method"] == app.MATCH_UNVERIFIED, rec["match_method"])
check("verified flag clear", rec["verified"] == 0)
check("no span invented", rec["char_start"] is None and rec["snippet"] is None)

print("\nonly fields with values produce records:")
records = evidence_for({**BASE_ANALYSIS, "pbt_current": None, "evidence": {}})
check("null field produces no record", "pbt_current" not in records)
check("populated fields still do", len(records) == 5, str(sorted(records)))

print("\na malformed evidence block from the model does not crash extraction:")
for bad in ("not a dict", ["a", "list"], None, 42):
    try:
        app.build_evidence({**BASE_ANALYSIS, "evidence": bad}, pdf_text, pages)
        ok = True
    except Exception as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    check(f"evidence={bad!r} handled", ok, "" if ok else detail)

print("\nthe reviewer is told when figures could not be traced:")
untraced = [{"metric": "pbt_current", "match_method": app.MATCH_UNVERIFIED, "verified": 0},
            {"metric": "revenue_current", "match_method": app.MATCH_LLM_VERIFIED, "verified": 1}]
warning = app.evidence_summary_warning(untraced)
check("a warning is produced", len(warning) == 1, str(warning))
check("it names the untraced field", "pbt_current" in warning[0], warning[0])
check("no warning when everything traced",
      app.evidence_summary_warning([untraced[1]]) == [])
check("the warning survives the approve path",
      app._SOURCE_ONLY_WARNING_RE.match(warning[0]) is not None,
      "approve() recomputes warnings without the PDF text, so it must carry this forward")

# --- a real but mis-keyed quote --------------------------------------------
# Found by adversarial review: locate_quote only asked whether the sentence
# exists. A genuine line of the document quoted for the wrong metric was
# stamped llm_verified and printed under "As printed in the source".
print("\na genuine quote that does not contain its figure is not accepted for it:")
boilerplate = next(ln.strip() for ln in pdf_text.splitlines()
                   if "today reported" in ln.lower() or "Analytics" in ln)
check("the line really is in the document", boilerplate in pdf_text)
rec = evidence_for({**BASE_ANALYSIS, "evidence": {"pbt_current": boilerplate}})["pbt_current"]
check("not certified as the model's verified source",
      rec["match_method"] != app.MATCH_LLM_VERIFIED, rec["match_method"])
check("the unrelated sentence is not stored as the printed figure",
      (rec["printed_form"] or "") != boilerplate, repr(rec["printed_form"]))

print("\nboth ways a document states a figure count as a citation:")
table_row = next(ln.strip() for ln in pdf_text.splitlines() if "48,200" in ln)
rec = evidence_for({**BASE_ANALYSIS, "evidence": {"revenue_current": table_row}})["revenue_current"]
check("the as-printed table form verifies", rec["match_method"] == app.MATCH_LLM_VERIFIED,
      rec["match_method"])
narrative = max((ln.strip() for ln in pdf_text.splitlines() if "48.2" in ln), key=len)
rec = evidence_for({**BASE_ANALYSIS, "evidence": {"revenue_current": narrative}})["revenue_current"]
check("the already-scaled narrative form verifies too",
      rec["match_method"] == app.MATCH_LLM_VERIFIED, rec["match_method"])

# --- coincidental digit matches --------------------------------------------
print("\nthe deterministic fallback refuses a coincidental match:")
# 6.9 appears in this document only as a growth percentage. Certifying that as
# the source of a monetary figure would be a fabricated citation.
rec = evidence_for({**BASE_ANALYSIS, "pbt_current": 0.0069, "evidence": {}})["pbt_current"]
check("a percentage is not accepted as a figure's source",
      rec["match_method"] == app.MATCH_UNVERIFIED, rec["match_method"])

print("\nthe deterministic fallback cites the line with the right caption:")
# Lindqvist's income statement reads "Gross profit 372 349 316 / Selling
# expenses (121) (118) (110) / ... / Profit before tax 148 132 121". The value
# 121 appears under two different captions, and taking the first match cited
# selling expenses as the source of a profit-before-tax figure.
LINDQVIST = os.path.join(BASE, "test_data", "02_golden_Lindqvist_Industrial_Q4_FY2025.pdf")
lq_pages = app.extract_pdf_pages(LINDQVIST)
lq_text = app.PAGE_SEPARATOR.join(lq_pages)
lq = {
    "currency": "SEK", "unit_raw": "SEK million",
    "revenue_current": 1240.0, "pbt_current": 148.0,
    "pbt_same_quarter_last_year": 121.0,
    "evidence": {},
}
lq_records = {r["metric"]: r for r in app.build_evidence(lq, lq_text, lq_pages)}

lq_flat = " ".join(lq_text.split())
check("121 really does appear under two captions",
      "Selling expenses (121)" in lq_flat and "Profit before tax 148 132 121" in lq_flat,
      repr([s for s in lq_flat.split(" Cost")[:1]])[:80])
rec = lq_records["pbt_same_quarter_last_year"]
cited = lq_text[max(0, rec["char_start"] - 60):rec["char_end"]]
check("it is cited to the profit-before-tax row",
      "before tax" in cited.lower(), repr(cited[-70:]))
check("not to selling expenses",
      "selling expenses" not in cited.lower(), repr(cited[-70:]))

rec = lq_records["revenue_current"]
cited = lq_text[max(0, rec["char_start"] - 60):rec["char_end"]]
check("revenue is cited to a revenue/net-sales line",
      any(w in cited.lower() for w in ("net sales", "revenue", "turnover")),
      repr(cited[-70:]))

print("\nfigures from a previously approved entry are labelled, not cited:")
records = {r["metric"]: r for r in app.build_evidence(
    {**BASE_ANALYSIS, "evidence": {"revenue_previous_quarter": real_line}},
    pdf_text, pages,
    document_sourced={"revenue_current"})}
rec = records["revenue_previous_quarter"]
check("marked as carried from a prior entry",
      rec["match_method"] == app.MATCH_PRIOR_ENTRY, rec["match_method"])
check("no page citation into this document", rec["page_number"] is None)
check("and it says where it came from",
      "previously approved" in (rec["snippet"] or ""), repr(rec["snippet"]))
check("document-sourced fields are still traced normally",
      records["revenue_current"]["match_method"] in
      (app.MATCH_LLM_VERIFIED, app.MATCH_DETERMINISTIC),
      records["revenue_current"]["match_method"])

# --- unusable model responses ----------------------------------------------
# Asking for evidence quotes makes responses longer, so running into the
# model's own output limit is more likely. A cut-off response must say so
# rather than surfacing as an opaque JSON decode error.
print("\nan unusable model response is diagnosed clearly:")


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content, finish_reason):
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, content, finish_reason):
        self.choices = [_FakeChoice(content, finish_reason)]


def _fake_openai(content, finish_reason):
    class _Completions:
        def create(self, **kw):
            return _FakeResponse(content, finish_reason)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return lambda **kw: _Client()


_real_openai, _real_available = app._OpenAI, app._openai_available
app._openai_available = True
os.environ["OPENAI_API_KEY"] = "test-key-not-used-for-a-real-call"

app._OpenAI = _fake_openai('{"company_name": "Half a resp', "length")
result = app.analyse_earnings("some text")
check("truncated response reports the truncation",
      "cut off" in (result.get("analysis_error") or ""), repr(result)[:160])
check("  and does not surface as a JSON decode error",
      "Expecting" not in (result.get("analysis_error") or ""), repr(result)[:160])

app._OpenAI = _fake_openai(None, "content_filter")
result = app.analyse_earnings("some text")
check("empty response reports emptiness",
      "empty response" in (result.get("analysis_error") or ""), repr(result)[:160])
check("  and names the finish reason",
      "content_filter" in (result.get("analysis_error") or ""), repr(result)[:160])

app._OpenAI = _fake_openai("this is not json at all", "stop")
result = app.analyse_earnings("some text")
check("malformed JSON reports what came back",
      "not valid JSON" in (result.get("analysis_error") or "")
      and "not json at all" in (result.get("analysis_error") or ""), repr(result)[:200])

app._OpenAI = _fake_openai('{"company_name": "Fine Corp"}', "stop")
result = app.analyse_earnings("some text")
check("a good response still parses", result.get("company_name") == "Fine Corp", repr(result))

app._OpenAI, app._openai_available = _real_openai, _real_available
os.environ.pop("OPENAI_API_KEY", None)

# --- persistence -----------------------------------------------------------
print("\nprovenance is persisted and survives approval:")
ws = tempfile.mkdtemp(prefix="evidence_test_")
app.UPLOAD_FOLDER = os.path.join(ws, "uploads")
app.ATTACHMENTS_FOLDER = os.path.join(ws, "attachments")
app.REPORTS_FOLDER = os.path.join(ws, "reports")
app.DB_PATH = os.path.join(ws, "t.db")
app.WATCHLIST_PATH = os.path.join(ws, "absent.json")
for d in (app.UPLOAD_FOLDER, app.ATTACHMENTS_FOLDER, app.REPORTS_FOLDER):
    os.makedirs(d, exist_ok=True)
app.init_db()

app.analyse_earnings = lambda text, **kw: {
    "company_name": "NorthPeak Analytics, Inc.", "quarter_end_date": "2026-03-31",
    "fiscal_quarter": "Q1", "fiscal_year": 2026, "confidence_score": 0.95,
    "management_commentary": "s", "outlook_summary": "s",
    **BASE_ANALYSIS,
    "evidence": {"revenue_current": real_line},
}

with app.app.app_context():
    db = app.get_db()
    result = app.ingest_pdf_bytes(db, open(SAMPLE, "rb").read(), "north.pdf")
    pending_id = result["id"]

    rows = db.execute(
        """SELECT o.metric, o.value_millions, o.pending_review_id, o.pdf_metadata_id,
                  e.match_method, e.verified, e.page_number
             FROM metric_observations o JOIN evidence e
               ON e.metric_observation_id = o.id"""
    ).fetchall()
    # Four, not six: previous-quarter values are DB-only by design, so packaging
    # drops them on an empty database. Evidence must describe the figures the
    # RECORD holds — tracing the model's raw output would cite a source line for
    # a figure the report itself shows as "—".
    traced_metrics = sorted(r["metric"] for r in rows)
    check("an observation for each figure the record actually holds",
          traced_metrics == ["pbt_current", "pbt_same_quarter_last_year",
                             "revenue_current", "revenue_same_quarter_last_year"],
          str(traced_metrics))
    check("no evidence for figures dropped during packaging",
          not any(m.endswith("previous_quarter") for m in traced_metrics),
          "the traceability table would contradict the financial summary")
    check("all linked to the pending review",
          all(r["pending_review_id"] == pending_id for r in rows))
    check("none linked to an approved entry yet",
          all(r["pdf_metadata_id"] is None for r in rows))
    check("the quoted figure is llm_verified",
          any(r["metric"] == "revenue_current" and r["match_method"] == app.MATCH_LLM_VERIFIED
              for r in rows), str([tuple(r) for r in rows])[:200])
    check("the unquoted ones fell back to deterministic",
          sum(1 for r in rows if r["match_method"] == app.MATCH_DETERMINISTIC)
          == len(rows) - 1,
          str([r["match_method"] for r in rows]))

    client = app.app.test_client()
    client.get(f"/pending/{pending_id}/report")
    approved = client.post(f"/pending/{pending_id}/approve").get_json()

    rows = db.execute(
        "SELECT metric, pdf_metadata_id, pending_review_id FROM metric_observations"
    ).fetchall()
    check("observations survived the pending row being deleted",
          len(rows) == len(traced_metrics), str(len(rows)))
    check("and now point at the approved entry",
          all(r["pdf_metadata_id"] == approved["id"] for r in rows),
          str([tuple(r) for r in rows])[:200])
    check("every approved monetary figure has evidence",
          db.execute("""SELECT COUNT(*) FROM metric_observations o
                         WHERE o.pdf_metadata_id = ?
                           AND NOT EXISTS (SELECT 1 FROM evidence e
                                            WHERE e.metric_observation_id = o.id)""",
                     (approved["id"],)).fetchone()[0] == 0)

shutil.rmtree(ws, ignore_errors=True)


def fresh_workspace():
    """A throwaway database and folder tree, with app pointed at them.

    Each section below needs a database holding no approved entries, so that
    "pdf_metadata is still empty" means the gate held rather than that this
    section simply had not got round to approving anything yet.
    """
    path = tempfile.mkdtemp(prefix="evidence_test_")
    app.UPLOAD_FOLDER = os.path.join(path, "uploads")
    app.ATTACHMENTS_FOLDER = os.path.join(path, "attachments")
    app.REPORTS_FOLDER = os.path.join(path, "reports")
    app.DB_PATH = os.path.join(path, "t.db")
    app.WATCHLIST_PATH = os.path.join(path, "absent.json")
    for folder in (app.UPLOAD_FOLDER, app.ATTACHMENTS_FOLDER, app.REPORTS_FOLDER):
        os.makedirs(folder, exist_ok=True)
    app.init_db()
    return path


def approved_count(conn):
    return conn.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0]


def pending_count(conn, pending_id):
    return conn.execute("SELECT COUNT(*) FROM pending_reviews WHERE id = ?",
                        (pending_id,)).fetchone()[0]


# --- the review gate, asserted in its failing direction ---------------------
# Every other approval in this suite downloads the report first, so the gate is
# only ever exercised where it lets the request through. That makes "nothing
# reached pdf_metadata" a statement about this run rather than about the rule:
# deleting the downloaded_at check would leave every one of those checks
# passing. These assert the refusal itself.
print("\nno figure reaches the record of account before a human reads the draft:")
ws = fresh_workspace()
with app.app.app_context():
    db = app.get_db()
    pending_id = app.ingest_pdf_bytes(db, open(SAMPLE, "rb").read(), "gate.pdf")["id"]
    client = app.app.test_client()

    def is_armed():
        # Tolerates the row having vanished: if the gate ever stops holding, the
        # approval goes through and deletes it, and that must surface as a failed
        # check rather than as a crash that hides the rest of the section.
        r = db.execute("SELECT downloaded_at FROM pending_reviews WHERE id = ?",
                       (pending_id,)).fetchone()
        return bool(r and r["downloaded_at"])

    resp = client.post(f"/pending/{pending_id}/approve")
    body = resp.get_json() or {}
    check("approving an undownloaded review is refused", resp.status_code == 400,
          f"{resp.status_code} {str(body)[:120]}")
    check("  and the refusal names the reason", body.get("error") == "not_reviewed", str(body)[:120])
    check("  nothing was written to the record of account", approved_count(db) == 0)
    check("  and the review is still there to be reviewed",
          pending_count(db, pending_id) == 1)

    # The same gate guards the reject path, which spends money on a rerun.
    resp = client.post(f"/pending/{pending_id}/reject", json={})
    body = resp.get_json() or {}
    check("failing an undownloaded review is refused too", resp.status_code == 400,
          f"{resp.status_code} {str(body)[:120]}")
    check("  for the same stated reason", body.get("error") == "not_reviewed", str(body)[:120])

    print("\nand a rerun re-arms the gate, so the NEW draft must be read as well:")
    client.get(f"/pending/{pending_id}/report")
    check("downloading the draft arms the approval", is_armed())

    resp = client.post(f"/pending/{pending_id}/reject", json={"instructions": "check the tax line"})
    check("the rerun ran", resp.status_code == 200, f"{resp.status_code} {str(resp.get_json())[:120]}")
    check("  and it cleared the download", not is_armed(),
          "the figures changed, so the draft the human read no longer describes them")

    resp = client.post(f"/pending/{pending_id}/approve")
    check("approving the unread rerun is refused", resp.status_code == 400, str(resp.status_code))
    check("  with nothing in the record of account", approved_count(db) == 0)

    client.get(f"/pending/{pending_id}/report")
    resp = client.post(f"/pending/{pending_id}/approve")
    check("approving after reading the new draft succeeds", resp.status_code == 201,
          f"{resp.status_code} {str(resp.get_json())[:120]}")
    check("  and writes exactly one record of account", approved_count(db) == 1)
shutil.rmtree(ws, ignore_errors=True)

# --- a draft that will not delete ------------------------------------------
# The bug _remove_quietly exists to fix: approval committed the record of
# account, then the unlink of the draft raised, turning a successful approval
# into a 500 and skipping the pending-row cleanup underneath it. The filing was
# then approved AND still queued, and approving it again wrote a second record.
# The pending row is now deleted inside the approval transaction, so the second
# half of that cannot recur — but the false 500 still can, and reporting failure
# for a filing that did reach the record of account is what makes a reviewer
# approve it twice. Both halves are asserted below.
print("\na draft that cannot be deleted does not undo the approval it follows:")
ws = fresh_workspace()
with app.app.app_context():
    db = app.get_db()
    pending_id = app.ingest_pdf_bytes(db, open(SAMPLE, "rb").read(), "locked.pdf")["id"]
    client = app.app.test_client()
    client.get(f"/pending/{pending_id}/report")

    # What happens in the field is Windows refusing to unlink a draft whose
    # reader has not let go of it. Holding a real lock is neither portable nor
    # reliable to arrange, so point the row at something os.remove can never
    # remove on any platform — a non-empty directory. The route and the helper
    # it calls are untouched, and os.remove genuinely raises.
    locked = os.path.join(app.REPORTS_FOLDER, f"pending_{pending_id}_held_open.pdf")
    os.makedirs(locked, exist_ok=True)
    with open(os.path.join(locked, "reader.lock"), "w", encoding="utf-8") as f:
        f.write("a reader still has this open")
    db.execute("UPDATE pending_reviews SET report_path = ? WHERE id = ?", (locked, pending_id))
    db.commit()

    try:
        os.remove(locked)
        raised = None
    except OSError as exc:
        raised = exc
    check("the draft really is undeletable", raised is not None,
          "the fixture must genuinely make os.remove fail, or this proves nothing")
    try:
        quietly = app._remove_quietly(locked, "draft report")
    except OSError as exc:
        quietly = f"raised {type(exc).__name__}"
    check("  and the helper reports that instead of raising", quietly is False, repr(quietly))

    resp = client.post(f"/pending/{pending_id}/approve")
    check("the approval still reports success", resp.status_code == 201,
          f"{resp.status_code} {str(resp.get_json())[:120]}")
    check("  the record of account was written exactly once", approved_count(db) == 1)
    check("  the pending row was cleaned up", pending_count(db, pending_id) == 0)
    check("  so the filing cannot be approved a second time",
          client.post(f"/pending/{pending_id}/approve").status_code == 404)
    check("  and the undeletable draft is still on disk, so the unlink did fail",
          os.path.isdir(locked))
shutil.rmtree(ws, ignore_errors=True)

# --- what the evaluation harness records about traceability -----------------
# verified=1 covers both a quote the model got right and a figure the code found
# unaided, so the coverage line alone cannot say which happened. _evaluate_case
# is called directly with the model stubbed: run_evaluation() sends the test
# PDFs to OpenAI and costs money, and none of this needs a real call.
print("\nthe evaluation harness records HOW each figure was traced, not just that it was:")
EVAL_CASE = {
    "category": "golden", "metadata": {}, "pages": None,
    "expected_extraction": {}, "expected_qoq_yoy": {},
    "expected_validation_warnings": [], "conditional_validation_warnings": [],
}
ALL_METHODS = sorted([app.MATCH_LLM_VERIFIED, app.MATCH_DETERMINISTIC,
                      app.MATCH_UNVERIFIED, app.MATCH_PRIOR_ENTRY])


def eval_checks(evidence_block):
    """Run one case through the harness with the model returning `evidence_block`.

    The breakdown row is returned separately, standing in an empty one if the
    harness stopped recording it — that absence is the regression, and it should
    show up as failed checks rather than as a crash that hides the rest.
    """
    app.analyse_earnings = lambda text, **kw: {
        "company_name": "NorthPeak Analytics, Inc.", "quarter_end_date": "2026-03-31",
        "fiscal_quarter": "Q1", "fiscal_year": 2026, "confidence_score": 0.95,
        "management_commentary": "s", "outlook_summary": "s",
        **BASE_ANALYSIS,
        "evidence": evidence_block,
    }
    case = app._evaluate_case(os.path.basename(SAMPLE), EVAL_CASE)
    by_field = {c["field"]: c for c in case["checks"]}
    return by_field, by_field.get("evidence_match_methods", {"actual": {}})


checks, row = eval_checks({"revenue_current": real_line})
check("the harness records a match_method breakdown", "evidence_match_methods" in checks,
      str(sorted(checks)))
check("it is informational and can never fail a case",
      row.get("info_only") is True and row.get("passed") is True, str(row))
check("every method has a key, so a zero is not a missing key",
      sorted(row["actual"]) == ALL_METHODS, str(sorted(row["actual"])))
quoted = row["actual"]
check("the model's own quote is counted as llm_verified",
      quoted.get(app.MATCH_LLM_VERIFIED, 0) >= 1, str(quoted))
check("the figures it did not quote are counted separately as deterministic",
      quoted.get(app.MATCH_DETERMINISTIC, 0) >= 1, str(quoted))
# This path calls build_evidence without document_sourced, so prior_entry is
# structurally zero here — a fixed shape, not a measurement of anything.
check("prior_entry is present but cannot arise on this path",
      quoted.get(app.MATCH_PRIOR_ENTRY) == 0, str(quoted))
check("it agrees with the coverage line it sits beside",
      checks["evidence_coverage"]["actual"].startswith(
          f"{quoted.get(app.MATCH_LLM_VERIFIED, 0) + quoted.get(app.MATCH_DETERMINISTIC, 0)}/"),
      checks["evidence_coverage"]["actual"])
check("and it survives the JSON round-trip run_evaluation writes it through",
      json.loads(json.dumps(row)) == row, str(row)[:120])

# The same document with no quotes at all. If the two methods were not genuinely
# distinguished, this number could not move.
unquoted = eval_checks({})[1]["actual"]
check("with no quote, nothing is credited to the model",
      unquoted.get(app.MATCH_LLM_VERIFIED) == 0, str(unquoted))
check("  the figure moves into the deterministic count instead",
      unquoted.get(app.MATCH_DETERMINISTIC, 0) > quoted.get(app.MATCH_DETERMINISTIC, 0),
      f"{unquoted} vs {quoted}")
check("  and no figure is lost or double-counted between the two",
      sum(unquoted.values()) == sum(quoted.values()) > 0, f"{unquoted} vs {quoted}")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

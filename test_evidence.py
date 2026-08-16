"""Evidence capture: the model proposes a source quote, the code confirms it.

    python test_evidence.py

Plain asserts, no runner, no API calls. The case that matters most is the
fabricated quote — a citation is exactly the kind of thing an LLM produces
fluently and wrongly, so a quote that is not in the document must be recorded
as unverified rather than accepted.
"""
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
    check("one observation per populated figure", len(rows) == 6, str(len(rows)))
    check("all linked to the pending review",
          all(r["pending_review_id"] == pending_id for r in rows))
    check("none linked to an approved entry yet",
          all(r["pdf_metadata_id"] is None for r in rows))
    check("the quoted figure is llm_verified",
          any(r["metric"] == "revenue_current" and r["match_method"] == app.MATCH_LLM_VERIFIED
              for r in rows), str([tuple(r) for r in rows])[:200])
    check("the unquoted ones fell back to deterministic",
          sum(1 for r in rows if r["match_method"] == app.MATCH_DETERMINISTIC) == 5,
          str([r["match_method"] for r in rows]))

    client = app.app.test_client()
    client.get(f"/pending/{pending_id}/report")
    approved = client.post(f"/pending/{pending_id}/approve").get_json()

    rows = db.execute(
        "SELECT metric, pdf_metadata_id, pending_review_id FROM metric_observations"
    ).fetchall()
    check("observations survived the pending row being deleted", len(rows) == 6, str(len(rows)))
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

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")

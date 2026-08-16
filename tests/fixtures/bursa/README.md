# Bursa fixtures

Saved payloads so the whole discovery pipeline can be exercised offline. The
default test suite makes **no live requests** — these files are the only input.

## Provenance — read this before trusting the parser

| File | Source |
|---|---|
| `announcements_objects.json` | **Synthetic.** Records-as-objects shape. |
| `announcements_arrays.json` | **Synthetic.** DataTables positional shape. |
| `announcements_listing.html` | **Synthetic.** HTML listing fallback. |
| `announcements_empty.json` | **Synthetic.** Valid response, no announcements. |
| `announcements_malformed.json` | **Synthetic.** Endpoint-shape-changed case. |

**No real Bursa response has been captured yet.** The field names in
`bursa/parser.py::FIELD_ALIASES` and the positional layout in `COLUMN_ORDER`
are a documented best guess, not observed fact.

Capturing one real response is the single live request in this design. Until
that happens:

- The parser's *structure* is tested — normalisation, both row shapes, the HTML
  fallback, date handling, PDF-link extraction, and the typed failure on an
  unrecognised shape.
- The parser's *field mapping* is unverified.

When you capture a real response, save it here as `announcements_live.json`,
add it to the parser test, and reconcile `FIELD_ALIASES` / `COLUMN_ORDER`
against it. `ParserError` deliberately reports the keys it actually saw, so the
first `poll_bursa.py --dry-run` prints exactly what needs changing.

## Two shapes on purpose

`announcements_objects.json` and `announcements_arrays.json` describe the *same
four announcements*. The parser test asserts they normalise to identical
records — that is what makes the shape genuinely swappable rather than two
code paths that happen to both run.

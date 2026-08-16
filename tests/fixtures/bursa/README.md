# Bursa fixtures

Saved payloads so the whole discovery pipeline can be exercised offline. The
default test suite makes **no live requests** — these files are the only input.

## Provenance — read this before trusting the parser

| File | Source |
|---|---|
| `announcements_empty_live.json` | **REAL.** Captured 2026-08-16 from `/api/v1/announcements/search`. |
| `robots.txt` | **REAL.** Captured 2026-08-16 from the live site. |
| `announcements_objects.json` | **Synthetic.** Records-as-objects shape. |
| `announcements_arrays.json` | **Synthetic.** DataTables positional shape. |
| `announcements_listing.html` | **Synthetic.** HTML listing fallback. |
| `announcements_empty.json` | **Synthetic.** Valid response, no announcements. |
| `announcements_malformed.json` | **Synthetic.** Endpoint-shape-changed case. |

## What the real capture confirmed, and what it did not

The live endpoint returned, verbatim:

```json
{"recordsTotal":0,"recordsFiltered":0,"category_message":"","data":[]}
```

**Confirmed:** the envelope. `data` is the announcement array — it is first in
`bursa/parser.py::_ROOT_KEYS`, so the root-key handling is correct against the
real service, not just against our own fixtures.

**Still unverified:**

- **Row shape and field names.** `data` was empty, so nothing exercised
  `FIELD_ALIASES` or `COLUMN_ORDER`. Those remain a documented best guess.
- **The query parameters that return results.** The bare URL answers 200 with an
  empty set. A guessed parameter set was refused with 403.

## Getting the row shape

Cloudflare sits in front of the site and refuses some automated requests — the
HTML announcements page 403s even though `robots.txt` permits it. Guessing
parameters against a WAF is not something to automate, so the remaining step is
a human one:

1. Open the announcements page in your browser and filter to *Financial Results*.
2. Open DevTools → Network, and find the `search` request the page itself makes.
3. Copy the full URL, and *Copy response*.
4. Save the response here as `announcements_live.json`, put the query string into
   `listing_params` in `poll_bursa.py`, and reconcile `FIELD_ALIASES` /
   `COLUMN_ORDER` against the real rows.

That is you reading your own browser session, which needs no permission from
anyone, and it yields both the correct parameters and the true row shape in one
step. `ParserError` reports the keys it actually saw, so a mismatch tells you
exactly what to change.

## Two shapes on purpose

`announcements_objects.json` and `announcements_arrays.json` describe the *same
four announcements*. The parser test asserts they normalise to identical
records — that is what makes the shape genuinely swappable rather than two
code paths that happen to both run.

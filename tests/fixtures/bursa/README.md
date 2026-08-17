# Bursa fixtures

Saved payloads so the whole discovery pipeline can be exercised offline. The
default test suite makes **no live requests** — these files are the only input.

## Provenance — read this before trusting the parser

| File | Source |
|---|---|
| `announcements_live.json` | **REAL.** Captured 2026-08-16 from the site's own request. Truncated to 6 of 20 rows for size; untouched otherwise. |
| `announcements_results_live.json` | **REAL.** The same endpoint with `cat=FA,FRCO` — the site's own *Financial Results* filter. Also truncated to 6 rows. |
| `announcements_empty_live.json` | **REAL.** The same endpoint with no parameters — an empty result set. |
| `robots.txt` | **REAL.** Captured 2026-08-16. |
| `announcements_objects.json` | **Synthetic.** Records-as-objects shape, the only fixture with attachments on the listing. |
| `announcements_arrays.json` | **Synthetic**, but modelled on the real layout. |
| `announcements_listing.html` | **Synthetic.** HTML fallback; layout inferred, see below. |
| `announcements_empty.json` | **Synthetic.** Valid response, no announcements. |
| `announcements_malformed.json` | **Synthetic.** Endpoint-shape-changed case. |

## The real shape, confirmed

Request the site itself makes:

```
/api/v1/announcements/search?ann_type=company&company=&keyword=&dt_ht=&dt_lt=
  &cat=&sub_type=&mkt=&sec=&subsec=&per_page=20&page=1&_=<cache-buster>
```

Response: `{"recordsTotal":…,"recordsFiltered":…,"category_message":"","data":[…]}`,
where each row is a **positional array**, not an object:

| Index | Content |
|---|---|
| 0 | Row number the table renders — not data |
| 1 | Date, rendered **twice** (a mobile `<div>` and a desktop one) |
| 2 | Company, with the code in the link: `?stock_code=7212` |
| 3 | Title, with the id in the link: `?ann_id=3695002`, sometimes followed by a `<p>` description |

Four things this corrected in the parser, each of which had been guessed wrong:

- **`COLUMN_ORDER` was off by one** — it had no row-number column, so every field
  landed one place to the left.
- **There is no category column.** Category is a query filter (`cat=`), not a field,
  so `announcement_type` is always `None` and the results filter works off the title.
- **Stock codes are not always numeric** — ETFs use forms like `0823EA`. They come
  from the link's query string, not from a parenthesised suffix.
- **The listing carries no attachments at all.** The PDF is on the announcement's
  own page, so the pipeline fetches that page for matched announcements only.

## The category filter, also confirmed

`cat=FA,FRCO` is the value the site's own *Financial Results* control sends,
captured as `announcements_results_live.json`. It takes the listing from
2,087,762 announcements to 1,081, so a poll reads a shortlist rather than a
haystack. It is the default in `poll_bursa.py` (`DEFAULT_CATEGORY`), and
`--category ""` disables it.

It is **not sufficient on its own** — it still admits filings like *"Change in
Financial Year End"* — so `looks_like_results()` remains the second gate,
matching on the title.

## Still unverified

- **Whether `dt_ht` / `dt_lt` are date-from and date-to**, and in which order.
  `--since` therefore filters client-side.
- **The HTML fallback layout.** The rendered announcements page returns 403 to
  automated clients (Cloudflare), so `announcements_listing.html` mirrors the
  JSON column order rather than a captured page.
- **The detail page structure.** `parse_attachment_page()` accepts any `.pdf`
  link or embed rather than assuming a fixed attachment path.

To pin any of these down, repeat the capture: open the announcements page,
filter to *Financial Results*, and read the `search` request in DevTools →
Network. That is you reading your own browser session, and it needs no
permission from anyone.

## Two shapes on purpose

`announcements_objects.json` and `announcements_arrays.json` describe the *same
four announcements*. The parser test asserts they normalise to identical
records — that is what makes the shape genuinely swappable rather than two
code paths that happen to both run.

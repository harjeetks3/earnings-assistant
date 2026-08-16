"""bytes -> Announcement[]. No network, no database, no side effects.

This module is the replaceability seam. The Bursa announcement endpoint is
undocumented: it can change field names, switch between row shapes, or move
entirely. When that happens only this file and its fixtures change, and the
failure surfaces as a failing test rather than a silently empty poll.

Two shapes are handled because the endpoint has been observed serving both:

  A. records as objects   {"data": [{"company": ..., "title": ...}, ...]}
  B. records as arrays    {"data": [["16 Aug 2026", "<a>ACME</a>", ...], ...]}

Shape B is DataTables-style and positional, so the column order matters. It is
declared in COLUMN_ORDER rather than hardcoded in the loop, so reconciling it
against a freshly captured response is a one-line edit.

NOTE: until a real response is captured and committed as a fixture, the field
names and column order below are a documented best guess. `ParserError` carries
the keys actually seen, so the first live --dry-run tells you exactly what to
change.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

from .models import Announcement, Attachment, normalise_date, strip_tags


class ParserError(Exception):
    """The payload did not match any shape this parser understands.

    Raised rather than returning [] so a changed endpoint is loud. An empty list
    is a legitimate result (no announcements in the window) and must stay
    distinguishable from a broken parser.
    """


# Candidate key names, in priority order, for shape A.
FIELD_ALIASES = {
    "bursa_id":          ("id", "announcement_id", "ann_id"),
    "title":             ("title", "announcement_title", "subject", "name"),
    "stock_code":        ("stock_code", "stockcode", "code", "company_code"),
    "company_name":      ("company", "company_name", "issuer", "entity"),
    "announcement_type": ("category", "type", "announcement_type", "cat"),
    "announced_at":      ("announcement_date", "date", "created_at", "datetime",
                          "date_announced", "announced_at"),
    "url":               ("url", "link", "href", "detail_url"),
}

# Positional layout for shape B.
COLUMN_ORDER = ("announced_at", "company_name", "title", "announcement_type")

_ROOT_KEYS = ("data", "records", "results", "items", "announcements")


def _first(record: dict, names) -> str | None:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    # Case-insensitive second pass — the endpoint is inconsistent about casing.
    lowered = {str(k).lower(): v for k, v in record.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _extract_links(value) -> tuple[Attachment, ...]:
    """Pull <a href> targets out of an HTML cell. Only PDFs are kept — the same
    cell also carries links to the announcement detail page."""
    if not value:
        return ()
    collector = _LinkCollector()
    try:
        collector.feed(str(value))
    except Exception:
        return ()
    return tuple(
        Attachment(filename=text or href.rsplit("/", 1)[-1], url=href)
        for href, text in collector.links
        if href.lower().split("?")[0].endswith(".pdf")
    )


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href, self._text = None, []


def _headline_from_cell(cell) -> str:
    """The headline, not the whole cell.

    Bursa's title column holds the detail link AND the attachment link, so
    flattening the cell would append the PDF filename to the headline
    ("...ended 30 Jun 2026 MAYBANK-Q2FY2026-Results.pdf"). The headline is the
    text of the first non-PDF anchor; a plain cell falls through unchanged.
    """
    if cell is None:
        return ""
    text = str(cell)
    if "<a" not in text.lower():
        return strip_tags(text)
    collector = _LinkCollector()
    try:
        collector.feed(text)
    except Exception:
        return strip_tags(text)
    for href, label in collector.links:
        if label and not href.lower().split("?")[0].endswith(".pdf"):
            return strip_tags(label)
    return strip_tags(text)


def _stock_code_from(record: dict, *cells) -> str | None:
    """Bursa often prints the code alongside the name, e.g. "ACME BERHAD (1234)".
    Try the explicit field first, then dig it out of the rendered text."""
    explicit = _first(record, FIELD_ALIASES["stock_code"])
    if explicit:
        return strip_tags(explicit) or None
    import re
    for cell in cells:
        match = re.search(r"\((\d{4,5})\)", strip_tags(cell))
        if match:
            return match.group(1)
    return None


def _announcement_from_object(record: dict) -> Announcement:
    title = _headline_from_cell(_first(record, FIELD_ALIASES["title"]))
    if not title:
        raise ParserError(
            f"Record has no recognisable title. Keys seen: {sorted(record)}"
        )
    company_cell = _first(record, FIELD_ALIASES["company_name"])
    attachments = ()
    for key in ("attachments", "files", "documents"):
        if record.get(key):
            attachments = _parse_attachment_list(record[key])
            break
    if not attachments:
        attachments = _extract_links(_first(record, FIELD_ALIASES["title"]))
    return Announcement(
        title=title,
        stock_code=_stock_code_from(record, company_cell),
        company_name=strip_tags(company_cell) or None,
        announcement_type=strip_tags(_first(record, FIELD_ALIASES["announcement_type"])) or None,
        announced_at=normalise_date(_first(record, FIELD_ALIASES["announced_at"])),
        url=(_first(record, FIELD_ALIASES["url"]) or None),
        bursa_id=(str(_first(record, FIELD_ALIASES["bursa_id"]))
                  if _first(record, FIELD_ALIASES["bursa_id"]) else None),
        attachments=attachments,
        raw=record,
    )


def _parse_attachment_list(value) -> tuple[Attachment, ...]:
    if isinstance(value, str):
        return _extract_links(value)
    out = []
    for item in value or ():
        if isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if url:
                out.append(Attachment(
                    filename=item.get("filename") or item.get("name") or url.rsplit("/", 1)[-1],
                    url=url,
                ))
        elif isinstance(item, str):
            out.append(Attachment(filename=item.rsplit("/", 1)[-1], url=item))
    return tuple(out)


def _announcement_from_array(cells: list) -> Announcement:
    record = {}
    for index, name in enumerate(COLUMN_ORDER):
        record[name] = cells[index] if index < len(cells) else None
    title = _headline_from_cell(record.get("title"))
    if not title:
        raise ParserError(
            f"Positional record has no title in column {COLUMN_ORDER.index('title')}. "
            f"Row had {len(cells)} cell(s): {[strip_tags(c)[:40] for c in cells]}"
        )
    attachments = ()
    for cell in cells:
        attachments = attachments or _extract_links(cell)
    return Announcement(
        title=title,
        stock_code=_stock_code_from({}, record.get("company_name"), *cells),
        company_name=strip_tags(record.get("company_name")) or None,
        announcement_type=strip_tags(record.get("announcement_type")) or None,
        announced_at=normalise_date(record.get("announced_at")),
        url=next((href for href, _ in _all_links(cells)
                  if not href.lower().split("?")[0].endswith(".pdf")), None),
        attachments=attachments,
        raw={"cells": [str(c) for c in cells]},
    )


def _all_links(cells):
    for cell in cells:
        collector = _LinkCollector()
        try:
            collector.feed(str(cell))
        except Exception:
            continue
        yield from collector.links


def parse_json(payload) -> list[Announcement]:
    """Parse the JSON announcement endpoint. Raises ParserError on an
    unrecognised shape; returns [] only when the payload genuinely holds no
    announcements."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParserError(f"Response was not valid JSON: {exc}") from exc

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in _ROOT_KEYS:
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if rows is None:
            raise ParserError(
                f"No announcement list found. Expected one of {_ROOT_KEYS}, "
                f"got keys: {sorted(payload)}"
            )
    else:
        raise ParserError(f"Unsupported payload type: {type(payload).__name__}")

    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(_announcement_from_object(row))
        elif isinstance(row, (list, tuple)):
            out.append(_announcement_from_array(list(row)))
        else:
            raise ParserError(f"Unsupported row type: {type(row).__name__}")
    return out


class _TableParser(HTMLParser):
    """Minimal <table> row extractor. Uses stdlib rather than BeautifulSoup so
    the offline half of monitoring adds no dependency."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._depth = 0
        elif self._cell is not None:
            self._depth += 1
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    # Keep the anchor so _extract_links can still see it.
                    self._cell.append(f'<a href="{href}">')

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if tag == "a":
                self._cell.append("</a>")
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "a" and self._cell is not None:
            self._cell.append("</a>")
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_html(payload) -> list[Announcement]:
    """Fallback for when the JSON endpoint is unavailable: scrape the rendered
    announcement table. Header rows are skipped by detecting a non-parseable
    date in the date column."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if not isinstance(payload, str):
        raise ParserError(f"Unsupported payload type: {type(payload).__name__}")

    parser = _TableParser()
    parser.feed(payload)
    if not parser.rows:
        raise ParserError("No <tr> rows found in HTML listing")

    out = []
    for row in parser.rows:
        # Skip header/garbage rows: a data row must carry a usable date and title.
        if len(row) < len(COLUMN_ORDER):
            continue
        if normalise_date(row[COLUMN_ORDER.index("announced_at")]) is None:
            continue
        try:
            out.append(_announcement_from_array(row))
        except ParserError:
            continue
    return out

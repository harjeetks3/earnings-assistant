"""Normalised records. Every parser produces these, so downstream code never
has to know whether an announcement came from the JSON endpoint or the HTML
listing fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# Bursa renders dates several ways depending on the surface. Ordered by how
# commonly we expect to see them; the first that parses wins.
_DATE_FORMATS = (
    "%d %b %Y",      # 16 Aug 2026
    "%d %B %Y",      # 16 August 2026
    "%d/%m/%Y",      # 16/08/2026
    "%Y-%m-%d",      # 2026-08-16
    "%d-%b-%Y",      # 16-Aug-2026
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
)

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(value) -> str:
    """Bursa's JSON embeds HTML inside cell values (links, <br>). Reduce a cell
    to its visible text."""
    if value is None:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return _WS_RE.sub(" ", text).strip()


# Bursa's date cell renders the date TWICE, once for mobile and once for
# desktop, so flattening it yields "14 Aug 2026 14 Aug 2026". Matching a date
# anywhere in the text rather than requiring the whole cell to be one is what
# makes that work.
_DATE_SEARCH_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"
    r"|\d{1,2}[/-][A-Za-z]{3,9}[/-]\d{4}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{4}"
)


def normalise_date(value) -> str | None:
    """Return an ISO date string, or None if the value cannot be understood.

    Returning None rather than guessing matters: `announced_at` feeds the
    `--since` window, and a silently wrong date would either skip real
    announcements or re-process old ones."""
    text = strip_tags(value)
    if not text:
        return None
    # Some cells carry a time or a trailing note after the date.
    text = text.split("|")[0].strip()

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    # Not a bare date: pull the first date-shaped run out of the surrounding text.
    for match in _DATE_SEARCH_RE.finditer(text):
        candidate = match.group(0)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return candidate
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return None


@dataclass(frozen=True)
class Attachment:
    """A file linked from an announcement. `url` may be relative to the site root."""
    filename: str
    url: str


@dataclass(frozen=True)
class Announcement:
    """One Bursa announcement, normalised.

    `raw` keeps the original record so a parser bug stays diagnosable after the
    fact — it is stored verbatim in announcements.raw_json.
    """
    title: str
    stock_code: str | None = None
    company_name: str | None = None
    announcement_type: str | None = None
    announced_at: str | None = None      # ISO date, or None if unparseable
    url: str | None = None
    bursa_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.title or not str(self.title).strip():
            raise ValueError("Announcement requires a title")

"""Filter discovered announcements down to companies we actually track.

Matching is deliberately conservative. A false positive costs an API call and
puts a stranger's filing in front of the reviewer; a false negative just means
the announcement is not picked up automatically, which a human can still do by
hand. So: stock code is authoritative, and name matching requires a strong
token overlap rather than a fuzzy score.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Company:
    id: int
    stock_code: str
    name: str
    short_name: str | None = None
    is_active: bool = True


def load_companies(db, *, active_only: bool = True) -> list[Company]:
    sql = "SELECT id, stock_code, name, short_name, is_active FROM companies"
    if active_only:
        sql += " WHERE is_active = 1"
    return [
        Company(
            id=row["id"], stock_code=str(row["stock_code"]).strip(),
            name=row["name"], short_name=row["short_name"],
            is_active=bool(row["is_active"]),
        )
        for row in db.execute(sql)
    ]


def _tokens(text) -> set[str]:
    """Reuse the tokeniser the metadata-contradiction check already uses, so
    "Berhad", "Ltd", "Holdings" and friends are dropped consistently in both
    places. Imported lazily to keep this module free of a Flask import and to
    avoid a cycle once app.py imports the bursa package."""
    from app import _identifying_tokens
    return _identifying_tokens(text)


def match_company(announcement, companies: list[Company]) -> Company | None:
    """Return the tracked company an announcement belongs to, or None.

    Stock code wins outright when present. Otherwise names must share every
    identifying token of the shorter name, and at least one token overall —
    so "Maybank" matches "Malayan Banking Berhad" only if the tokens line up,
    and a partial coincidence like a shared "Industries" does not match.
    """
    code = (announcement.stock_code or "").strip()
    if code:
        for company in companies:
            if company.stock_code == code:
                return company
        # An explicit but unknown code is decisive: do not fall through to a
        # name guess, or an untracked company could be matched by coincidence.
        return None

    name_tokens = _tokens(announcement.company_name)
    if not name_tokens:
        return None

    best = None
    for company in companies:
        for candidate in (company.name, company.short_name):
            company_tokens = _tokens(candidate)
            if not company_tokens:
                continue
            smaller = min(name_tokens, company_tokens, key=len)
            if smaller and smaller <= (name_tokens & company_tokens):
                # Prefer the match that shares the most tokens.
                overlap = len(name_tokens & company_tokens)
                if best is None or overlap > best[0]:
                    best = (overlap, company)
    return best[1] if best else None


def filter_to_watchlist(announcements, companies: list[Company]):
    """Yield (announcement, company) for announcements we track. Untracked
    announcements are dropped silently — the caller logs the counts."""
    for announcement in announcements:
        company = match_company(announcement, companies)
        if company is not None:
            yield announcement, company

"""Stable identity for announcements and attachments, plus idempotent inserts.

Idempotency is the property the whole monitor rests on: polling the same window
twice must insert nothing the second time. Without it, every run would re-queue
the same filings and the reviewer's list would fill with duplicates.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

_WS_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def normalise_title(title: str) -> str:
    """Fold case and whitespace so cosmetic re-rendering of the same headline
    does not read as a new announcement."""
    return _WS_RE.sub(" ", (title or "").strip().lower())


def dedup_key(announcement) -> str:
    """Stable identity for an announcement.

    Bursa's own id is used when present. Otherwise the key is derived from the
    fields that identify a filing rather than describe it — issuer, date and
    headline — so a changed summary or reordered listing does not create a
    duplicate. The date is included because a company can file the same
    headline in consecutive quarters.
    """
    if announcement.bursa_id:
        return f"bursa:{announcement.bursa_id}"
    basis = "|".join((
        (announcement.stock_code or "").strip(),
        normalise_title(announcement.company_name or ""),
        announcement.announced_at or "",
        normalise_title(announcement.title),
    ))
    return "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def upsert_announcement(db, announcement, company_id: int | None) -> tuple[int, bool]:
    """Insert if new. Returns (announcement_id, created). Never updates an
    existing row: a discovered announcement is a historical fact, and rewriting
    it would quietly change what the reviewer already saw."""
    key = dedup_key(announcement)
    existing = db.execute(
        "SELECT id FROM announcements WHERE dedup_key = ?", (key,)
    ).fetchone()
    if existing:
        return existing["id"], False

    cur = db.execute(
        """INSERT INTO announcements (
               company_id, bursa_announcement_id, dedup_key, title,
               announcement_type, announced_at, url, raw_json, status, discovered_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            company_id, announcement.bursa_id, key, announcement.title,
            announcement.announcement_type, announcement.announced_at,
            announcement.url, json.dumps(announcement.raw, ensure_ascii=False,
                                         default=str),
            "discovered", _now(),
        ),
    )
    db.commit()
    return cur.lastrowid, True


def upsert_attachment(db, announcement_id: int, attachment, *,
                      sha256: str | None = None, file_size: int | None = None,
                      local_path: str | None = None,
                      verification_status: str = "pending",
                      verification_detail: str | None = None) -> tuple[int, bool]:
    """Insert if new for this announcement, or complete a row already recorded.

    The pipeline records each discovered file before downloading it, so that a
    failure part-way through a multi-file announcement leaves a durable note of
    what is still outstanding. That means this is called twice per attachment:
    once with no hash, then again once the bytes are in hand. The second call
    must COMPLETE the first row rather than insert a second one, or the
    outstanding-work query would never drain.
    """
    if sha256:
        existing = db.execute(
            "SELECT id FROM attachments WHERE announcement_id = ? AND sha256 = ?",
            (announcement_id, sha256),
        ).fetchone()
        if existing:
            # The same document reached us under two URLs — announcements do
            # link a file more than once. UNIQUE(announcement_id, sha256) means
            # one row is the correct outcome, so drop the placeholder we made
            # for the other URL. Leaving it would keep the announcement looking
            # permanently unfinished and re-fetched on every poll.
            db.execute(
                "DELETE FROM attachments "
                " WHERE announcement_id = ? AND url = ? AND sha256 IS NULL",
                (announcement_id, attachment.url),
            )
            db.commit()
            return existing["id"], False

        # A row we created before downloading: fill it in.
        placeholder = db.execute(
            "SELECT id FROM attachments "
            " WHERE announcement_id = ? AND url = ? AND sha256 IS NULL",
            (announcement_id, attachment.url),
        ).fetchone()
        if placeholder:
            db.execute(
                """UPDATE attachments
                      SET sha256 = ?, file_size = ?, local_path = ?,
                          verification_status = ?, verification_detail = ?,
                          downloaded_at = ?
                    WHERE id = ?""",
                (sha256, file_size, local_path, verification_status,
                 verification_detail, _now(), placeholder["id"]),
            )
            db.commit()
            return placeholder["id"], False
    else:
        existing = db.execute(
            "SELECT id FROM attachments WHERE announcement_id = ? AND url = ?",
            (announcement_id, attachment.url),
        ).fetchone()
        if existing:
            return existing["id"], False

    cur = db.execute(
        """INSERT INTO attachments (
               announcement_id, filename, url, sha256, file_size, local_path,
               verification_status, verification_detail, downloaded_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            announcement_id, attachment.filename, attachment.url, sha256, file_size,
            local_path, verification_status, verification_detail,
            _now() if local_path else None,
        ),
    )
    db.commit()
    return cur.lastrowid, True


def already_ingested(db, sha256: str, file_size: int) -> dict | None:
    """Has this exact file already been through the system, by either route?

    Reuses the manual-upload duplicate rule (sha256 + file_size) so a filing the
    journalist already uploaded by hand is recognised instead of re-processed.
    """
    row = db.execute(
        "SELECT id, filename FROM pdf_metadata WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if row:
        return {"scope": "approved", "id": row["id"], "filename": row["filename"]}
    row = db.execute(
        "SELECT id, filename FROM pending_reviews WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if row:
        return {"scope": "pending", "id": row["id"], "filename": row["filename"]}
    return None

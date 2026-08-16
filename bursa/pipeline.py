"""discovery -> watchlist filter -> dedup -> download -> hash -> verify -> queue.

The pipeline stops at "queue". It never calls the LLM and never writes to
pdf_metadata: a verified attachment simply sits in the discovery queue until a
human asks for it. That keeps API spend under the reviewer's control and keeps
the human gate exactly where it already was.
"""
from __future__ import annotations

import hashlib
import os
import re

from . import dedup, watchlist
from .client import BlockedError, BursaClientError, RequestBudgetExceeded
from .parser import ParserError, parse_attachment_page, parse_html, parse_json
from .verify import VERIFIED, verify_pdf_bytes

# Only announcements that look like results filings are worth queueing. Kept
# broad on purpose — a missed match costs a manual upload, and the reviewer sees
# everything that does match before any money is spent.
RESULTS_PATTERNS = (
    re.compile(r"quarterly\s+r(?:e)?p(?:or)?t", re.I),
    # Not every results filing says "quarterly". Bursa's own FA,FRCO category
    # returned "Consolidated results for the financial period ended 31/05/2026"
    # (Key Asic, 2026-07-22), which the quarterly pattern misses entirely.
    re.compile(r"\bconsolidated\s+results?\b", re.I),
    re.compile(r"\bfinancial\s+results?\b", re.I),
    re.compile(r"\binterim\s+(?:financial\s+)?report\b", re.I),
    re.compile(r"\bcondensed\s+consolidated\b", re.I),
)


def looks_like_results(announcement) -> bool:
    haystack = f"{announcement.title} {announcement.announcement_type or ''}"
    return any(p.search(haystack) for p in RESULTS_PATTERNS)


def _safe_filename(name: str, fallback: str) -> str:
    base = os.path.basename((name or "").split("?")[0]).strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if not base.lower().endswith(".pdf"):
        base = (base or fallback) + ".pdf"
    return base[:120]


class PollSummary(dict):
    """Counters for one run, printed by the CLI and easy to assert on."""

    FIELDS = ("fetched", "parsed", "results_filings", "watchlisted", "new_announcements",
              "detail_pages_fetched", "attachments_seen", "downloaded", "verified",
              "rejected", "already_ingested", "skipped_existing", "no_attachment",
              "errors")

    def __init__(self):
        super().__init__({f: 0 for f in self.FIELDS})
        self["messages"] = []

    def note(self, message: str):
        self["messages"].append(message)


def run_poll(db, client, *, since: str | None = None, limit: int | None = None,
             dry_run: bool = False, attachments_folder: str,
             listing_path: str = "/api/v1/announcements/search",
             listing_params: dict | None = None,
             use_html: bool = False, pages: int = 1) -> PollSummary:
    """One polling pass. Returns a PollSummary; never raises for ordinary
    failures — a monitor that dies on a bad row is worse than one that reports
    it. BlockedError is the exception: it stops the run immediately."""
    summary = PollSummary()
    announcements = []

    # ---- fetch + parse, one listing page at a time ----
    # The listing is not filtered by category, so a single page covers only the
    # most recent handful of announcements of every kind. Walking a couple of
    # pages gives an hourly poll enough headroom during results season.
    for page in range(1, max(1, pages) + 1):
        params = dict(listing_params or {})
        if params:
            params["page"] = page
        elif page > 1:
            break

        try:
            payload = client.fetch(listing_path, params or None)
            summary["fetched"] += 1
        except BlockedError as exc:
            summary.note(f"BLOCKED: {exc}")
            summary["errors"] += 1
            return summary
        except (BursaClientError, RequestBudgetExceeded) as exc:
            summary.note(f"Fetch failed on page {page}: {exc}")
            summary["errors"] += 1
            break

        try:
            batch = parse_html(payload) if use_html else parse_json(payload)
        except ParserError as exc:
            summary.note(
                f"PARSER: {exc}\n"
                f"  The endpoint shape has probably changed. Save this response as a "
                f"fixture and reconcile bursa/parser.py COLUMN_ORDER / FIELD_ALIASES."
            )
            summary["errors"] += 1
            break

        announcements.extend(batch)
        if not batch:
            break  # ran past the end of the results

    summary["parsed"] = len(announcements)

    if since:
        announcements = [a for a in announcements
                         if a.announced_at is None or a.announced_at >= since]

    # ---- filter to results filings, then to the watchlist ----
    filings = [a for a in announcements if looks_like_results(a)]
    summary["results_filings"] = len(filings)

    companies = watchlist.load_companies(db, active_only=True)
    matched = list(watchlist.filter_to_watchlist(filings, companies))
    summary["watchlisted"] = len(matched)

    if limit:
        matched = matched[:limit]

    for announcement, company in matched:
        try:
            _process_one(db, client, announcement, company, summary,
                         dry_run=dry_run, attachments_folder=attachments_folder)
        except BlockedError as exc:
            summary.note(f"BLOCKED: {exc}")
            summary["errors"] += 1
            return summary
        except RequestBudgetExceeded as exc:
            summary.note(f"{exc}")
            summary["errors"] += 1
            return summary
        except Exception as exc:  # one bad announcement must not end the run
            summary["errors"] += 1
            summary.note(f"{announcement.title[:60]!r}: {type(exc).__name__}: {exc}")

    return summary


def _process_one(db, client, announcement, company, summary, *,
                 dry_run: bool, attachments_folder: str) -> None:
    if dry_run:
        summary.note(
            f"[dry-run] would queue {company.stock_code} "
            f"{announcement.announced_at} {announcement.title[:70]}"
        )
        summary["attachments_seen"] += len(announcement.attachments)
        return

    announcement_id, created = dedup.upsert_announcement(db, announcement, company.id)
    if created:
        summary["new_announcements"] += 1
    else:
        summary["skipped_existing"] += 1
        # Already seen, so normally there is nothing to do — re-downloading
        # would spend requests to learn nothing.
        #
        # But the announcement row is written BEFORE its attachments are
        # fetched, so a download that failed on the run that discovered it
        # (a timeout, a 500) would otherwise strand the filing forever: every
        # later poll would skip it as "already seen" and its PDF would never
        # arrive. Compare what is stored against what the listing offers, and
        # only retry when something is genuinely missing.
        stored = db.execute(
            "SELECT COUNT(*) FROM attachments WHERE announcement_id = ?",
            (announcement_id,),
        ).fetchone()[0]
        # The listing does not say how many files an announcement has, so once
        # anything is stored, treat it as handled. A first pass that found none
        # (text-only announcement, or a failed detail fetch) is retried.
        if stored:
            return
        summary.note(
            f"retrying {len(announcement.attachments) - stored} missing attachment(s) "
            f"for previously discovered {announcement.title[:60]!r}"
        )

    # The listing carries date, company and title only — no files. The PDF is on
    # the announcement's own page, so fetch it, but only for the handful of
    # announcements that survived the results filter and the watchlist.
    attachments = announcement.attachments
    if not attachments and announcement.url:
        detail = client.fetch(announcement.url)
        summary["detail_pages_fetched"] += 1
        attachments = parse_attachment_page(detail)
        if not attachments:
            summary["no_attachment"] += 1
            summary.note(
                f"no PDF linked from {announcement.title[:60]!r} "
                f"({announcement.url}) — nothing to queue"
            )
            return

    for attachment in attachments:
        summary["attachments_seen"] += 1
        data = client.fetch(attachment.url)
        summary["downloaded"] += 1

        sha256 = hashlib.sha256(data).hexdigest()
        file_size = len(data)

        seen = dedup.already_ingested(db, sha256, file_size)
        if seen:
            summary["already_ingested"] += 1
            dedup.upsert_attachment(
                db, announcement_id, attachment, sha256=sha256, file_size=file_size,
                verification_status="already_ingested",
                verification_detail=(
                    f"Already in the system as {seen['scope']} #{seen['id']} "
                    f"({seen['filename']})"
                ),
            )
            continue

        status, detail = verify_pdf_bytes(data)
        local_path = None
        if status == VERIFIED:
            os.makedirs(attachments_folder, exist_ok=True)
            filename = _safe_filename(attachment.filename, f"announcement_{announcement_id}")
            local_path = os.path.join(attachments_folder, filename)
            if os.path.exists(local_path):
                stem, ext = os.path.splitext(filename)
                local_path = os.path.join(attachments_folder, f"{stem}_{sha256[:8]}{ext}")
            with open(local_path, "wb") as f:
                f.write(data)
            summary["verified"] += 1
        else:
            summary["rejected"] += 1
            summary.note(f"rejected {attachment.filename}: {detail}")

        dedup.upsert_attachment(
            db, announcement_id, attachment, sha256=sha256, file_size=file_size,
            local_path=local_path, verification_status=status,
            verification_detail=detail,
        )

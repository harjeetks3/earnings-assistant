#!/usr/bin/env python
"""Bursa announcement monitor. Runs standalone — the Flask app never makes an
outbound Bursa request, so if this breaks the review tool is unaffected.

    python poll_bursa.py --dry-run                 # show what would be queued
    python poll_bursa.py --once --since 2026-08-01 # one real pass
    python poll_bursa.py --fixture-dir tests/fixtures/bursa --once   # offline

Schedule with Task Scheduler or cron. Hourly is plenty; the client refuses to
go below a 2-second gap between requests and caps each run at 30 requests.

Discovery only ever QUEUES a filing. No OpenAI call is made here, and nothing
is written to pdf_metadata — a human still triggers extraction and still
approves the result.
"""
from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import app  # noqa: E402
from bursa import pipeline  # noqa: E402
from bursa.client import DEFAULT_USER_AGENT, BursaClient, FixtureClient  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll Bursa for results announcements from watchlisted companies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--once", action="store_true",
                   help="Run a single pass (the default; there is no loop mode "
                        "on purpose — use the OS scheduler)")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="Ignore announcements dated before this")
    p.add_argument("--limit", type=int,
                   help="Process at most N matched announcements this run")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and match, but download nothing and write nothing")
    p.add_argument("--fixture-dir", metavar="DIR",
                   help="Replay saved payloads from DIR instead of making requests")
    p.add_argument("--fixture-listing", default="announcements_objects.json",
                   help="Which fixture file to treat as the listing response")
    p.add_argument("--html", action="store_true",
                   help="Parse the listing as HTML instead of JSON (fallback)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                   help="Override the identifying User-Agent (keep a contact address in it)")
    p.add_argument("--cache-dir", default=os.path.join(BASE_DIR, "bursa_cache"),
                   help="Where to save raw responses for later diagnosis")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    app.init_db()

    if args.fixture_dir:
        # A real filing stands in for every attachment so the full chain —
        # download, hash, verify, queue — runs without a request leaving the box.
        sample = os.path.join(BASE_DIR, "test_data",
                              "01_golden_NorthPeak_Analytics_Q1_FY2026.pdf")
        client = FixtureClient(args.fixture_dir, listing=args.fixture_listing,
                               sample_pdf=sample if os.path.exists(sample) else None)
        print(f"[poll] fixture mode: {args.fixture_dir}/{args.fixture_listing} "
              f"(no network)")
    else:
        client = BursaClient(user_agent=args.user_agent, cache_dir=args.cache_dir)
        print(f"[poll] live mode as: {args.user_agent}")
        print(f"[poll] min {client.min_delay}s between requests, "
              f"max {client.max_requests} requests this run")

    with app.app.app_context():
        db = app.get_db()
        summary = pipeline.run_poll(
            db, client,
            since=args.since,
            limit=args.limit,
            dry_run=args.dry_run,
            attachments_folder=app.ATTACHMENTS_FOLDER,
            use_html=args.html,
        )

    print("\n[poll] summary")
    for field in pipeline.PollSummary.FIELDS:
        print(f"    {field:<20} {summary[field]}")
    for message in summary["messages"]:
        print(f"    · {message}")

    if not args.dry_run and summary["verified"]:
        print(f"\n[poll] {summary['verified']} filing(s) queued for review. "
              f"Open the app and click Extract on the ones you want — "
              f"no API call is spent until you do.")

    # Non-zero only when nothing could be done at all, so a scheduled run does
    # not alarm on an ordinary quiet hour.
    return 1 if summary["errors"] and not summary["parsed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

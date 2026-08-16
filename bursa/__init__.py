"""Bursa Malaysia announcement monitoring.

Deliberately split so that everything except `client` is pure and offline:

    models     normalised records shared by every parser
    parser     bytes -> Announcement[]      no network, no DB
    watchlist  filter announcements to tracked companies
    dedup      stable keys and idempotent inserts
    verify     downloaded-PDF checks before any paid API call
    client     the ONLY module that touches the network

The endpoint we read is undocumented and may change without notice, so parsing
is isolated behind `parser`. When the shape changes, that module and its saved
fixtures are what change — a shape change fails a test rather than a live run.
"""

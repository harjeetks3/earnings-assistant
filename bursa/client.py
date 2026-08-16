"""The only module in this package that touches the network.

Everything here is built to be a good citizen of someone else's site, because
the endpoint is undocumented and we are a guest on it:

  * one identifiable User-Agent carrying a contact address
  * robots.txt is consulted and obeyed
  * a hard minimum delay between requests
  * a hard cap on requests per run
  * 403 or 429 ABORTS the run -- we never retry harder, rotate anything, or
    dress the request up as a browser

There is deliberately no proxy support, no User-Agent list, no cookie or
referer spoofing and no retry-on-block. Those are the mechanics of evading a
block, and this tool does not do that. If Bursa blocks us, the correct
behaviour is to stop and tell the operator.

Built on stdlib urllib so monitoring adds no dependency to the project.
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

DEFAULT_BASE_URL = "https://www.bursamalaysia.com"

# Identify the tool and give the site owner a way to reach the operator. A
# contact address is what separates a declared, low-volume reader from an
# anonymous scraper.
DEFAULT_USER_AGENT = (
    "BursaEarningsBriefAssistant/0.2 (newsroom earnings review tool; "
    "contact: karamjit@digitalnewsasia.com)"
)

MIN_ALLOWED_DELAY = 2.0     # seconds between requests, floor
MAX_REQUESTS_PER_RUN = 30
DEFAULT_TIMEOUT = 20.0
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


class BursaClientError(Exception):
    """Any failure while fetching."""


class BlockedError(BursaClientError):
    """The server told us to go away (403/429). The run stops here.

    Separate from other errors so callers cannot accidentally treat it as a
    transient failure worth retrying.
    """


class RequestBudgetExceeded(BursaClientError):
    """The per-run request cap was hit. A safety net against a loop that would
    otherwise hammer the site."""


class BursaClient:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL,
                 user_agent: str = DEFAULT_USER_AGENT,
                 min_delay: float = MIN_ALLOWED_DELAY,
                 max_requests: int = MAX_REQUESTS_PER_RUN,
                 timeout: float = DEFAULT_TIMEOUT,
                 respect_robots: bool = True,
                 cache_dir: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        # The floor is not configurable downwards; a caller cannot ask to be rude.
        self.min_delay = max(float(min_delay), MIN_ALLOWED_DELAY)
        self.max_requests = min(int(max_requests), MAX_REQUESTS_PER_RUN)
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.cache_dir = cache_dir
        self.requests_made = 0
        self._last_request_at = 0.0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._robots_error: str | None = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ---- robots ----------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        # A previous failure is remembered. Without this, every attachment in
        # the run would re-fetch robots.txt after a network blip — hammering
        # the very file we are trying to respect.
        if self._robots_error is not None:
            raise BursaClientError(self._robots_error)
        if self._robots is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(urllib.parse.urljoin(self.base_url + "/", "robots.txt"))
            try:
                parser.read()
            except Exception as exc:
                # Can't read robots.txt: assume we're not welcome rather than
                # assuming we are. (A 404 is handled inside read() as "no rules",
                # which is the correct reading of a site with no robots.txt.)
                self._robots_error = (
                    f"Could not read robots.txt ({exc}) — refusing to fetch"
                )
                raise BursaClientError(self._robots_error) from exc
            self._robots = parser
        return self._robots.can_fetch(self.user_agent, url)

    # ---- pacing ----------------------------------------------------------
    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)

    def absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return urllib.parse.urljoin(self.base_url + "/", path_or_url.lstrip("/"))

    # ---- fetch -----------------------------------------------------------
    def fetch(self, path_or_url: str, params: dict | None = None,
              *, cache_name: str | None = None) -> bytes:
        url = self.absolute(path_or_url)
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"

        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(
                f"Per-run request cap of {self.max_requests} reached — stopping. "
                f"Narrow the window or raise the cap deliberately."
            )
        if not self._robots_allows(url):
            raise BursaClientError(f"robots.txt disallows fetching {url}")

        self._wait_turn()
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        })

        self.requests_made += 1
        self._last_request_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                raise BlockedError(
                    f"Bursa returned HTTP {exc.code} for {url}. Stopping this run."
                    + (f" Server asked us to wait {retry_after}s." if retry_after else "")
                    + " Not retrying — reduce the polling frequency and try later."
                ) from exc
            raise BursaClientError(f"HTTP {exc.code} fetching {url}") from exc
        except urllib.error.URLError as exc:
            raise BursaClientError(f"Could not reach {url}: {exc.reason}") from exc

        if len(body) > MAX_DOWNLOAD_BYTES:
            raise BursaClientError(
                f"Response from {url} exceeded {MAX_DOWNLOAD_BYTES} bytes — refusing"
            )

        if self.cache_dir:
            name = cache_name or _safe_cache_name(url)
            with open(os.path.join(self.cache_dir, name), "wb") as f:
                f.write(body)
        return body


class FixtureClient:
    """Replays saved payloads from disk. Same surface as BursaClient, so
    poll_bursa.py exercises the identical pipeline offline.

    Any URL ending in .pdf resolves to a sample PDF, which lets the whole chain
    — discovery, dedup, download, hashing, verification, queueing — run end to
    end in a test without a single request leaving the machine.
    """

    def __init__(self, fixture_dir: str, *, listing: str = "announcements_objects.json",
                 sample_pdf: str | None = None):
        self.fixture_dir = fixture_dir
        self.listing = listing
        self.sample_pdf = sample_pdf
        self.requests_made = 0
        self.base_url = "fixture://"

    def absolute(self, path_or_url: str) -> str:
        return path_or_url

    def fetch(self, path_or_url: str, params: dict | None = None,
              *, cache_name: str | None = None) -> bytes:
        self.requests_made += 1
        target = path_or_url.lower().split("?")[0]
        if target.endswith(".pdf"):
            if not self.sample_pdf:
                raise BursaClientError(
                    f"No sample PDF configured for fixture fetch of {path_or_url}"
                )
            with open(self.sample_pdf, "rb") as f:
                return f.read()
        path = os.path.join(self.fixture_dir, self.listing)
        if not os.path.exists(path):
            raise BursaClientError(f"Fixture not found: {path}")
        with open(path, "rb") as f:
            return f.read()


def _safe_cache_name(url: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in url)
    return keep[-120:] or "response"

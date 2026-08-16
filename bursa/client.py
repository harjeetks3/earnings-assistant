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
import re
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
MAX_ROBOTS_BYTES = 512 * 1024


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
        self._wildcard_disallows: list = []
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
            self._robots = self._load_robots()
        if not self._robots.can_fetch(self.user_agent, url):
            return False
        # Python's robotparser ignores '*' inside a Disallow path, so a rule
        # like "Disallow: /api/*" would be silently unenforced. Apply those
        # ourselves rather than fetching something explicitly forbidden.
        path = urllib.parse.urlsplit(url).path or "/"
        if urllib.parse.urlsplit(url).query:
            path += "?" + urllib.parse.urlsplit(url).query
        return not any(p.match(path) for p in self._wildcard_disallows)

    def _load_robots(self) -> urllib.robotparser.RobotFileParser:
        """Fetch and parse robots.txt **using our own User-Agent**.

        RobotFileParser.read() would do this for us, but it requests the file
        with Python's default urllib User-Agent. A site that rejects unidentified
        clients answers 403, and read() turns that into disallow_all — so the
        monitor concludes it is banned from the whole site when in fact it simply
        never introduced itself. Bursa behaves exactly this way.

        So we make the request ourselves, with the same identity we use for
        everything else, and hand the text to parse().
        """
        url = urllib.parse.urljoin(self.base_url + "/", "robots.txt")
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_ROBOTS_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Refused the rules themselves, to a client that did identify
                # itself. Read that as "not welcome" and stop.
                self._robots_error = (
                    f"robots.txt returned HTTP {exc.code} to an identified client "
                    f"— treating the site as closed to us and stopping."
                )
                raise BlockedError(self._robots_error) from exc
            if 400 <= exc.code < 500:
                body = ""  # No robots.txt published: no restrictions to honour.
            else:
                self._robots_error = (
                    f"robots.txt returned HTTP {exc.code} — refusing to fetch"
                )
                raise BursaClientError(self._robots_error) from exc
        except Exception as exc:
            # Cannot read the rules at all: assume we are not welcome rather
            # than assuming we are.
            self._robots_error = f"Could not read robots.txt ({exc}) — refusing to fetch"
            raise BursaClientError(self._robots_error) from exc

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        self._wildcard_disallows = _wildcard_disallow_patterns(body, self.user_agent)

        # If the site asks for a slower pace than ours, take theirs.
        try:
            stated = parser.crawl_delay(self.user_agent)
        except Exception:
            stated = None
        if stated:
            self.min_delay = max(self.min_delay, float(stated))
        return parser

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

    # The real listing links to an announcement page rather than to files, so a
    # fixture run has to be able to serve that page too or the detail-fetch step
    # goes untested. `detail_pdf` controls whether the stand-in page links a PDF.
    DETAIL_TEMPLATE = (
        "<html><body><h1>Announcement</h1>"
        "<a href='/market_information/announcements/company_announcement'>Back</a>"
        "{pdf}</body></html>"
    )

    def __init__(self, fixture_dir: str, *, listing: str = "announcements_objects.json",
                 sample_pdf: str | None = None, detail_pdf: bool = True):
        self.fixture_dir = fixture_dir
        self.listing = listing
        self.sample_pdf = sample_pdf
        self.detail_pdf = detail_pdf
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

        if "announcement_details" in path_or_url:
            ann_id = path_or_url.rsplit("=", 1)[-1]
            pdf = (f"<a href='/misc/announcement/attachment/ann_{ann_id}.pdf'>"
                   f"ann_{ann_id}.pdf</a>") if self.detail_pdf else ""
            return self.DETAIL_TEMPLATE.format(pdf=pdf).encode("utf-8")

        # Only page 1 has fixture data. Replaying it for every page would make a
        # fixture run report duplicate announcements that a real run would not.
        if params and int(params.get("page", 1) or 1) > 1:
            return b'{"recordsTotal":0,"recordsFiltered":0,"category_message":"","data":[]}'

        path = os.path.join(self.fixture_dir, self.listing)
        if not os.path.exists(path):
            raise BursaClientError(f"Fixture not found: {path}")
        with open(path, "rb") as f:
            return f.read()


def _wildcard_disallow_patterns(body: str, user_agent: str) -> list:
    """Disallow rules containing '*' or '$', compiled to regexes.

    Python's robotparser matches a Disallow path by plain prefix, so any
    wildcard in it is treated as a literal character and the rule never fires.
    We collect those rules from the groups that apply to us and enforce them
    separately — erring toward not fetching.
    """
    ua = (user_agent or "").lower()
    applicable, current_agents, in_group = [], [], False

    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()

        if field == "user-agent":
            if in_group:            # a new block starts after any rule line
                current_agents = []
                in_group = False
            current_agents.append(value.lower())
        elif field == "disallow":
            in_group = True
            applies = any(a == "*" or (a and a in ua) for a in current_agents)
            if applies and value and ("*" in value or "$" in value):
                applicable.append(value)
        else:
            in_group = True

    compiled = []
    for pattern in applicable:
        out = []
        for index, char in enumerate(pattern):
            if char == "*":
                out.append(".*")
            elif char == "$" and index == len(pattern) - 1:
                out.append("$")
            else:
                out.append(re.escape(char))
        compiled.append(re.compile("^" + "".join(out)))
    return compiled


def _safe_cache_name(url: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in url)
    return keep[-120:] or "response"

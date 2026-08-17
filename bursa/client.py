"""The only module in this package that touches the network.

Everything here is built to be a good citizen of someone else's site, because
the endpoint is undocumented and we are a guest on it:

  * one identifiable User-Agent carrying a contact address
  * robots.txt is consulted and obeyed PER HOST -- that is the scope
    robots.txt is defined at, and Python's parser cannot enforce it for us
  * a hard minimum delay between requests to the same host
  * a hard cap on requests per run, plus a share of it per host
  * 403 or 429 ABORTS the run -- we never retry harder, rotate anything, or
    dress the request up as a browser

There is deliberately no proxy support, no User-Agent list, no cookie or
referer spoofing and no retry-on-block. Those are the mechanics of evading a
block, and this tool does not do that. If a site blocks us, the correct
behaviour is to stop and tell the operator.

Built on stdlib urllib so monitoring adds no dependency to the project.
"""
from __future__ import annotations

import hashlib
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

MIN_ALLOWED_DELAY = 2.0     # seconds between requests to one host, floor
# A site may ask us to go slower, never faster. Past this we do not speed up --
# we skip the host, because sitting out a whole run is not politeness either.
MAX_HONOURED_DELAY = 60.0
MAX_REQUESTS_PER_RUN = 30
# Defaults to the run cap so a single-host run behaves exactly as before. A
# caller that walks many hosts lowers it so one site cannot spend the run.
MAX_REQUESTS_PER_HOST = MAX_REQUESTS_PER_RUN
# Each new host costs a robots.txt read, which the request cap does not count.
MAX_HOSTS_PER_RUN = 40
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


class HostBudgetExceeded(RequestBudgetExceeded):
    """One host's share of the run is spent.

    A subclass on purpose: every existing caller catches RequestBudgetExceeded
    and ends the run, which is still right when there is one host. A caller
    that polls many hosts catches this narrower type and moves to the next one
    -- one chatty site must not end everyone else's poll.
    """


def _host_key(url: str) -> str:
    """scheme://host[:port] -- the identity robots.txt is actually scoped to.

    The scheme is part of the key because http and https serve *different*
    robots.txt documents; netloc alone would let one stand in for the other,
    which is the mistake this whole module change exists to stop. Scheme and
    host are lowercased because both are case-insensitive (the path is not).
    Default ports collapse so https://x and https://x:443 do not each fetch
    their own copy of the rules. Userinfo is dropped so a credential can never
    reach a dict key or a cache filename.

    Returns "" for anything without a usable http(s) host -- callers refuse it
    rather than guess. That also keeps file:// and ftp:// links out of
    urlopen(): urljoin() happily passes an absolute foreign-scheme href
    straight through, and such a URL has no host to be a good citizen of.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:      # a malformed port is not a host we can reason about
        return ""
    if port and port != (443 if scheme == "https" else 80):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class _HostState:
    """Everything the conduct rules track about ONE site.

    robots.txt, its Crawl-delay, the pacing clock and the request budget are
    properties of a server, not of the client. They were one un-keyed slot, so
    a URL on a second host was judged against the first host's rules --
    silently, and in the permissive direction, because RobotFileParser.can_fetch
    discards the scheme and netloc and matches on the path alone. It cannot
    catch that mistake for us; keying the state here is what prevents it.
    """

    __slots__ = ("robots", "robots_error", "wildcard_disallows", "delay",
                 "last_request_at", "requests_made")

    def __init__(self, delay: float):
        self.robots: urllib.robotparser.RobotFileParser | None = None
        self.robots_error: str | None = None
        self.wildcard_disallows: list = []
        self.delay = delay
        self.last_request_at = 0.0
        self.requests_made = 0


class BursaClient:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL,
                 user_agent: str = DEFAULT_USER_AGENT,
                 min_delay: float = MIN_ALLOWED_DELAY,
                 max_requests: int = MAX_REQUESTS_PER_RUN,
                 max_requests_per_host: int = MAX_REQUESTS_PER_HOST,
                 timeout: float = DEFAULT_TIMEOUT,
                 respect_robots: bool = True,
                 cache_dir: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        # The floor is not configurable downwards; a caller cannot ask to be rude.
        # It is a floor for every host, not one shared value: see min_delay below.
        self._delay_floor = max(float(min_delay), MIN_ALLOWED_DELAY)
        self.max_requests = min(int(max_requests), MAX_REQUESTS_PER_RUN)
        self.max_requests_per_host = max(
            1, min(int(max_requests_per_host), MAX_REQUESTS_PER_HOST))
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.cache_dir = cache_dir
        # Run-wide, across every host. Unchanged meaning: the ceiling that stops
        # a runaway run no matter how the requests are spread around.
        self.requests_made = 0
        self._hosts: dict[str, _HostState] = {}
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ---- per-host state --------------------------------------------------
    def _host_state(self, key: str) -> _HostState:
        state = self._hosts.get(key)
        if state is None:
            if len(self._hosts) >= MAX_HOSTS_PER_RUN:
                raise RequestBudgetExceeded(
                    f"This run has already visited {MAX_HOSTS_PER_RUN} hosts — "
                    f"stopping. Every new host costs a robots.txt read that the "
                    f"request cap does not count; widen it deliberately."
                )
            state = _HostState(self._delay_floor)
            self._hosts[key] = state
        return state

    @property
    def min_delay(self) -> float:
        """Effective delay for this client's own base host.

        Read-only: pacing is per host now, so there is no single value left to
        assign. Use delay_for(url) for any other host.
        """
        state = self._hosts.get(_host_key(self.base_url))
        return state.delay if state else self._delay_floor

    def delay_for(self, path_or_url: str) -> float:
        state = self._hosts.get(_host_key(self.absolute(path_or_url)))
        return state.delay if state else self._delay_floor

    @property
    def requests_made_by_host(self) -> dict:
        """Where the run's budget actually went. requests_made stays the total."""
        return {key: state.requests_made for key, state in self._hosts.items()}

    # ---- robots ----------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        key = _host_key(url)
        if not key:
            raise BursaClientError(
                f"Refusing {url}: only http(s) URLs with a host are fetched"
            )
        state = self._host_state(key)
        # A previous failure is remembered PER HOST. Without this, every
        # attachment on that host would re-fetch robots.txt after a network
        # blip — hammering the very file we are trying to respect. Keying it
        # is what stops one unreachable site from silencing all the others.
        if state.robots_error is not None:
            raise BursaClientError(state.robots_error)
        if state.robots is None:
            state.robots = self._load_robots(key, state)
        if not state.robots.can_fetch(self.user_agent, url):
            return False
        # can_fetch() matches the PATH only — it discards the hostname — so a
        # parser loaded from one site will answer confidently about another.
        # The per-host state above is the only thing preventing that.
        #
        # It also ignores '*' inside a Disallow path, so a rule like
        # "Disallow: /api/*" would be silently unenforced. Apply those ourselves
        # rather than fetching something explicitly forbidden.
        path = urllib.parse.urlsplit(url).path or "/"
        if urllib.parse.urlsplit(url).query:
            path += "?" + urllib.parse.urlsplit(url).query
        return not any(p.match(path) for p in state.wildcard_disallows)

    def _load_robots(self, key: str,
                     state: _HostState) -> urllib.robotparser.RobotFileParser:
        """Fetch and parse one host's robots.txt **using our own User-Agent**.

        RobotFileParser.read() would do this for us, but it requests the file
        with Python's default urllib User-Agent. A site that rejects unidentified
        clients answers 403, and read() turns that into disallow_all — so the
        monitor concludes it is banned from the whole site when in fact it simply
        never introduced itself. Bursa behaves exactly this way.

        So we make the request ourselves, with the same identity we use for
        everything else, and hand the text to parse(). The URL is built from the
        host being visited, not from base_url: fetching Bursa's rules and
        applying them to someone else's site is not obeying robots.txt.
        """
        url = key + "/robots.txt"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_ROBOTS_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Refused the rules themselves, to a client that did identify
                # itself. Read that as "not welcome" and stop.
                state.robots_error = (
                    f"robots.txt at {key} returned HTTP {exc.code} to an identified "
                    f"client — treating that site as closed to us and stopping."
                )
                raise BlockedError(state.robots_error) from exc
            if 400 <= exc.code < 500:
                body = ""  # No robots.txt published: no restrictions to honour.
            else:
                state.robots_error = (
                    f"robots.txt at {key} returned HTTP {exc.code} — refusing to fetch"
                )
                raise BursaClientError(state.robots_error) from exc
        except Exception as exc:
            # Cannot read the rules at all: assume we are not welcome rather
            # than assuming we are.
            state.robots_error = (
                f"Could not read robots.txt at {key} ({exc}) — refusing to fetch"
            )
            raise BursaClientError(state.robots_error) from exc
        finally:
            # Reading robots.txt was itself a request to this host, so start its
            # clock here. With many hosts this is the one burst left: every host
            # would otherwise get robots.txt and its first page back to back.
            state.last_request_at = time.monotonic()

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        state.wildcard_disallows = _wildcard_disallow_patterns(body, self.user_agent)

        # If the site asks for a slower pace than ours, take theirs — for this
        # host only, and only upwards. A site cannot talk us below our own floor.
        try:
            stated = parser.crawl_delay(self.user_agent)
        except Exception:
            stated = None
        if stated:
            stated = float(stated)
            if stated > MAX_HONOURED_DELAY:
                state.robots_error = (
                    f"{key} asks for a Crawl-delay of {stated:g}s, longer than the "
                    f"{MAX_HONOURED_DELAY:g}s a run will wait — skipping this host "
                    f"rather than reading it faster than it asked."
                )
                raise BursaClientError(state.robots_error)
            state.delay = max(state.delay, stated)
        return parser

    # ---- pacing ----------------------------------------------------------
    def _wait_turn(self, state: _HostState) -> None:
        # Per host. A gap owed to one site is no reason to make another site's
        # poll late: Crawl-delay is a per-server request, and coupling them
        # would queue every host behind the slowest one. Total volume is bounded
        # by the run-wide request cap, not by this.
        elapsed = time.monotonic() - state.last_request_at
        if state.last_request_at and elapsed < state.delay:
            time.sleep(state.delay - elapsed)

    def absolute(self, path_or_url: str, base: str | None = None) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        # `base` resolves a relative link against the page it was found on. With
        # more than one host in play, defaulting to base_url would quietly move
        # another site's link onto Bursa.
        root = (base or self.base_url).rstrip("/")
        return urllib.parse.urljoin(root + "/", path_or_url.lstrip("/"))

    # ---- fetch -----------------------------------------------------------
    def fetch(self, path_or_url: str, params: dict | None = None,
              *, cache_name: str | None = None, base: str | None = None) -> bytes:
        url = self.absolute(path_or_url, base)
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"

        # Derived from the resolved URL, not from base_url: a protocol-relative
        # href ('//other.example/x.pdf') lands on another host, and every rule
        # below has to be that host's.
        key = _host_key(url)
        if not key:
            raise BursaClientError(
                f"Refusing {url}: only http(s) URLs with a host are fetched"
            )
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(
                f"Per-run request cap of {self.max_requests} reached — stopping. "
                f"Narrow the window or raise the cap deliberately."
            )
        state = self._host_state(key)
        if state.requests_made >= self.max_requests_per_host:
            raise HostBudgetExceeded(
                f"Per-host cap of {self.max_requests_per_host} requests reached for "
                f"{key} — leaving the rest of the run for the other hosts."
            )
        if not self._robots_allows(url):
            raise BursaClientError(f"robots.txt disallows fetching {url}")

        self._wait_turn(state)
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        })

        self.requests_made += 1
        state.requests_made += 1
        state.last_request_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                raise BlockedError(
                    f"{key} returned HTTP {exc.code} for {url}. Stopping this run."
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

    def absolute(self, path_or_url: str, base: str | None = None) -> str:
        return path_or_url

    def fetch(self, path_or_url: str, params: dict | None = None,
              *, cache_name: str | None = None, base: str | None = None) -> bytes:
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
    """host_digest_tail.

    The host goes first because the old name sanitised the whole URL and kept
    the last 120 characters — and the host is at the front, so it was the first
    thing truncation dropped. With one host that could not collide; with a host
    per watched company, two sites serving the same long path would overwrite
    each other's saved response, and a saved response is provenance.
    The digest is what actually guarantees uniqueness; the tail is for humans.
    """
    parts = urllib.parse.urlsplit(url)
    host = _sanitise(parts.hostname or "nohost")[:40]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    tail = _sanitise(parts.path + (("?" + parts.query) if parts.query else ""))[-80:]
    return f"{host}_{digest}_{tail or 'response'}"


def _sanitise(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)

"""Polite HTTP helper.

Non-negotiable rules encoded here:
  * robots.txt is fetched and obeyed for every host before any page request.
  * A single, honest User-Agent identifying this tool. No browser spoofing.
  * A minimum delay between requests to the same host.
  * No cookie jars, no login, no captcha handling, no proxy rotation.
If a site does not want to be read by a program, this module does not read it.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

USER_AGENT = "porsche-deal-hunter/0.1 (private inventory research tool; contact: operator)"
MIN_DELAY_SECONDS = 5.0

_last_request: dict[str, float] = {}
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


class NotAllowed(RuntimeError):
    """Raised when robots.txt disallows the URL, or the domain is not opted in."""


def _host(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def robots_allows(url: str, timeout: int = 15) -> bool:
    """Fetch robots.txt WITH OUR REAL User-Agent, then check it.

    ``RobotFileParser.read()`` fetches robots.txt as ``Python-urllib``, and a
    number of dealer CDNs block or vary their response for that agent -- which
    produced false "disallowed" results and hid sites we are in fact permitted
    to read. We fetch robots.txt as the same UA we crawl with and parse that.

    Fail-closed on anything that signals "not welcome": a 401/403/429 or a 5xx
    on robots.txt, a timeout, or a refused connection all mean off-limits. A 404
    (no robots.txt) means the site published no rules -> allowed.
    """
    base = _host(url)
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = base + "/robots.txt"
        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(1_000_000)
                charset = resp.headers.get_content_charset() or "utf-8"
            rp.parse(raw.decode(charset, errors="replace").splitlines())
            _robots_cache[base] = rp
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                rp.parse([])           # no robots.txt -> nothing disallowed
                _robots_cache[base] = rp
            else:
                _robots_cache[base] = None   # 401/403/429/5xx -> off limits
        except Exception:
            _robots_cache[base] = None       # timeout, refused, TLS error -> off limits
    rp = _robots_cache[base]
    if rp is None:
        return False
    return rp.can_fetch(USER_AGENT, url)


def _throttle(url: str) -> None:
    base = _host(url)
    last = _last_request.get(base)
    if last is not None:
        wait = MIN_DELAY_SECONDS - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request[base] = time.time()


def fetch(url: str, timeout: int = 25, check_robots: bool = True,
          max_bytes: int = 4_000_000) -> str:
    """Fetch a URL as text after a robots.txt check and a per-host delay."""
    if check_robots and not robots_allows(url):
        raise NotAllowed(
            f"robots.txt for {_host(url)} does not permit {USER_AGENT} to fetch {url}. "
            "Use manual entry for this listing instead."
        )
    _throttle(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")

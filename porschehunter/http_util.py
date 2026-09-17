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
    base = _host(url)
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            # Could not read robots.txt -> treat the site as off limits.
            _robots_cache[base] = None
            return False
        _robots_cache[base] = rp
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

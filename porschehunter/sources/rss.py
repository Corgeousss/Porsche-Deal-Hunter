"""RSS/Atom saved-search reader.

Intended for feeds a site publishes for readers -- Craigslist search pages
expose one. It is OFF by default because Craigslist's terms endorse RSS
readers but prohibit bulk harvesting; a small number of personal saved
searches polled slowly is the only use this connector supports.

A feed gives title, link, price if the title carries one, and a post date.
It gives no VIN, no mileage and no photos, so every item lands with those
fields recorded as missing, for the operator to fill in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import db as _db
from .. import generations as _gens
from .. import http_util
from .base import IngestResult

FEEDS_PATH = Path("data/feeds.txt")

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_PRICE_RE = re.compile(r"\$\s?(\d[\d,]*)")


def load_feeds(path: Path = FEEDS_PATH) -> list[tuple[str, str]]:
    """Each line: <source_key> <feed_url>   (# comments allowed)."""
    if not path.exists():
        return []
    feeds = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1:
            feeds.append(("craigslist_rss", parts[0]))
        else:
            feeds.append((parts[0], parts[1]))
    return feeds


def parse_feed(xml_text: str) -> list[dict]:
    """Parse RSS 1.0/2.0 and Atom into {title, url, published}."""
    root = ET.fromstring(xml_text)
    items: list[dict] = []

    def text(el, *paths):
        for p in paths:
            found = el.find(p, _NS)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return None

    candidates = (root.findall(".//item") + root.findall(".//rss:item", _NS)
                  + root.findall(".//atom:entry", _NS))
    for el in candidates:
        title = text(el, "title", "rss:title", "atom:title")
        link = text(el, "link", "rss:link", "atom:link")
        if link is None:
            a = el.find("atom:link", _NS)
            if a is not None:
                link = a.get("href")
        if not link:
            continue
        published = text(el, "pubDate", "dc:date", "atom:updated", "atom:published")
        items.append({"title": title, "url": link.strip(), "published": published})
    return items


def price_from_title(title: str | None) -> float | None:
    if not title:
        return None
    m = _PRICE_RE.search(title)
    return float(m.group(1).replace(",", "")) if m else None


def looks_like_911(title: str | None) -> bool:
    if not title:
        return False
    t = title.lower()
    if "porsche" not in t and "911" not in t:
        return False
    # Exclude obvious non-911 Porsches that still say "Porsche".
    for other in ("cayenne", "macan", "panamera", "boxster", "cayman", "taycan", "944", "928", "924"):
        if other in t and "911" not in t:
            return False
    return "911" in t or "carrera" in t or "porsche" in t


def ingest(conn, feeds: list[tuple[str, str]] | None = None,
           porsche_only: bool = True) -> IngestResult:
    feeds = load_feeds() if feeds is None else feeds
    res = IngestResult(source_key="craigslist_rss")
    if not feeds:
        res.status = "skipped"
        res.message = f"No feeds configured. Add saved-search feed URLs to {FEEDS_PATH}."
        return res

    for source_key, url in feeds:
        try:
            xml_text = http_util.fetch(url)
            items = parse_feed(xml_text)
        except Exception as exc:
            res.status = "error"
            res.message += f"{url}: {type(exc).__name__}: {exc}\n"
            continue

        for it in items:
            res.seen += 1
            if porsche_only and not looks_like_911(it["title"]):
                continue
            year = _gens.parse_year(it["title"])
            gen, _amb = _gens.from_year(year)
            listing_id, created = _db.upsert_listing(conn, {
                "source_key": source_key,
                "url": it["url"],
                "title": it["title"],
                "year": year,
                "generation": gen,
                "variant": _gens.normalize_variant(it["title"]),
                "body_style": _gens.normalize_body_style(it["title"]),
                "transmission": _gens.normalize_transmission(it["title"]),
                "price": price_from_title(it["title"]),
                "listing_type": "fixed",
                "seller_type": "unknown",
                "data_source_note": f"RSS feed item ({url})",
            }, raw=it)
            res.new += int(created)
            res.updated += int(not created)
            res.listing_ids.append(listing_id)
    return res

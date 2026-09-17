"""Dealer websites that publish schema.org Vehicle data.

Many dealer platforms embed a JSON-LD <script type="application/ld+json">
block describing the car -- it exists so search engines can read it. This
connector reads exactly that block and nothing else.

Guardrails:
  * The domain must be on the operator's allowlist (data/allowed_domains.txt).
  * robots.txt is checked on every request (see http_util).
  * One page at a time, rate limited, honest User-Agent.
  * If the page has no JSON-LD, we stop. We do not fall back to scraping HTML.
"""

from __future__ import annotations

import gzip
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import db as _db
from .. import generations as _gens
from .. import http_util
from .. import model_guard as _guard
from .. import vin as _vin
from .base import IngestResult

# Re-exported so callers (and tests) can reach the shared 911 helpers here too.
is_911 = _guard.is_911
is_non_911_model = _guard.is_non_911_model
looks_like_911_url = _guard.looks_like_911_url

ALLOWLIST_PATH = Path("data/allowed_domains.txt")

_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def load_allowlist(path: Path = ALLOWLIST_PATH) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def domain_allowed(url: str, allowlist: set[str] | None = None) -> bool:
    allowlist = load_allowlist() if allowlist is None else allowlist
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    host = host.split(":")[0]
    return any(host == d or host.endswith("." + d) for d in allowlist)


# Some dealer platforms emit `"image": "[\"a.jpg\",\"b.jpg\"]"` -- a JSON array
# serialised as a string, but with the inner quotes left unescaped, which makes
# the whole block invalid JSON. This narrowly unwraps that one broken shape so
# the vehicle is not lost. URLs never contain `]`, so the non-greedy match is safe.
_STRINGIFIED_ARRAY_RE = re.compile(r'("image"\s*:\s*)"(\[.*?\])"', re.DOTALL)


def _repair_jsonld(text: str) -> str:
    return _STRINGIFIED_ARRAY_RE.sub(r"\1\2", text)


def extract_jsonld(page_html: str) -> list[dict]:
    """Return every JSON-LD object found in the page, flattened."""
    objects: list[dict] = []
    for raw in _SCRIPT_RE.findall(page_html):
        text = html.unescape(raw).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                data = json.loads(_repair_jsonld(text))
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                objects.append(item)
                if "@graph" in item:
                    stack.append(item["@graph"])
    return objects


def _type_of(obj: dict) -> list[str]:
    t = obj.get("@type") or obj.get("type") or []
    return [t] if isinstance(t, str) else list(t)


MIN_PLAUSIBLE_PRICE = 1000.0


def _plausible_price(price: float | None) -> float | None:
    """Reject placeholder prices ($0, $1, "call for price" encoded as 0)."""
    if price is None or price < MIN_PLAUSIBLE_PRICE:
        return None
    return price


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _num(value.get("value") or value.get("@value"))
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(m.group(0).replace(",", "")) if m else None


_VEHICLE_TYPES = ("Vehicle", "Car", "Product")
_TYPE_RANK = {"Vehicle": 0, "Car": 1, "Product": 2}


def parse_vehicle(objects: list[dict], url: str) -> dict | None:
    """Map schema.org Vehicle/Car/Product objects onto our listing fields.

    Real dealer pages routinely split one car across several JSON-LD objects:
    a `Product` carrying the price, a `Car`, and a `Vehicle` carrying the VIN,
    mileage and body style, with the seller address complete in only one of
    them. Picking the first match alone silently drops fields, so we parse every
    candidate and merge them, letting the richer type (Vehicle > Car > Product)
    win each field.
    """
    candidates = [o for o in objects
                  if any(t in _VEHICLE_TYPES for t in _type_of(o))]
    if not candidates:
        return None
    candidates.sort(key=lambda o: min(
        (_TYPE_RANK.get(t, 3) for t in _type_of(o)), default=3))

    parts = [_extract_one(o, url) for o in candidates]
    # Seed from the top-ranked candidate so every field key is present (some may
    # be None), then fill gaps from lower-ranked candidates.
    record: dict = dict(parts[0]["record"])
    for p in parts[1:]:
        for k, v in p["record"].items():
            if record.get(k) in (None, "", []) and v not in (None, "", []):
                record[k] = v

    photos: list[str] = next((p["photos"] for p in parts if p["photos"]), [])
    brand = next((p["brand"] for p in parts if p.get("brand")), None)
    gen, ambiguous = _gens.from_year(record.get("year"))
    record["generation"] = gen
    return {
        "record": record,
        "photos": photos,
        "brand": brand,
        "ambiguous_generation": ambiguous,
        "raw": parts[0]["raw"],
    }


def _extract_one(vehicle: dict, url: str) -> dict:
    """Map a single schema.org Vehicle/Car/Product object onto listing fields."""
    offer = vehicle.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    offer = offer if isinstance(offer, dict) else {}

    brand = vehicle.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    name = vehicle.get("name") or ""

    year = _num(vehicle.get("modelDate") or vehicle.get("productionDate")
                or vehicle.get("vehicleModelDate") or vehicle.get("year")
                or vehicle.get("releaseDate"))
    year = int(year) if year else _gens.parse_year(name)

    model = vehicle.get("model")
    if isinstance(model, dict):
        model = model.get("name")
    descriptor = " ".join(str(x) for x in (name, model, vehicle.get("vehicleConfiguration"),
                                           vehicle.get("trim"), vehicle.get("description"))
                          if x)

    mileage = _num((vehicle.get("mileageFromOdometer") or {}) if isinstance(
        vehicle.get("mileageFromOdometer"), dict) else vehicle.get("mileageFromOdometer"))

    trans = vehicle.get("vehicleTransmission")
    seller = offer.get("seller") or vehicle.get("seller") or {}
    seller_name = seller.get("name") if isinstance(seller, dict) else None
    address = seller.get("address") if isinstance(seller, dict) else None
    if not isinstance(address, dict):
        at = offer.get("availableAtOrFrom")
        address = at.get("address") if isinstance(at, dict) else None
    if not isinstance(address, dict):
        address = {}

    images = vehicle.get("image") or []
    if isinstance(images, str):
        images = [images]
    images = [i.get("url") if isinstance(i, dict) else i for i in images]
    images = [i for i in images if isinstance(i, str)]

    # Key the listing on the detail page we actually fetched, NOT the URL the
    # JSON-LD declares. Some dealer platforms (e.g. Marshall Goldman) point
    # offers.url at a generic inventory landing page, which would collapse every
    # distinct car onto one row. The fetched URL is always the unique VDP.
    listing_url = url

    gen, ambiguous = _gens.from_year(year)
    return {
        "record": {
            "source_key": "dealer_jsonld",
            "url": listing_url,
            "title": name or descriptor.strip() or None,
            "year": year,
            "generation": gen,
            "variant": _gens.normalize_variant(descriptor),
            "body_style": _gens.normalize_body_style(descriptor)
                          or (vehicle.get("bodyType") if isinstance(vehicle.get("bodyType"), str) else None),
            "transmission": _gens.normalize_transmission(trans) or _gens.normalize_transmission(descriptor),
            "drivetrain": vehicle.get("driveWheelConfiguration") if isinstance(
                vehicle.get("driveWheelConfiguration"), str) else None,
            "exterior_color": vehicle.get("color"),
            "interior_color": vehicle.get("vehicleInteriorColor"),
            "mileage": int(mileage) if mileage else None,
            "vin": _vin.normalize(vehicle.get("vehicleIdentificationNumber")
                                  or vehicle.get("serialNumber")
                                  or vehicle.get("mpn")),
            # A 911 is never listed at $0/$1 -- dealers encode "call for price"
            # that way. Store it as unknown rather than a misleading $0.
            "price": _plausible_price(_num(offer.get("price"))),
            "currency": offer.get("priceCurrency") or "USD",
            "listing_type": "fixed",
            "seller_type": "dealer",
            "seller_name": seller_name,
            "seller_city": address.get("addressLocality"),
            "seller_state": address.get("addressRegion"),
            "seller_zip": address.get("postalCode"),
            "data_source_note": "schema.org JSON-LD published by the dealer site",
        },
        "photos": images[:12],
        "brand": brand,
        "ambiguous_generation": ambiguous,
        "raw": vehicle,
    }


def ingest_url(conn, url: str, *, require_porsche: bool = True,
               only_911: bool = False) -> tuple[int | None, bool, dict]:
    report: dict = {"notices": []}
    if not domain_allowed(url):
        raise http_util.NotAllowed(
            f"{urllib.parse.urlparse(url).netloc} is not in {ALLOWLIST_PATH}. "
            "Add the domain only after you have read that site's terms, "
            "or enter the listing manually instead."
        )
    page = http_util.fetch(url)
    objects = extract_jsonld(page)
    if not objects:
        raise ValueError(
            "No JSON-LD found on the page. This connector does not scrape raw "
            "HTML -- enter the listing manually instead."
        )
    parsed = parse_vehicle(objects, url)
    if parsed is None:
        raise ValueError("JSON-LD present but no schema.org Vehicle/Car object in it.")

    rec = parsed["record"]
    brand = (parsed.get("brand") or "") + " " + (rec.get("title") or "")
    if require_porsche and "porsche" not in brand.lower():
        return None, False, {"notices": [f"Skipped: not a Porsche ({brand.strip() or 'unknown'})."],
                             "verdict": "rejected"}

    # Establish the model. Decode the VIN FIRST (authoritative) so a Cayman with
    # a "Carrera"-flavoured title cannot slip in on text alone.
    vin_model = None
    if rec.get("vin"):
        dec = _vin.decode_and_store(conn, rec["vin"], model_year=rec.get("year"))
        vin_model = (dec.get("row") or {}).get("model")
    verdict, reason = _guard.classify_911(
        title=rec.get("title"), variant=rec.get("variant"), vin_model=vin_model)

    if only_911 and verdict == _guard.VERDICT_REJECTED:
        return None, False, {"notices": [f"Rejected (not a 911): {reason}."],
                             "verdict": "rejected"}
    if only_911 and verdict == _guard.VERDICT_QUARANTINE:
        rec = {**rec, "status": "quarantined", "quarantine_reason": reason}
        report["notices"].append(f"Quarantined: {reason}.")

    if parsed["ambiguous_generation"]:
        report["notices"].append(
            f"Model year {rec['year']} spans two generations; '{rec['generation']}' assumed."
        )

    listing_id, created = _db.upsert_listing(conn, rec, raw=parsed["raw"])
    if parsed["photos"]:
        _db.add_photos(conn, listing_id, parsed["photos"])
    report["verdict"] = verdict
    report["missing_fields"] = [r["field"] for r in conn.execute(
        "SELECT field FROM missing_fields WHERE listing_id=?", (listing_id,))]
    return listing_id, created, report


def ingest(conn, urls: list[str], *, only_911: bool = False) -> IngestResult:
    res = IngestResult(source_key="dealer_jsonld")
    for u in urls:
        try:
            lid, created, rep = ingest_url(conn, u, only_911=only_911)
        except (http_util.NotAllowed, ValueError, OSError) as exc:
            res.status = "error"
            res.message += f"{u}: {type(exc).__name__}: {exc}\n"
            continue
        res.seen += 1
        if lid is None:
            res.rejected += int(rep.get("verdict") == "rejected")
            continue
        if rep.get("verdict") == "quarantine":
            res.quarantined += 1
        res.new += int(created)
        res.updated += int(not created)
        res.listing_ids.append(lid)
    return res


# ---------------------------------------------------------------------------
# Sitemap-driven discovery -- a permitted, no-paid-API listing source.
#
# A sitemap is a file a site publishes specifically to tell automated clients
# which pages exist. Using it is the opposite of circumventing access control:
# it is the front door. This still goes through http_util.fetch, so robots.txt
# is enforced on every single request, the domain must be on the operator
# allowlist, and requests are rate limited to one per 5s per host.
# ---------------------------------------------------------------------------

# URL shapes that typically indicate a vehicle detail page.
VEHICLE_URL_HINTS = ("porsche", "911", "/inventory/", "/vehicle/", "/vehicles/",
                     "/used/", "/detail", "/vdp", "/listing")
EXCLUDE_URL_HINTS = ("/blog/", "/news/", "/service/", "/parts/", "/finance",
                     "/about", "/contact", "/staff", "/careers", "/specials")


def sitemap_urls_from_robots(domain: str, timeout: int = 20) -> list[str]:
    """Read Sitemap: directives out of robots.txt. Falls back to /sitemap.xml."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    robots_url = f"{base}/robots.txt"
    found: list[str] = []
    try:
        req = urllib.request.Request(
            robots_url, headers={"User-Agent": http_util.USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(1_000_000).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                found.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return found or [f"{base}/sitemap.xml"]


def _fetch_bytes(url: str, timeout: int = 25, max_bytes: int = 20_000_000) -> bytes:
    if not http_util.robots_allows(url):
        raise http_util.NotAllowed(f"robots.txt disallows {url}")
    http_util._throttle(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": http_util.USER_AGENT,
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes)
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_sitemap(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """Returns (page_urls, nested_sitemap_urls)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [], []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    pages, nested = [], []
    for el in root.findall(".//sm:sitemap/sm:loc", ns) + root.findall(".//sitemap/loc"):
        if el.text:
            nested.append(el.text.strip())
    for el in root.findall(".//sm:url/sm:loc", ns) + root.findall(".//url/loc"):
        if el.text:
            pages.append(el.text.strip())
    return pages, nested


def looks_like_vehicle_url(url: str) -> bool:
    low = url.lower()
    if any(x in low for x in EXCLUDE_URL_HINTS):
        return False
    return any(h in low for h in VEHICLE_URL_HINTS)


def discover_urls(domain: str, max_sitemaps: int = 8,
                  max_urls: int = 500) -> tuple[list[str], list[str]]:
    """Walk a domain's sitemaps and return (candidate_urls, notes)."""
    notes: list[str] = []
    if not domain_allowed(domain if domain.startswith("http") else f"https://{domain}"):
        raise http_util.NotAllowed(
            f"{domain} is not in {ALLOWLIST_PATH}. Add it only after reading "
            f"that site's terms of use.")

    queue = sitemap_urls_from_robots(domain)
    notes.append(f"sitemap entry points: {', '.join(queue[:5])}")
    seen_maps: set[str] = set()
    candidates: list[str] = []

    while queue and len(seen_maps) < max_sitemaps and len(candidates) < max_urls:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            data = _fetch_bytes(sm)
        except Exception as exc:
            notes.append(f"{sm}: {type(exc).__name__}: {exc}")
            continue
        pages, nested = parse_sitemap(data)
        notes.append(f"{sm}: {len(pages)} urls, {len(nested)} nested sitemaps")
        # Prefer nested sitemaps whose name hints at inventory.
        nested.sort(key=lambda u: 0 if looks_like_vehicle_url(u) else 1)
        queue.extend(nested)
        for u in pages:
            if looks_like_vehicle_url(u):
                candidates.append(u)
                if len(candidates) >= max_urls:
                    break

    # Preserve order, drop duplicates.
    deduped = list(dict.fromkeys(candidates))
    notes.append(f"{len(deduped)} candidate vehicle URLs after filtering")
    return deduped, notes


def ingest_domain(conn, domain: str, max_pages: int = 40,
                  max_urls: int = 500, only_911: bool = True) -> IngestResult:
    """Discover and ingest Porsche 911 listings from one allowlisted domain.

    Every request obeys robots.txt and the per-host rate limit. Pages without
    schema.org JSON-LD are skipped, never scraped. When ``only_911`` (the
    default, since this is a 911 tool), candidate URLs are pre-filtered to the
    911 line so the per-host rate limit is not spent fetching Macans and
    Cayennes; a non-911 that slips through the URL filter is dropped after parse.
    """
    res = IngestResult(source_key="dealer_jsonld")
    try:
        candidates, notes = discover_urls(domain, max_urls=max_urls)
    except http_util.NotAllowed as exc:
        res.status = "error"
        res.message = str(exc)
        return res
    res.message = "\n".join(notes) + "\n"

    if only_911 and candidates:
        before = len(candidates)
        filtered = [u for u in candidates if looks_like_911_url(u)]
        # Only narrow when the URLs actually name their model; if none match,
        # the site uses opaque URLs and we fall back to parse-time filtering.
        if filtered:
            candidates = filtered
            res.message += (f"911 URL filter: {len(candidates)} of {before} "
                            f"candidate URLs name a 911\n")

    if not candidates:
        res.status = "skipped"
        res.message += ("No candidate vehicle URLs found in the sitemaps. This "
                        "dealer may not publish one, or may use URL shapes this "
                        "filter does not recognise. Use `add <url>` per listing "
                        "instead.")
        return res

    fetched = 0
    for url in candidates:
        if fetched >= max_pages:
            res.message += (f"stopped after {max_pages} pages "
                            f"({len(candidates) - fetched} candidates left)\n")
            break
        fetched += 1
        try:
            listing_id, created, rep = ingest_url(conn, url, only_911=only_911)
        except http_util.NotAllowed as exc:
            res.message += f"{url}: {exc}\n"
            continue
        except (ValueError, OSError):
            # No JSON-LD, or not reachable. Expected for many pages.
            continue
        res.seen += 1
        if listing_id is None:
            res.rejected += int(rep.get("verdict") == "rejected")
            continue
        if rep.get("verdict") == "quarantine":
            res.quarantined += 1
        res.new += int(created)
        res.updated += int(not created)
        res.listing_ids.append(listing_id)

    res.message += (f"fetched {fetched} pages, {res.seen} parsed as vehicles, "
                    f"{res.new} new Porsche 911 listings, {res.updated} updated, "
                    f"{res.rejected} rejected as non-911, {res.quarantined} quarantined")
    return res

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

import html
import json
import re
import urllib.parse
from pathlib import Path

from .. import db as _db
from .. import generations as _gens
from .. import http_util
from .. import vin as _vin
from .base import IngestResult

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


def extract_jsonld(page_html: str) -> list[dict]:
    """Return every JSON-LD object found in the page, flattened."""
    objects: list[dict] = []
    for raw in _SCRIPT_RE.findall(page_html):
        text = html.unescape(raw).strip()
        try:
            data = json.loads(text)
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


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _num(value.get("value") or value.get("@value"))
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def parse_vehicle(objects: list[dict], url: str) -> dict | None:
    """Map a schema.org Vehicle/Car object onto our listing fields."""
    vehicle = next(
        (o for o in objects if any(t in ("Vehicle", "Car", "Product") for t in _type_of(o))),
        None,
    )
    if vehicle is None:
        return None

    offer = vehicle.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    offer = offer if isinstance(offer, dict) else {}

    brand = vehicle.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    name = vehicle.get("name") or ""

    year = _num(vehicle.get("modelDate") or vehicle.get("productionDate") or vehicle.get("vehicleModelDate"))
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

    gen, ambiguous = _gens.from_year(year)
    return {
        "record": {
            "source_key": "dealer_jsonld",
            "url": offer.get("url") or vehicle.get("url") or url,
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
            "vin": _vin.normalize(vehicle.get("vehicleIdentificationNumber")),
            "price": _num(offer.get("price")),
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


def ingest_url(conn, url: str, *, require_porsche: bool = True) -> tuple[int | None, bool, dict]:
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
        return None, False, {"notices": [f"Skipped: not a Porsche ({brand.strip() or 'unknown'})."]}
    if parsed["ambiguous_generation"]:
        report["notices"].append(
            f"Model year {rec['year']} spans two generations; '{rec['generation']}' assumed."
        )

    listing_id, created = _db.upsert_listing(conn, rec, raw=parsed["raw"])
    if parsed["photos"]:
        _db.add_photos(conn, listing_id, parsed["photos"])
    if rec.get("vin"):
        _vin.decode_and_store(conn, rec["vin"], model_year=rec.get("year"))
    report["missing_fields"] = [r["field"] for r in conn.execute(
        "SELECT field FROM missing_fields WHERE listing_id=?", (listing_id,))]
    return listing_id, created, report


def ingest(conn, urls: list[str]) -> IngestResult:
    res = IngestResult(source_key="dealer_jsonld")
    for u in urls:
        try:
            lid, created, rep = ingest_url(conn, u)
        except (http_util.NotAllowed, ValueError, OSError) as exc:
            res.status = "error"
            res.message += f"{u}: {type(exc).__name__}: {exc}\n"
            continue
        res.seen += 1
        if lid is None:
            continue
        res.new += int(created)
        res.updated += int(not created)
        res.listing_ids.append(lid)
    return res

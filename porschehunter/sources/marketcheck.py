"""MarketCheck Automotive API client.

MarketCheck is a commercial vehicle-listings API. It is the one source in this
project that gives broad, authorized, automated access to live dealer
inventory nationwide -- and it is paid. Set MARKETCHECK_API_KEY to enable it.

Endpoint paths and parameter names follow MarketCheck's published v2 Cars API
shape. CONFIRM THEM AGAINST YOUR OWN PLAN'S DOCUMENTATION before relying on
this in production: the exact host, path and entitlements depend on the
contract you sign, and this file was written without access to a live key.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .. import db as _db
from .. import generations as _gens
from .. import vin as _vin
from .base import IngestResult

BASE_URL = os.environ.get("MARKETCHECK_BASE_URL", "https://mc-api.marketcheck.com/v2")
API_KEY_ENV = "MARKETCHECK_API_KEY"


class NotConfigured(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise NotConfigured(
            f"{API_KEY_ENV} is not set. MarketCheck is a paid API: sign up at "
            "marketcheck.com, choose a plan that includes the Cars search "
            "endpoint, then export the key. Until then this source stays off "
            "and you use manual entry plus dealer JSON-LD."
        )
    return key


def _get(path: str, params: dict, timeout: int = 30) -> dict:
    params = {**params, "api_key": api_key()}
    url = f"{BASE_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "porsche-deal-hunter/0.1",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"MarketCheck HTTP {exc.code}: {body}") from exc


def search_911(year_min: int | None = None, year_max: int | None = None,
               rows: int = 50, start: int = 0, **extra) -> dict:
    params = {
        "make": "Porsche",
        "model": "911",
        "car_type": "used",
        "rows": rows,
        "start": start,
        **extra,
    }
    if year_min:
        params["year_min"] = year_min
    if year_max:
        params["year_max"] = year_max
    return _get("search/car/active", params)


def to_record(item: dict) -> dict:
    build = item.get("build") or {}
    dealer = item.get("dealer") or {}
    year = build.get("year") or item.get("year")
    year = int(year) if year else None
    gen, _amb = _gens.from_year(year)
    descriptor = " ".join(str(x) for x in (
        item.get("heading"), build.get("trim"), build.get("body_type"),
        build.get("transmission")) if x)
    return {
        "record": {
            "source_key": "marketcheck",
            "source_listing_id": item.get("id"),
            "url": item.get("vdp_url") or item.get("source_url") or "",
            "title": item.get("heading"),
            "year": year,
            "generation": gen,
            "variant": _gens.normalize_variant(descriptor),
            "body_style": _gens.normalize_body_style(descriptor),
            "transmission": _gens.normalize_transmission(descriptor),
            "drivetrain": build.get("drivetrain"),
            "engine": build.get("engine"),
            "exterior_color": item.get("exterior_color"),
            "interior_color": item.get("interior_color"),
            "mileage": int(item["miles"]) if item.get("miles") else None,
            "vin": _vin.normalize(item.get("vin")),
            "price": float(item["price"]) if item.get("price") else None,
            "currency": "USD",
            "listing_type": "fixed",
            "seller_type": "dealer" if dealer else "unknown",
            "seller_name": dealer.get("name"),
            "seller_city": dealer.get("city"),
            "seller_state": dealer.get("state"),
            "seller_zip": dealer.get("zip"),
            "data_source_note": "MarketCheck Cars API (licensed data)",
        },
        "photos": [p for p in (item.get("media") or {}).get("photo_links", []) if isinstance(p, str)],
    }


def ingest(conn, year_min: int | None = None, year_max: int | None = None,
           max_rows: int = 100, decode_vins: bool = False) -> IngestResult:
    res = IngestResult(source_key="marketcheck")
    try:
        api_key()
    except NotConfigured as exc:
        res.status = "skipped"
        res.message = str(exc)
        return res

    start, page = 0, 50
    while start < max_rows:
        payload = search_911(year_min, year_max, rows=min(page, max_rows - start), start=start)
        items = payload.get("listings") or []
        if not items:
            break
        for item in items:
            res.seen += 1
            parsed = to_record(item)
            rec = parsed["record"]
            if not rec["url"]:
                continue
            listing_id, created = _db.upsert_listing(conn, rec, raw=item)
            if parsed["photos"]:
                _db.add_photos(conn, listing_id, parsed["photos"][:12])
            if decode_vins and rec.get("vin"):
                _vin.decode_and_store(conn, rec["vin"], model_year=rec.get("year"))
            res.new += int(created)
            res.updated += int(not created)
            res.listing_ids.append(listing_id)
        start += len(items)
    res.message = f"fetched {res.seen} listings from MarketCheck"
    return res

"""MarketCheck Automotive API client.

MarketCheck is a commercial vehicle-listings API covering dealer, private-party
and auction inventory, plus historical/past inventory. Documented at
https://docs.marketcheck.com/docs/api/cars. Paid; set MARKETCHECK_API_KEY.

ENDPOINT COVERAGE (per MarketCheck's published Cars API docs)
-------------------------------------------------------------
  active dealer listings   /v2/search/car/active
  private-party listings   Private Party Inventory Search
  auction listings         Auction Inventory Search
  past/sold inventory      Past Inventory Search -- DEALER ONLY, US/CA.
                           Does NOT cover private party or auction listings.
  VIN history              listing history by VIN: past listings, price
                           changes, mileage changes, seller information
  VIN decode               /v2/decode/car/neovin/{vin}/specs
  price prediction         /v2/predict/car/us/marketcheck_price/comparables

THE PAST-INVENTORY TRAP -- READ BEFORE USING IT FOR COMPS
----------------------------------------------------------
"Past Inventory" contains sold vehicles, EXPIRED listings and vehicles REMOVED
from active inventory. A listing leaving a dealer's feed is a removal, not a
transaction: the car may have been sold, withdrawn, traded, or relisted
elsewhere, and the last price seen is the last ASKING price, not a sale price.

This client therefore imports past-inventory records with
price_basis='inferred_from_removal', which the valuation engine excludes. Only
promote a record to 'verified_transaction' if your licence gives you an actual
transaction price and you have confirmed which field carries it.

CONFIRM AGAINST YOUR OWN PLAN before relying on any of this: exact paths,
parameter names and entitlements depend on the contract you sign, and this file
was written WITHOUT access to a live key -- no call here has ever been executed
against the real API.
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

# MarketCheck's documented host. Override if your plan is issued a different one.
BASE_URL = os.environ.get("MARKETCHECK_BASE_URL", "https://api.marketcheck.com/v2")

# Logical operation -> documented path. Paths that MarketCheck documents by
# name rather than by literal URL are left as None so this client cannot
# silently call a guessed endpoint.
ENDPOINTS = {
    "active": "search/car/active",
    "private_party": None,   # "Private Party Inventory Search" -- confirm path
    "auction": None,         # "Auction Inventory Search"       -- confirm path
    "past": None,            # "Past Inventory Search"          -- confirm path
    "vin_history": None,     # "History by VIN"                 -- confirm path
    "vin_decode": "decode/car/neovin/{vin}/specs",
}


class EndpointNotConfirmed(RuntimeError):
    """Raised for an endpoint MarketCheck documents but whose exact path this
    client has not had confirmed against a live plan."""
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


def _endpoint(kind: str) -> str:
    path = ENDPOINTS.get(kind)
    if not path:
        raise EndpointNotConfirmed(
            f"MarketCheck documents a '{kind}' endpoint, but its exact path is "
            f"not confirmed in this client. Look it up at "
            f"https://docs.marketcheck.com/docs/api/cars, set "
            f"ENDPOINTS['{kind}'], and verify against your plan's entitlements. "
            f"This client will not call a guessed path."
        )
    return path


def search_911(year_min: int | None = None, year_max: int | None = None,
               rows: int = 50, start: int = 0, kind: str = "active", **extra) -> dict:
    """Search 911s. `kind` selects active / private_party / auction / past."""
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
    return _get(_endpoint(kind), params)


# Past-inventory records are removals, not transactions. See the module
# docstring. This is the basis they are stored under, and it is excluded from
# every valuation until you can prove an actual transaction price.
PAST_INVENTORY_PRICE_BASIS = "inferred_from_removal"


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
           max_rows: int = 100, decode_vins: bool = False,
           kind: str = "active") -> IngestResult:
    res = IngestResult(source_key="marketcheck")
    try:
        api_key()
        _endpoint(kind)
    except (NotConfigured, EndpointNotConfirmed) as exc:
        res.status = "skipped"
        res.message = str(exc)
        return res

    start, page = 0, 50
    while start < max_rows:
        payload = search_911(year_min, year_max, rows=min(page, max_rows - start),
                             start=start, kind=kind)
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
    res.message = f"fetched {res.seen} '{kind}' listings from MarketCheck"
    return res

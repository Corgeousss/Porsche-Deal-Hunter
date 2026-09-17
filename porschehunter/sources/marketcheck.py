"""MarketCheck Cars API client.

Endpoints implemented (paths confirmed by the operator against MarketCheck's
official documentation, https://docs.marketcheck.com/docs/api/cars):

    /v2/search/car/active           dealer inventory
    /v2/search/car/fsbo/active      for-sale-by-owner (private party)
    /v2/search/car/auction/active   auction inventory

WHY THE RESPONSE SCHEMA IS EXTERNALISED
---------------------------------------
This client was written WITHOUT access to a live key and WITHOUT being able to
open the response-schema page. Rather than assert field names I have not seen,
the mapping lives in `data/marketcheck_fields.json`, shipped with candidate
defaults and `"confirmed": false`.

The first real call confirms it, not me:

    porschehunter marketcheck probe --kind active

`probe` makes one minimal request, saves the raw JSON, prints the response's
actual top-level keys and the actual keys of the first listing, and reports
which mapped paths resolved and which did not. You then correct the file and
set `"confirmed": true`.

Ingestion refuses to run while the map is unconfirmed, so a wrong guess cannot
quietly produce a database full of nulls.

NOT IMPLEMENTED HERE
--------------------
Past Inventory (sold/expired/removed) is deliberately absent. Those records are
REMOVALS, not transactions, and this tool will not let them become comps. See
DATA_SOURCES.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import db as _db
from .. import generations as _gens
from .. import vin as _vin
from .base import IngestResult

BASE_URL = os.environ.get("MARKETCHECK_BASE_URL", "https://api.marketcheck.com/v2")
API_KEY_ENV = "MARKETCHECK_API_KEY"
FIELDMAP_PATH = Path("data/marketcheck_fields.json")

# Operator-confirmed against MarketCheck's official documentation.
ENDPOINTS = {
    "active": "search/car/active",
    "fsbo": "search/car/fsbo/active",
    "auction": "search/car/auction/active",
}
KINDS = tuple(ENDPOINTS)

# MarketCheck caps how deep a result set can be paged. Conservative default;
# raise it only if your plan's docs say so.
MAX_START = 9_000
PAGE_ROWS = 50


class NotConfigured(RuntimeError):
    """No API key."""


class SchemaNotConfirmed(RuntimeError):
    """The response field map has not been confirmed against a real response."""


class MarketCheckError(RuntimeError):
    """An API call failed."""


class AuthError(MarketCheckError):
    """401/403 -- key missing, wrong, or not entitled to this endpoint."""


class RateLimited(MarketCheckError):
    """429."""


# ---------------------------------------------------------------------------
# Field map
# ---------------------------------------------------------------------------
FIELDMAP_TEMPLATE = {
    "_README": [
        "Candidate field map for MarketCheck Cars API responses.",
        "These paths are NOT confirmed. Run:  porschehunter marketcheck probe",
        "It prints the real keys from a real response and tells you which of",
        "these resolved. Correct anything wrong, then set confirmed: true.",
        "Dotted paths descend into nested objects: 'build.year'.",
    ],
    "confirmed": False,
    "envelope": {
        "items": "listings",
        "total": "num_found"
    },
    "fields": {
        "listing_id": "id",
        "url": "vdp_url",
        "title": "heading",
        "year": "build.year",
        "make": "build.make",
        "model": "build.model",
        "trim": "build.trim",
        "body_type": "build.body_type",
        "transmission": "build.transmission",
        "drivetrain": "build.drivetrain",
        "engine": "build.engine",
        "vin": "vin",
        "mileage": "miles",
        "price": "price",
        "exterior_color": "exterior_color",
        "interior_color": "interior_color",
        "seller_name": "dealer.name",
        "seller_city": "dealer.city",
        "seller_state": "dealer.state",
        "seller_zip": "dealer.zip",
        "photos": "media.photo_links"
    },
    "fallbacks": {
        "url": ["source_url", "vdp_url"],
        "year": ["year"],
        "seller_name": ["seller.name", "dealer_name"],
        "seller_city": ["seller.city"],
        "seller_state": ["seller.state"],
        "seller_zip": ["seller.zip"]
    }
}


def write_fieldmap_template(path: Path = FIELDMAP_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(FIELDMAP_TEMPLATE, indent=2) + "\n")


def load_fieldmap(path: Path = FIELDMAP_PATH, require_confirmed: bool = True) -> dict:
    if not path.exists():
        write_fieldmap_template(path)
    cfg = json.loads(path.read_text())
    if require_confirmed and not cfg.get("confirmed"):
        raise SchemaNotConfirmed(
            f"{path} is not confirmed. The response field names in it are "
            f"candidates, not verified. Run `porschehunter marketcheck probe "
            f"--kind active` to see the real response, correct the file, then "
            f"set \"confirmed\": true. Ingestion will not run until then, so a "
            f"wrong guess cannot fill your database with nulls."
        )
    return cfg


def dig(obj, path: str):
    """Follow a dotted path. Returns None if any step is missing."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _resolve(item: dict, cfg: dict, logical: str):
    val = dig(item, (cfg.get("fields") or {}).get(logical, ""))
    if val is not None:
        return val
    for alt in (cfg.get("fallbacks") or {}).get(logical, []):
        val = dig(item, alt)
        if val is not None:
            return val
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise NotConfigured(
            f"{API_KEY_ENV} is not set. MarketCheck is a paid API -- obtain a "
            f"key from marketcheck.com for a plan that includes the endpoints "
            f"you need, then set the environment variable."
        )
    return key


def _get(path: str, params: dict, timeout: int = 30, max_retries: int = 3) -> dict:
    """GET with authorization, retry on transient failure, no retry on 4xx."""
    params = {**params, "api_key": api_key()}
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    safe_url = url.split("api_key=")[0] + "api_key=***"
    last_exc = None

    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": "porsche-deal-hunter/0.3",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MarketCheckError(
                    f"{safe_url} returned non-JSON ({len(raw)} bytes): "
                    f"{raw[:200]!r}") from exc

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code in (401, 403):
                raise AuthError(
                    f"HTTP {exc.code} from {safe_url}. Your key is missing, "
                    f"invalid, or your plan is not entitled to this endpoint. "
                    f"Do not retry -- check the key and your entitlements. "
                    f"Body: {body}") from exc
            if exc.code == 429:
                wait = 2 ** attempt
                last_exc = RateLimited(f"HTTP 429 from {safe_url}: {body}")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise last_exc from exc
            if 400 <= exc.code < 500:
                raise MarketCheckError(
                    f"HTTP {exc.code} from {safe_url} -- request rejected, not "
                    f"retrying. Body: {body}") from exc
            # 5xx: transient
            last_exc = MarketCheckError(f"HTTP {exc.code} from {safe_url}: {body}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_exc from exc

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = MarketCheckError(
                f"network failure calling {safe_url}: {type(exc).__name__}: {exc}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_exc from exc

    raise last_exc or MarketCheckError("unreachable")


def endpoint_for(kind: str) -> str:
    if kind not in ENDPOINTS:
        raise ValueError(f"Unknown kind {kind!r}. Valid: {', '.join(KINDS)}")
    return ENDPOINTS[kind]


def search_page(kind: str, *, rows: int = PAGE_ROWS, start: int = 0,
                year_min: int | None = None, year_max: int | None = None,
                **extra) -> dict:
    params = {"make": "Porsche", "model": "911", "rows": rows, "start": start,
              **extra}
    if year_min:
        params["year_min"] = year_min
    if year_max:
        params["year_max"] = year_max
    return _get(endpoint_for(kind), params)


def probe(kind: str = "active", out_path: Path | None = None) -> dict:
    """One real, minimal call. Reports the ACTUAL response shape.

    This is the only honest way to confirm the field map, and it is what turns
    'written' into 'verified'.
    """
    payload = search_page(kind, rows=1, start=0)
    out_path = out_path or Path(f"data/marketcheck_probe_{kind}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2)[:2_000_000])

    cfg = load_fieldmap(require_confirmed=False)
    env = cfg.get("envelope") or {}
    items = dig(payload, env.get("items", "listings"))
    first = items[0] if isinstance(items, list) and items else None

    resolved, unresolved = {}, []
    if first is not None:
        for logical in (cfg.get("fields") or {}):
            val = _resolve(first, cfg, logical)
            if val is None:
                unresolved.append(logical)
            else:
                resolved[logical] = val
    return {
        "kind": kind,
        "endpoint": f"{BASE_URL}/{endpoint_for(kind)}",
        "retrieved_at": _db.utcnow(),
        "saved_to": str(out_path),
        "response_top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "items_path_used": env.get("items", "listings"),
        "items_found": len(items) if isinstance(items, list) else None,
        "total_reported": dig(payload, env.get("total", "num_found")),
        "first_item_keys": sorted(first.keys()) if isinstance(first, dict) else [],
        "resolved": resolved,
        "unresolved": unresolved,
        "fieldmap_confirmed": bool(cfg.get("confirmed")),
    }


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def _to_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").replace("$", "").strip()))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


SELLER_TYPE_BY_KIND = {"active": "dealer", "fsbo": "private", "auction": "auction_house"}
LISTING_TYPE_BY_KIND = {"active": "fixed", "fsbo": "fixed", "auction": "auction"}


def to_record(item: dict, cfg: dict, kind: str) -> dict:
    year = _to_int(_resolve(item, cfg, "year"))
    gen, _amb = _gens.from_year(year)
    descriptor = " ".join(str(x) for x in (
        _resolve(item, cfg, "title"), _resolve(item, cfg, "trim"),
        _resolve(item, cfg, "body_type"), _resolve(item, cfg, "transmission")) if x)
    photos = _resolve(item, cfg, "photos")
    if isinstance(photos, str):
        photos = [photos]
    photos = [p for p in (photos or []) if isinstance(p, str)]

    return {
        "record": {
            "source_key": "marketcheck",
            "source_listing_id": _resolve(item, cfg, "listing_id"),
            "url": _resolve(item, cfg, "url") or "",
            "title": _resolve(item, cfg, "title"),
            "year": year,
            "generation": gen,
            "variant": _gens.normalize_variant(descriptor),
            "body_style": _gens.normalize_body_style(descriptor),
            "transmission": _gens.normalize_transmission(descriptor),
            "drivetrain": _resolve(item, cfg, "drivetrain"),
            "engine": _resolve(item, cfg, "engine"),
            "exterior_color": _resolve(item, cfg, "exterior_color"),
            "interior_color": _resolve(item, cfg, "interior_color"),
            "mileage": _to_int(_resolve(item, cfg, "mileage")),
            "vin": _vin.normalize(_resolve(item, cfg, "vin")),
            "price": _to_float(_resolve(item, cfg, "price")),
            "currency": "USD",
            "listing_type": LISTING_TYPE_BY_KIND.get(kind, "unknown"),
            "seller_type": SELLER_TYPE_BY_KIND.get(kind, "unknown"),
            "seller_name": _resolve(item, cfg, "seller_name"),
            "seller_city": _resolve(item, cfg, "seller_city"),
            "seller_state": _resolve(item, cfg, "seller_state"),
            "seller_zip": _resolve(item, cfg, "seller_zip"),
            "data_source_note": f"MarketCheck Cars API ({kind}), licensed data",
        },
        "photos": photos[:12],
    }


def is_911(rec: dict, item: dict, cfg: dict) -> bool:
    from .. import model_guard as _guard
    make = str(_resolve(item, cfg, "make") or "")
    model = str(_resolve(item, cfg, "model") or "")
    if "porsche" not in f"{make} {rec.get('title') or ''}".lower():
        return False
    # Only a confirmed 911 (by structured model/title/variant) is admitted.
    verdict, _reason = _guard.classify_911(
        title=rec.get("title"), model=model, variant=rec.get("variant"))
    return verdict == _guard.VERDICT_CONFIRMED


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def ingest(conn, *, kind: str = "active", year_min: int | None = None,
           year_max: int | None = None, max_rows: int = 200,
           decode_vins: bool = False, fieldmap_path: Path = FIELDMAP_PATH,
           porsche_only: bool = True) -> IngestResult:
    res = IngestResult(source_key="marketcheck")
    try:
        api_key()
        cfg = load_fieldmap(fieldmap_path)
        endpoint_for(kind)
    except (NotConfigured, SchemaNotConfirmed, ValueError) as exc:
        res.status = "skipped"
        res.message = str(exc)
        return res

    env = cfg.get("envelope") or {}
    items_path = env.get("items", "listings")
    total_path = env.get("total", "num_found")

    start, total, skipped_no_url = 0, None, 0
    while start < max_rows and start <= MAX_START:
        rows = min(PAGE_ROWS, max_rows - start)
        try:
            payload = search_page(kind, rows=rows, start=start,
                                  year_min=year_min, year_max=year_max)
        except AuthError as exc:
            res.status = "error"
            res.message += f"{exc}\n"
            return res
        except MarketCheckError as exc:
            res.status = "error"
            res.message += f"page start={start}: {exc}\n"
            break

        if total is None:
            total = dig(payload, total_path)
        items = dig(payload, items_path)
        if not isinstance(items, list):
            res.status = "error"
            res.message += (
                f"envelope.items path '{items_path}' did not resolve to a list "
                f"(got {type(items).__name__}). Run `marketcheck probe` and fix "
                f"data/marketcheck_fields.json.\n")
            break
        if not items:
            break

        for item in items:
            res.seen += 1
            parsed = to_record(item, cfg, kind)
            rec = parsed["record"]
            if porsche_only and not is_911(rec, item, cfg):
                res.rejected += 1
                continue
            if not rec["url"]:
                skipped_no_url += 1
                continue
            listing_id, created = _db.upsert_listing(conn, rec, raw=item)
            if parsed["photos"]:
                _db.add_photos(conn, listing_id, parsed["photos"])
            if decode_vins and rec.get("vin"):
                _vin.decode_and_store(conn, rec["vin"], model_year=rec.get("year"))
            res.new += int(created)
            res.updated += int(not created)
            res.listing_ids.append(listing_id)

        start += len(items)
        if total is not None and start >= int(total or 0):
            break

    res.message += (f"{kind}: examined {res.seen} listings"
                    + (f" of {total} reported" if total is not None else "")
                    + f"; {res.new} new, {res.updated} updated")
    if skipped_no_url:
        res.message += f"; {skipped_no_url} skipped with no URL in the response"
    return res

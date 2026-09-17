"""CLASSIC.COM licensed API adapter.

CLASSIC.COM operates an official API for licensed third parties, documented at
https://support.classic.com/classic.com-api, covering taxonomy, sales history
and comparable-sales functionality. Access is negotiated directly --
datasupport@classic.com -- and is not self-serve.

WHY THIS FILE IS CONFIGURATION-DRIVEN RATHER THAN HARD-CODED
------------------------------------------------------------
This adapter was written without access to the licensed documentation or a
credential. Rather than guess endpoint paths and response field names -- which
would produce code that looks finished and fails on first contact -- the wire
details live in a config file you fill in FROM THE LICENSED DOCS:

    data/classic_com_api.json

The adapter refuses to run until that file exists, is marked `confirmed`, and
a credential is present. There is no fallback, no scraping path, and no
default endpoint. Nothing here touches classic.com until you license it.

THE ONE MAPPING DECISION THAT MATTERS
-------------------------------------
CLASSIC.COM's historical database preserves BOTH completed sales AND removed
listings. Where no sold price was provided it retains the "Last Asking" price
-- the final price the car was listed at, which is NOT a transaction.

So the config must state, explicitly and per field, which response field holds
a verified sold price and which holds an asking price. Records that do not map
to a verified sold price are still imported, but are stored with
price_basis='last_asking' (or 'inferred_from_removal') and are therefore
excluded from every valuation. See porschehunter/comps.py.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import comps as _comps
from .. import db as _db
from .. import generations as _gens
from .. import vin as _vin
from .base import IngestResult

CONFIG_PATH = Path("data/classic_com_api.json")
API_KEY_ENV = "CLASSIC_COM_API_KEY"


class NotLicensed(RuntimeError):
    """Raised when credentials or the licensed endpoint config are absent."""


TEMPLATE = {
    "_README": [
        "Fill this in from the licensed CLASSIC.COM API documentation at",
        "https://support.classic.com/classic.com-api after agreeing terms with",
        "datasupport@classic.com. Set 'confirmed' to true only once you have",
        "checked every value against those docs. Nothing is contacted until then.",
    ],
    "confirmed": False,
    "base_url": "",
    "auth": {
        "style": "",
        "header_name": "",
        "query_param": ""
    },
    "endpoints": {
        "sales_history": "",
        "comparables": "",
        "taxonomy": ""
    },
    "response": {
        "items_path": "",
        "fields": {
            "sale_price": "",
            "sale_date": "",
            "year": "",
            "model": "",
            "trim": "",
            "mileage": "",
            "vin": "",
            "url": "",
            "venue": "",
            "body_style": "",
            "transmission": ""
        },
        "_price_basis_README": [
            "CLASSIC.COM retains a removed listing's final ASKING price when no",
            "sold price was provided. Name the field that distinguishes them.",
            "sold_flag_field: a field that is true/'sold' for completed sales.",
            "asking_price_field: the field holding a last-asking price, if separate.",
            "Records that do not prove a completed sale are stored with",
            "price_basis='last_asking' and excluded from valuations."
        ],
        "sold_flag_field": "",
        "sold_flag_true_values": ["sold", "SOLD", True],
        "asking_price_field": ""
    },
    "licence": {
        "_README": "Record what your agreement actually permits. Used by the audit trail.",
        "internal_use_only": None,
        "may_store_locally": None,
        "may_display_to_third_parties": None,
        "retention_days": None,
        "attribution_required": None
    }
}


def write_template(path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(TEMPLATE, indent=2) + "\n")


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise NotLicensed(
            f"{path} does not exist. Run `porschehunter classic-com init-config` "
            "to write the template, then fill it in from the licensed docs."
        )
    cfg = json.loads(path.read_text())
    if not cfg.get("confirmed"):
        raise NotLicensed(
            f"{path} is not marked confirmed. Fill in base_url, auth, endpoints "
            "and the response field map from the licensed CLASSIC.COM "
            "documentation, then set \"confirmed\": true. This adapter will not "
            "guess endpoint paths."
        )
    missing = [k for k in ("base_url",) if not cfg.get(k)]
    if not (cfg.get("endpoints") or {}).get("sales_history"):
        missing.append("endpoints.sales_history")
    if not (cfg.get("response") or {}).get("items_path"):
        missing.append("response.items_path")
    for f in ("sale_price", "sale_date", "url"):
        if not ((cfg.get("response") or {}).get("fields") or {}).get(f):
            missing.append(f"response.fields.{f}")
    if missing:
        raise NotLicensed(f"{path} is incomplete. Missing: {', '.join(missing)}")
    return cfg


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise NotLicensed(
            f"{API_KEY_ENV} is not set. CLASSIC.COM API access is licensed, not "
            "self-serve: contact datasupport@classic.com to agree terms and "
            "obtain a credential. Until then this adapter stays disabled."
        )
    return key


def preflight(config_path: Path = CONFIG_PATH) -> dict:
    """Report readiness without contacting anything. Safe to call any time."""
    out = {"credential": False, "config": False, "ready": False, "blockers": []}
    try:
        api_key()
        out["credential"] = True
    except NotLicensed as exc:
        out["blockers"].append(str(exc))
    try:
        cfg = load_config(config_path)
        out["config"] = True
        out["licence"] = cfg.get("licence", {})
        unanswered = [k for k, v in (cfg.get("licence") or {}).items()
                      if not k.startswith("_") and v is None]
        if unanswered:
            out["blockers"].append(
                "Licence terms not recorded in config: " + ", ".join(unanswered)
                + ". Confirm these with CLASSIC.COM before relying on the data.")
    except NotLicensed as exc:
        out["blockers"].append(str(exc))
    out["ready"] = out["credential"] and out["config"]
    return out


def _dig(obj, path: str):
    """Follow a dotted path into a nested response payload."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur


def _request(cfg: dict, endpoint_key: str, params: dict, timeout: int = 30) -> tuple[dict, str]:
    """Returns (payload, url_called). Raises on HTTP or network failure."""
    path = cfg["endpoints"][endpoint_key]
    base = cfg["base_url"].rstrip("/")
    auth = cfg.get("auth") or {}
    headers = {"User-Agent": "porsche-deal-hunter/0.2", "Accept": "application/json"}
    key = api_key()

    if auth.get("style") == "header" and auth.get("header_name"):
        headers[auth["header_name"]] = key
    elif auth.get("style") == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif auth.get("style") == "query" and auth.get("query_param"):
        params = {**params, auth["query_param"]: key}
    else:
        raise NotLicensed(
            "auth.style in the config must be one of: header, bearer, query -- "
            "set it from the licensed documentation."
        )

    url = f"{base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), url
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"CLASSIC.COM HTTP {exc.code} for {path}: {body}") from exc


def classify_price_basis(item: dict, cfg: dict) -> tuple[str, float | None]:
    """Decide whether this record is a verified sale or an asking price.

    Returns (price_basis, price). This is the function that keeps asking prices
    out of valuations, so it fails CLOSED: anything it cannot prove is a
    completed sale is treated as an asking price.
    """
    resp = cfg["response"]
    fields = resp["fields"]
    sold_flag_field = resp.get("sold_flag_field") or ""
    true_values = resp.get("sold_flag_true_values") or ["sold", "SOLD", True]

    sale_price = _dig(item, fields.get("sale_price", ""))
    asking_field = resp.get("asking_price_field") or ""
    asking_price = _dig(item, asking_field) if asking_field else None

    def as_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace("$", "").replace(",", "").strip())
        except ValueError:
            return None

    sale_price = as_float(sale_price)
    asking_price = as_float(asking_price)

    if sold_flag_field:
        flag = _dig(item, sold_flag_field)
        norm = [str(t).lower() for t in true_values]
        is_sold = str(flag).lower() in norm or flag is True
        if is_sold and sale_price:
            return "verified_transaction", sale_price
        if not is_sold:
            return "last_asking", (asking_price or sale_price)
        return "unknown", sale_price

    # No sold flag configured: we cannot prove a completed sale.
    if asking_field and asking_price and not sale_price:
        return "last_asking", asking_price
    return "unknown", sale_price


def to_comp(item: dict, cfg: dict) -> dict | None:
    fields = cfg["response"]["fields"]
    basis, price = classify_price_basis(item, cfg)
    if price is None:
        return None
    url = _dig(item, fields.get("url", ""))
    sale_date = _dig(item, fields.get("sale_date", ""))
    if not url or not sale_date:
        return None

    year = _dig(item, fields.get("year", ""))
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = None
    descriptor = " ".join(str(x) for x in (
        _dig(item, fields.get("model", "")), _dig(item, fields.get("trim", "")),
        _dig(item, fields.get("body_style", "")),
        _dig(item, fields.get("transmission", ""))) if x)
    gen, _amb = _gens.from_year(year)
    mileage = _dig(item, fields.get("mileage", ""))
    try:
        mileage = int(float(str(mileage).replace(",", "")))
    except (TypeError, ValueError):
        mileage = None

    return {
        "generation": gen,
        "variant": _gens.normalize_variant(descriptor),
        "year": year,
        "body_style": _gens.normalize_body_style(descriptor),
        "transmission": _gens.normalize_transmission(descriptor),
        "mileage": mileage,
        "sale_price": price,
        "sale_date": str(sale_date)[:10],
        "venue": "classic_com",
        "source_key": "classic_com",
        "source_url": str(url),
        "vin": _vin.normalize(_dig(item, fields.get("vin", ""))),
        "price_basis": basis,
        "permission_basis": "licensed_api",
        "permission_note": "Received under the CLASSIC.COM data licence.",
    }


def ingest_sales_history(conn, params: dict, config_path: Path = CONFIG_PATH,
                         dry_run: bool = False) -> IngestResult:
    """Pull completed-sale records and file them as comps with correct basis."""
    res = IngestResult(source_key="classic_com")
    try:
        cfg = load_config(config_path)
        api_key()
    except NotLicensed as exc:
        res.status = "skipped"
        res.message = str(exc)
        return res

    payload, url_called = _request(cfg, "sales_history", params)
    items = _dig(payload, cfg["response"]["items_path"]) or []
    if not isinstance(items, list):
        res.status = "error"
        res.message = (f"response.items_path '{cfg['response']['items_path']}' did not "
                       f"resolve to a list. Check the field map against the docs.")
        return res

    counts = {"verified_transaction": 0, "last_asking": 0, "unknown": 0}
    for item in items:
        res.seen += 1
        rec = to_comp(item, cfg)
        if rec is None:
            continue
        counts[rec["price_basis"]] = counts.get(rec["price_basis"], 0) + 1
        if dry_run:
            continue
        if not rec["generation"]:
            continue
        try:
            _comps.add_comp(conn, **rec)
            res.new += 1
        except _comps.CompRejected:
            continue

    res.message = (f"called {url_called.split('?')[0]}; "
                   f"{counts['verified_transaction']} verified sales, "
                   f"{counts['last_asking']} asking-price-only records "
                   f"(excluded from valuations), {counts['unknown']} unclassified")
    return res

"""Inventory filter + sort engine for the dashboard.

`query()` returns one enriched dict per listing: every stored column plus
computed fields (days on market, price-reduction total, duplicate-VIN flag,
a cost-side estimate, and the valuation-derived deal numbers). `apply_filters`
and `sort_rows` are pure functions over that list so they can be unit-tested
without a server.

Unknown-handling rules, made explicit because the operator asked for them:

* Categorical filters (generation, variant, transmission, body, drivetrain,
  seller type, listing type, source, title status, ...) EXCLUDE a listing whose
  value is unknown *only when* a concrete value is selected -- selecting a value
  is choosing "a filter that requires a confirmed value". Each categorical
  filter also accepts the literal "unknown" so the operator can deliberately
  keep unknown-valued cars.
* Numeric range filters (price, year, mileage, ...) exclude an unknown-valued
  car only while a bound is set; clear the bound and unknowns return.
* Valuation-dependent filters (resale, net profit, ROI, max bid, capital)
  match only cars that carry a real, comp-backed valuation. An unvalued car is
  never treated as qualifying.
"""

from __future__ import annotations

import datetime as _dt

from . import db as _db
from . import deal as _deal
from . import generations as _gens
from . import geo as _geo


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------
def _parse_ts(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _age_days(ts, now):
    d = _parse_ts(ts)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return (now - d).total_seconds() / 86400.0


def _cost_estimate(conn, row, destination_state):
    """Comp-independent cost-side estimate used by the cost filters.

    These numbers do not need comparable sales -- they are shipping, recon and
    holding costs -- so they are available for every car, valued or not, and are
    labelled as estimates in the UI. Per-car recorded quotes beat the defaults.
    """
    lid = row["id"]
    # Reconditioning: recorded recon rows win; else the repairs override; else
    # the generation default repair budget, plus detailing/photography.
    recon_rows = conn.execute(
        "SELECT amount FROM recon_costs WHERE listing_id=?", (lid,)).fetchall()
    override = _db.get_override(conn, lid)
    if recon_rows:
        recon = sum(float(r["amount"]) for r in recon_rows)
    elif override.get("repairs") is not None:
        recon = float(override["repairs"])
    else:
        rk = _deal.repairs_key_for(row["generation"])
        recon = _db.get_assumption(conn, rk) + _db.get_assumption(
            conn, "detail_and_photography")

    # Shipping: recorded transport quote wins; else the centroid estimate.
    if override.get("transport") is not None:
        shipping = float(override["transport"])
    else:
        miles, _basis = _geo.estimate_road_miles(row["seller_state"], destination_state)
        if miles is None:
            miles = _db.get_assumption(conn, "transport_fallback_miles")
        shipping = (_db.get_assumption(conn, "transport_base")
                    + miles * _db.get_assumption(conn, "transport_per_mile"))

    holding_days = (override.get("days_to_sell")
                    if override.get("days_to_sell") is not None
                    else _db.get_assumption(conn, "days_to_sell"))

    admin = (_db.get_assumption(conn, "title_and_admin")
             + _db.get_assumption(conn, "ppi_cost"))
    price = row["price"]
    capital = (price + recon + shipping + admin) if price is not None else None
    return {
        "recon_estimate": round(recon, 2),
        "shipping_estimate": round(shipping, 2),
        "holding_days_estimate": round(float(holding_days), 1),
        "capital_required_estimate": round(capital, 2) if capital is not None else None,
    }


def _price_history(conn, lid):
    rows = conn.execute(
        "SELECT price, observed_at FROM price_history WHERE listing_id=? "
        "AND price IS NOT NULL ORDER BY observed_at", (lid,)).fetchall()
    prices = [float(r["price"]) for r in rows]
    if len(prices) < 2:
        return 0.0, False
    reduction = prices[0] - prices[-1]
    return (round(reduction, 2), True) if reduction > 0 else (0.0, False)


def enrich(conn, destination_state: str, include_synthetic: bool = False) -> list[dict]:
    """One enriched dict per listing, ready for filtering, sorting and display."""
    now = _dt.datetime.now(_dt.timezone.utc)
    deal_results = _deal.evaluate_all(conn, destination_state=destination_state,
                                      include_synthetic=include_synthetic, store=False)
    by_id = {r["listing"]["id"]: r for r in deal_results}

    # Duplicate-VIN detection across active listings (Task 4F).
    vin_counts: dict[str, int] = {}
    for r in deal_results:
        vin = (r["listing"]["vin"] or "").strip().upper()
        if vin:
            vin_counts[vin] = vin_counts.get(vin, 0) + 1

    out = []
    for r in deal_results:
        L = dict(r["listing"])
        lid = L["id"]
        deal = by_id[lid]
        valued = deal["status"] == "ok"

        reduction, reduced = _price_history(conn, lid)
        photo = conn.execute(
            "SELECT url FROM listing_photos WHERE listing_id=? ORDER BY position LIMIT 1",
            (lid,)).fetchone()
        features = {f["feature_key"]: f["present"] for f in conn.execute(
            "SELECT feature_key, present FROM listing_features WHERE listing_id=?", (lid,))}
        vin = (L["vin"] or "").strip().upper()

        net = deal.get("net_profit")
        total_cost = deal.get("total_cost")
        roi = (net / total_cost) if (valued and net is not None
                                     and total_cost) else None

        v = {
            **L,
            "deal_status": L.get("deal_stage"),          # operator pipeline stage
            "valuation_status": deal["status"],           # ok | insufficient_comps | ...
            "underwriting_status": deal.get("underwriting_status"),
            "meets_threshold": bool(deal.get("meets_threshold")),
            "expected_resale": deal.get("expected_resale") if valued else None,
            "net_profit": net if valued else None,
            "roi": round(roi, 4) if roi is not None else None,
            "max_purchase_price": deal.get("max_purchase_price") if valued else None,
            "total_cost": total_cost if valued else None,
            "days_on_market": (None if _age_days(L["first_seen_at"], now) is None
                               else round(_age_days(L["first_seen_at"], now), 2)),
            "price_reduction_total": reduction,
            "price_reduced": reduced,
            "auction_hours_left": _hours_until(L.get("auction_ends_at"), now),
            "is_duplicate_vin": bool(vin and vin_counts.get(vin, 0) > 1),
            "photo_url": photo["url"] if photo else None,
            "features": features,
            **_cost_estimate(conn, L, destination_state),
        }
        out.append(v)
    return out


def _hours_until(ts, now):
    d = _parse_ts(ts)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return round((d - now).total_seconds() / 3600.0, 2)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cat_ok(value, selected):
    """Categorical match. Empty selection = no filter. Unknown value passes only
    if the operator explicitly included 'unknown'."""
    sel = [str(s).lower() for s in _as_list(selected)]
    if not sel:
        return True
    if value in (None, ""):
        return "unknown" in sel
    return str(value).lower() in sel


def _range_ok(value, lo, hi):
    """Numeric range. If a bound is set and the value is unknown, exclude it
    (the operator asked for a confirmed value); otherwise compare."""
    lo, hi = _num(lo), _num(hi)
    if lo is None and hi is None:
        return True
    val = _num(value)
    if val is None:
        return False
    if lo is not None and val < lo:
        return False
    if hi is not None and val > hi:
        return False
    return True


def _expand_generations(gens):
    out = []
    for g in _as_list(gens):
        out.extend(_gens.expand(g) or [g])
    return out


def matches(v: dict, f: dict) -> bool:
    # --- price & basic specs ------------------------------------------------
    if not _range_ok(v.get("price"), f.get("price_min"), f.get("price_max")):
        return False
    if not _range_ok(v.get("year"), f.get("year_min"), f.get("year_max")):
        return False
    if not _range_ok(v.get("mileage"), f.get("mileage_min"), f.get("mileage_max")):
        return False

    # --- model & generation -------------------------------------------------
    gens = _expand_generations(f.get("generations"))
    if gens:
        gv = v.get("generation")
        if gv in (None, "") or gv not in set(gens):
            # unknown generation only kept if operator included 'unknown'
            if not (gv in (None, "") and "unknown" in [str(x).lower() for x in _as_list(f.get("generations"))]):
                return False
    if not _cat_ok(v.get("variant"), f.get("variants")):
        return False
    if not _cat_ok(v.get("transmission"), f.get("transmissions")):
        return False
    if not _cat_ok(v.get("body_style"), f.get("body_styles")):
        return False
    if not _cat_ok(v.get("drivetrain"), f.get("drivetrains")):
        return False

    # --- colours (substring, case-insensitive) ------------------------------
    for field, key in (("exterior_color", "exterior_color"),
                       ("interior_color", "interior_color")):
        needle = (f.get(key) or "").strip().lower()
        if needle:
            hay = (v.get(field) or "")
            if not hay or needle not in hay.lower():
                return False

    # --- location & seller --------------------------------------------------
    if not _cat_ok(v.get("seller_state"), f.get("states")):
        return False
    city = (f.get("city") or "").strip().lower()
    if city and city not in (v.get("seller_city") or "").lower():
        return False
    if not _cat_ok(v.get("seller_type"), f.get("seller_types")):
        return False
    if not _cat_ok(v.get("listing_type"), f.get("listing_types")):
        return False
    if not _cat_ok(v.get("source_key"), f.get("sources")):
        return False
    if f.get("distance_from_state") and f.get("max_distance_mi"):
        miles, _b = _geo.estimate_road_miles(v.get("seller_state"),
                                             f["distance_from_state"])
        if miles is None or miles > _num(f["max_distance_mi"]):
            return False

    # --- listing activity ---------------------------------------------------
    if f.get("listed_within_days") is not None:
        dom = v.get("days_on_market")
        if dom is None or dom > _num(f["listed_within_days"]):
            return False
    if not _range_ok(v.get("days_on_market"),
                     f.get("days_on_market_min"), f.get("days_on_market_max")):
        return False
    if f.get("price_reduced") and not v.get("price_reduced"):
        return False
    if f.get("total_reduction_min") is not None:
        if (v.get("price_reduction_total") or 0) < _num(f["total_reduction_min"]):
            return False
    if f.get("auction_ending_within_hours") is not None:
        hrs = v.get("auction_hours_left")
        if hrs is None or hrs < 0 or hrs > _num(f["auction_ending_within_hours"]):
            return False
    if f.get("duplicates_only") and not v.get("is_duplicate_vin"):
        return False

    # --- status (verified only) ---------------------------------------------
    if not _cat_ok(v.get("status"), f.get("listing_status")):
        return False

    # --- condition & history (Yes/No/Unknown) -------------------------------
    if not _cat_ok(v.get("title_status"), f.get("title_status")):
        return False
    if not _cat_ok(v.get("accident_history"), f.get("accident_history")):
        return False
    if not _cat_ok(v.get("service_records"), f.get("service_records")):
        return False
    if not _cat_ok(v.get("ppi_done"), f.get("ppi_done")):
        return False
    if not _cat_ok(v.get("seller_docs"), f.get("seller_docs")):
        return False
    if not _cat_ok(v.get("original_status"), f.get("original_status")):
        return False
    if f.get("owners_max") is not None:
        oc = v.get("owners_count")
        if oc is None or oc > _num(f["owners_max"]):
            return False

    # --- options (confirmed present only; unknown never counts as present) ---
    for opt in _as_list(f.get("options")):
        if v.get("features", {}).get(opt) != 1:
            return False

    # --- deal / pipeline status ---------------------------------------------
    ds = [str(s).lower() for s in _as_list(f.get("deal_statuses"))]
    if ds and "all" not in ds:
        if not _deal_status_match(v, ds):
            return False

    # --- valuation-dependent (only ever match valued cars) ------------------
    valued = v.get("valuation_status") == "ok"
    for key, lo, hi in (
        ("expected_resale", f.get("resale_min"), f.get("resale_max")),
    ):
        if (lo is not None or hi is not None):
            if not valued or not _range_ok(v.get(key), lo, hi):
                return False
    for key, lo in (("net_profit", f.get("net_profit_min")),
                    ("roi", f.get("roi_min")),
                    ("max_purchase_price", f.get("max_purchase_min"))):
        if lo is not None:
            if not valued or _num(v.get(key)) is None or _num(v.get(key)) < _num(lo):
                return False
    # cost-side (available for all cars, they are estimates)
    if not _range_ok(v.get("capital_required_estimate"), None, f.get("capital_max")):
        return False
    if not _range_ok(v.get("recon_estimate"), None, f.get("recon_max")):
        return False
    if not _range_ok(v.get("shipping_estimate"), None, f.get("shipping_max")):
        return False
    if not _range_ok(v.get("holding_days_estimate"), None, f.get("holding_max_days")):
        return False
    return True


def _deal_status_match(v, statuses) -> bool:
    for s in statuses:
        if s == "unvalued" and v.get("valuation_status") != "ok":
            return True
        if s == "preliminary" and v.get("underwriting_status") == "preliminary":
            return True
        if s in ("underwritten", "fully_underwritten") and \
                v.get("underwriting_status") == "underwritten":
            return True
        # operator pipeline stages stored on the listing
        if v.get("deal_stage") and s == str(v["deal_stage"]).lower():
            return True
    return False


def apply_filters(rows: list[dict], f: dict) -> list[dict]:
    f = f or {}
    return [v for v in rows if matches(v, f)]


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------
def _key_newest(v):
    return v.get("first_seen_at") or ""


SORTS = {
    "newest": (lambda v: v.get("first_seen_at") or "", True),
    "price_asc": (lambda v: (v.get("price") is None, v.get("price") or 0), False),
    "price_desc": (lambda v: v.get("price") or 0, True),
    "mileage_asc": (lambda v: (v.get("mileage") is None, v.get("mileage") or 0), False),
    "largest_reduction": (lambda v: v.get("price_reduction_total") or 0, True),
    "highest_profit": (lambda v: (v.get("net_profit") is None,
                                  -(v.get("net_profit") or 0)), False),
    "highest_roi": (lambda v: (v.get("roi") is None, -(v.get("roi") or 0)), False),
    "auction_soon": (lambda v: (v.get("auction_hours_left") is None,
                                v.get("auction_hours_left") if v.get("auction_hours_left") is not None else 1e9), False),
}


def sort_rows(rows: list[dict], sort: str | None) -> list[dict]:
    spec = SORTS.get(sort or "newest")
    if spec is None:
        spec = SORTS["newest"]
    keyfn, reverse = spec
    return sorted(rows, key=keyfn, reverse=reverse)


# ---------------------------------------------------------------------------
# Top-level query
# ---------------------------------------------------------------------------
def query(conn, filters: dict | None = None, sort: str | None = None,
          destination_state: str = "OH", include_synthetic: bool = False) -> list[dict]:
    rows = enrich(conn, destination_state, include_synthetic=include_synthetic)
    rows = apply_filters(rows, filters or {})
    return sort_rows(rows, sort)

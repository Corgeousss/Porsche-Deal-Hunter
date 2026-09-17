"""Comparable-sale valuation.

Method (deliberately simple and fully auditable):
  1. Select comps that are the same generation family, within a model-year
     band, a mileage band and an age window. Same variant preferred; falling
     back to any variant is allowed but downgrades confidence and is recorded.
  2. Adjust each comp to the subject car:
       - buyer premium, if the comp was recorded as hammer-only
       - mileage, at a stated $/mile, capped
       - time, at a stated %/year (default 0 = no adjustment)
  3. Aggregate with a trimmed median, and report the spread.

Every comp used and every adjustment applied is stored on the valuation row,
so any number on the dashboard can be traced back to real sales.

What this is NOT: it does not know about options, service history, accident
history, paint condition, colour desirability or a matching-numbers engine.
Those move 911 prices a lot. Treat the output as a screen, not an appraisal.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import statistics

from . import comps as _comps
from . import db as _db
from . import generations as _gens
from . import taxonomy as _tax

MIN_REQUIRED_FIELDS = ("generation", "year", "mileage")

# Material-mismatch guard (method, not a market assumption): after adjustment,
# a comp more than this fraction away from the median is treated as a different
# car and dropped with a recorded reason.
MATERIAL_MISMATCH_FRACTION = 0.60


def _age_days(sale_date: str, today: _dt.date) -> int:
    return (today - _dt.date.fromisoformat(sale_date)).days


def select_comps(conn: sqlite3.Connection, subject: dict,
                 today: _dt.date | None = None,
                 include_synthetic: bool = False) -> tuple[list[sqlite3.Row], dict]:
    """Return (comps, selection_report). Widens in documented steps."""
    today = today or _dt.date.today()
    max_age = _db.get_assumption(conn, "comp_max_age_days")
    mileage_window = _db.get_assumption(conn, "comp_mileage_window")
    year_window = _db.get_assumption(conn, "comp_year_window")
    min_needed = int(_db.get_assumption(conn, "min_comps_low_confidence"))

    gens = _gens.expand(subject.get("generation")) or []
    if not gens:
        return [], {"steps": [], "reason": "subject has no generation"}
    fam = _gens.family(subject.get("generation"))
    family_gens = _gens.expand(fam) if fam else gens

    cutoff = (today - _dt.timedelta(days=int(max_age))).isoformat()
    year = subject.get("year")
    mileage = subject.get("mileage")
    variant = subject.get("variant")
    body = subject.get("body_style")
    transmission = subject.get("transmission")
    tier_vars = _tax.tier_variants(variant)

    steps = []

    def query(gen_list, *, use_variant=False, use_tier=False, use_body=False,
              use_trans=False, use_mileage=False, use_year=False):
        synth_clause = "" if include_synthetic else " AND is_synthetic=0"
        # Only verified transactions we are permitted to use ever reach a
        # valuation. Asking prices and inferred sales are stored but excluded.
        usable = _comps.usable_sql_clause()
        sql = [f"SELECT * FROM comps WHERE {usable}{synth_clause} AND sale_date>=?",
               f"AND generation IN ({','.join('?' * len(gen_list))})"]
        args: list = [cutoff, *gen_list]
        if use_variant and variant:
            sql.append("AND variant=?")
            args.append(variant)
        # Same-tier fallback: never cross a performance tier (base/S/GTS/Turbo/GT).
        if use_tier and tier_vars:
            sql.append(f"AND variant IN ({','.join('?' * len(tier_vars))})")
            args += tier_vars
        if use_body and body:
            sql.append("AND (body_style=? OR body_style IS NULL)")
            args.append(body)
        # Transmission is material on a 911 (manual vs PDK/Tiptronic). Match it
        # when known; comps with unknown transmission are allowed through.
        if use_trans and transmission:
            sql.append("AND (transmission=? OR transmission IS NULL)")
            args.append(transmission)
        if use_mileage and mileage:
            sql.append("AND (mileage IS NULL OR ABS(mileage-?)<=?)")
            args += [mileage, mileage_window]
        if use_year and year:
            sql.append("AND (year IS NULL OR ABS(year-?)<=?)")
            args += [year, year_window]
        return conn.execute(" ".join(sql), args).fetchall()

    # Ladder from strictest to loosest. Variant is only ever dropped as far as
    # the same performance tier -- never to "any variant".
    ladder = [
        ("exact generation + variant + transmission + body + mileage band + year band",
         dict(gen_list=gens, use_variant=True, use_trans=True, use_body=True, use_mileage=True, use_year=True)),
        ("exact generation + variant + transmission + mileage band + year band",
         dict(gen_list=gens, use_variant=True, use_trans=True, use_mileage=True, use_year=True)),
        ("exact generation + variant + mileage band + year band",
         dict(gen_list=gens, use_variant=True, use_mileage=True, use_year=True)),
        ("exact generation + variant + year band",
         dict(gen_list=gens, use_variant=True, use_year=True)),
        ("generation family + variant",
         dict(gen_list=family_gens, use_variant=True)),
        ("generation family + same performance tier",
         dict(gen_list=family_gens, use_tier=True)),
    ]
    exact_variant_labels = {lbl for lbl, kw in ladder if kw.get("use_variant")}
    for label, kw in ladder:
        rows = query(**kw)
        steps.append({"filter": label, "matched": len(rows)})
        if len(rows) >= min_needed:
            return rows, {"steps": steps, "used": label,
                          "widened": label != ladder[0][0],
                          "variant_exact": label in exact_variant_labels,
                          "tier_only": kw.get("use_tier", False)}
    # Nothing met the bar; return the widest same-tier set so the caller can explain.
    rows = query(gen_list=family_gens, use_tier=True)
    return rows, {"steps": steps, "used": None, "widened": True,
                  "variant_exact": False, "tier_only": True}


def _excluded_breakdown(conn: sqlite3.Connection, subject: dict,
                        today: _dt.date, include_synthetic: bool = False) -> dict:
    """Comps that match the car but are barred on provenance grounds. This is
    usually the real reason a valuation cannot be produced, so it is surfaced
    rather than silently dropped."""
    gens = _gens.expand(_gens.family(subject.get("generation"))) or []
    if not gens:
        return {}
    max_age = _db.get_assumption(conn, "comp_max_age_days")
    cutoff = (today - _dt.timedelta(days=int(max_age))).isoformat()
    synth_clause = "" if include_synthetic else " AND is_synthetic=0"
    rows = conn.execute(
        f"""SELECT price_basis, permission_basis, COUNT(*) AS n FROM comps
            WHERE sale_date>=?{synth_clause}
              AND generation IN ({','.join('?' * len(gens))})
              AND NOT ({_comps.usable_sql_clause()})
            GROUP BY price_basis, permission_basis""",
        [cutoff, *gens]).fetchall()
    out = {"total": 0, "reasons": []}
    for r in rows:
        out["total"] += r["n"]
        why = []
        if r["price_basis"] != _comps.USABLE_PRICE_BASIS:
            why.append(f"price_basis={r['price_basis']} "
                       f"({_comps.PRICE_BASES.get(r['price_basis'], '?')})")
        if r["permission_basis"] not in _comps.USABLE_PERMISSION_BASES:
            why.append(f"permission_basis={r['permission_basis']} "
                       f"({_comps.PERMISSION_BASES.get(r['permission_basis'], '?')})")
        out["reasons"].append({"n": r["n"], "why": "; ".join(why)})
    return out


def adjust_comp(conn: sqlite3.Connection, comp: sqlite3.Row, subject: dict,
                today: _dt.date) -> dict:
    per_mile = _db.get_assumption(conn, "mileage_adjust_per_mile")
    cap = _db.get_assumption(conn, "mileage_adjust_cap")
    trend = _db.get_assumption(conn, "market_trend_pct_per_year")
    premium = _db.get_assumption(conn, "buyer_premium_pct")

    base = float(comp["sale_price"])
    adjustments = []

    # Auction comps recorded as hammer-only: add the buyer premium so every
    # comp is on an "all-in price a buyer paid" basis.
    if comp["venue"] in ("bring_a_trailer", "cars_and_bids", "auction_house", "pcarmarket") \
            and not comp["includes_fees"]:
        delta = base * premium
        adjustments.append({"kind": "buyer_premium", "amount": round(delta, 2),
                            "basis": f"hammer-only comp + {premium:.1%} premium (UNVERIFIED rate)"})
        base += delta

    value = base

    # Mileage: a comp with MORE miles than the subject is worth less, so the
    # subject is worth more than the comp.
    if comp["mileage"] is not None and subject.get("mileage") is not None:
        delta = (float(comp["mileage"]) - float(subject["mileage"])) * per_mile
        delta = max(-cap, min(cap, delta))
        adjustments.append({"kind": "mileage", "amount": round(delta, 2),
                            "basis": f"{comp['mileage']:,} comp mi vs {subject['mileage']:,} subject mi "
                                     f"@ ${per_mile}/mi (UNVERIFIED), capped at ${cap:,.0f}"})
        value += delta
    else:
        adjustments.append({"kind": "mileage", "amount": 0.0,
                            "basis": "mileage unknown on comp or subject -- no adjustment"})

    # Time
    if trend:
        years = _age_days(comp["sale_date"], today) / 365.0
        delta = value * trend * years
        adjustments.append({"kind": "time", "amount": round(delta, 2),
                            "basis": f"{years:.2f}y old @ {trend:.1%}/yr"})
        value += delta

    return {
        "comp_id": comp["id"],
        "source_url": comp["source_url"],
        "venue": comp["venue"],
        "price_basis": comp["price_basis"],
        "permission_basis": comp["permission_basis"],
        "sale_date": comp["sale_date"],
        "sale_price": float(comp["sale_price"]),
        "year": comp["year"],
        "variant": comp["variant"],
        "mileage": comp["mileage"],
        "adjusted_value": round(value, 2),
        "adjustments": adjustments,
        "age_days": _age_days(comp["sale_date"], today),
    }


def _trimmed(values: list[float]) -> list[float]:
    """Drop the single highest and lowest once there are enough points."""
    if len(values) >= 5:
        s = sorted(values)
        return s[1:-1]
    return sorted(values)


def value_listing(conn: sqlite3.Connection, listing: sqlite3.Row | dict,
                  today: _dt.date | None = None, store: bool = True,
                  include_synthetic: bool = False) -> dict:
    today = today or _dt.date.today()
    subject = dict(listing)

    missing = [f for f in MIN_REQUIRED_FIELDS if not subject.get(f)]
    if missing:
        result = {
            "status": "missing_inputs",
            "confidence": "none",
            "point_value": None, "low_value": None, "high_value": None,
            "n_comps": 0,
            "method": "comparable_sales_v1",
            "detail": {"missing_fields": missing,
                       "explanation": "Cannot value the car without " + ", ".join(missing) + "."},
        }
        if store:
            _store(conn, subject.get("id"), result)
        return result

    rows, selection = select_comps(conn, subject, today, include_synthetic=include_synthetic)
    selection["excluded"] = _excluded_breakdown(conn, subject, today,
                                                include_synthetic=include_synthetic)
    min_low = int(_db.get_assumption(conn, "min_comps_low_confidence"))
    min_med = int(_db.get_assumption(conn, "min_comps_medium_confidence"))
    min_high = int(_db.get_assumption(conn, "min_comps_high_confidence"))

    if len(rows) < min_low:
        result = {
            "status": "insufficient_comps",
            "confidence": "none",
            "point_value": None, "low_value": None, "high_value": None,
            "n_comps": len(rows),
            "method": "comparable_sales_v1",
            "detail": {
                "selection": selection,
                "explanation": (
                    f"INSUFFICIENT DATA: only {len(rows)} usable verified sale(s) "
                    f"match this car; {min_low} are required. "
                    + (f"{selection['excluded']['total']} further matching record(s) "
                       f"were excluded on provenance grounds "
                       f"({'; '.join(r['why'] for r in selection['excluded']['reasons'])}). "
                       if selection.get("excluded", {}).get("total") else "")
                    + "No estimate is produced from thin data."
                ),
            },
        }
        if store:
            _store(conn, subject.get("id"), result)
        return result

    adjusted = [adjust_comp(conn, r, subject, today) for r in rows]

    # --- material-mismatch guard -------------------------------------------
    # A minimum count is necessary but not sufficient: a comp whose adjusted
    # value sits more than MATERIAL_MISMATCH_FRACTION from the median of the set
    # is a different car (wrong options, condition, mis-tagged variant) and is
    # dropped with a recorded reason rather than dragging the estimate.
    prelim = statistics.median([a["adjusted_value"] for a in adjusted])
    rejected = []
    kept = []
    for a in adjusted:
        if prelim and abs(a["adjusted_value"] - prelim) / prelim > MATERIAL_MISMATCH_FRACTION:
            a = {**a, "rejected_reason": (
                f"adjusted ${a['adjusted_value']:,.0f} is "
                f"{abs(a['adjusted_value'] - prelim) / prelim:.0%} from the "
                f"${prelim:,.0f} median -- treated as materially mismatched")}
            rejected.append(a)
        else:
            kept.append(a)

    if len(kept) < min_low:
        result = {
            "status": "insufficient_comps", "confidence": "none",
            "point_value": None, "low_value": None, "high_value": None,
            "n_comps": len(kept), "method": "comparable_sales_v1",
            "detail": {
                "selection": selection, "rejected_comps": rejected,
                "explanation": (
                    f"INSUFFICIENT DATA after rejecting materially mismatched "
                    f"comps: {len(kept)} usable of {len(adjusted)} matched "
                    f"({len(rejected)} rejected as too far from the median); "
                    f"{min_low} are required. No estimate is produced."),
            },
        }
        if store:
            _store(conn, subject.get("id"), result)
        return result

    adjusted = kept
    values = [a["adjusted_value"] for a in adjusted]
    core = _trimmed(values)
    point = statistics.median(core)
    low, high = min(core), max(core)
    spread_pct = (high - low) / point if point else 0.0

    n = len(adjusted)
    if n >= min_high and not selection.get("widened"):
        confidence = "high"
    elif n >= min_med:
        confidence = "medium"
    else:
        confidence = "low"
    if spread_pct > 0.45 and confidence == "high":
        confidence = "medium"
    # A comp set that had to drop to same-tier (variant not matched exactly) is
    # never more than "low" confidence, and a mixed-variant set is flagged.
    if selection.get("tier_only") or not selection.get("variant_exact", True):
        confidence = "low"

    result = {
        "status": "ok",
        "confidence": confidence,
        "point_value": round(point, 2),
        "low_value": round(low, 2),
        "high_value": round(high, 2),
        "n_comps": n,
        "method": "comparable_sales_v1",
        "detail": {
            "selection": selection,
            "spread_pct": round(spread_pct, 4),
            "comps": adjusted,
            "rejected_comps": rejected,
            "variant_match": ("exact" if selection.get("variant_exact")
                              else ("same-tier only" if selection.get("tier_only")
                                    else "mixed")),
            "assumptions_used": [
                "mileage_adjust_per_mile", "mileage_adjust_cap",
                "market_trend_pct_per_year", "buyer_premium_pct",
                "comp_max_age_days", "comp_mileage_window", "comp_year_window",
            ],
            "explanation": (
                f"Trimmed median of {len(core)} mileage-adjusted comps drawn from "
                f"{n} documented completed sales ({selection.get('used')}). "
                + (f"{len(rejected)} comp(s) rejected as materially mismatched. "
                   if rejected else "")
                + f"Adjusted range ${low:,.0f}-${high:,.0f}."
            ),
            "caveats": [
                "No adjustment for options, service history, accident history, "
                "paint or colour. Those routinely move a 911 by more than the "
                "mileage adjustment applied here.",
            ] + ([
                "Comps are same performance tier but NOT the exact variant, so "
                "confidence is capped at low. Record exact-variant sales to lift it."
            ] if (selection.get("tier_only") or not selection.get("variant_exact", True)) else []),
        },
    }
    if store:
        _store(conn, subject.get("id"), result)
    return result


def _store(conn: sqlite3.Connection, listing_id: int | None, result: dict) -> int | None:
    if listing_id is None:
        return None
    cur = conn.execute(
        """INSERT INTO valuations (listing_id, computed_at, method, point_value,
                                   low_value, high_value, n_comps, confidence, status, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (listing_id, _db.utcnow(), result["method"], result["point_value"],
         result["low_value"], result["high_value"], result["n_comps"],
         result["confidence"], result["status"], json.dumps(result["detail"])),
    )
    conn.commit()
    result["valuation_id"] = int(cur.lastrowid)
    return result["valuation_id"]

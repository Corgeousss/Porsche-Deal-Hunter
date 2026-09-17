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

from . import db as _db
from . import generations as _gens

MIN_REQUIRED_FIELDS = ("generation", "year", "mileage")


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

    steps = []

    def query(gen_list, use_variant, use_body, use_mileage, use_year):
        synth_clause = "" if include_synthetic else " AND is_synthetic=0"
        sql = [f"SELECT * FROM comps WHERE 1=1{synth_clause} AND sale_date>=?",
               f"AND generation IN ({','.join('?' * len(gen_list))})"]
        args: list = [cutoff, *gen_list]
        if use_variant and variant:
            sql.append("AND variant=?")
            args.append(variant)
        if use_body and body:
            sql.append("AND (body_style=? OR body_style IS NULL)")
            args.append(body)
        if use_mileage and mileage:
            sql.append("AND (mileage IS NULL OR ABS(mileage-?)<=?)")
            args += [mileage, mileage_window]
        if use_year and year:
            sql.append("AND (year IS NULL OR ABS(year-?)<=?)")
            args += [year, year_window]
        return conn.execute(" ".join(sql), args).fetchall()

    ladder = [
        ("exact generation + variant + body + mileage band + year band", gens, True, True, True, True),
        ("exact generation + variant + mileage band + year band", gens, True, False, True, True),
        ("exact generation + variant + year band", gens, True, False, False, True),
        ("generation family + variant", family_gens, True, False, False, False),
        ("generation family, any variant", family_gens, False, False, False, False),
    ]
    for label, gl, uv, ub, um, uy in ladder:
        rows = query(gl, uv, ub, um, uy)
        steps.append({"filter": label, "matched": len(rows)})
        if len(rows) >= min_needed:
            return rows, {"steps": steps, "used": label,
                          "widened": label != ladder[0][0]}
    # Nothing met the bar; return the widest set so the caller can explain.
    rows = query(family_gens, False, False, False, False)
    return rows, {"steps": steps, "used": None, "widened": True}


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
                    f"Only {len(rows)} documented completed sale(s) match this car; "
                    f"{min_low} are required. Record more comps with "
                    f"`comps add` before this car can be valued. No estimate is "
                    f"produced from thin data."
                ),
            },
        }
        if store:
            _store(conn, subject.get("id"), result)
        return result

    adjusted = [adjust_comp(conn, r, subject, today) for r in rows]
    values = [a["adjusted_value"] for a in adjusted]
    core = _trimmed(values)
    point = statistics.median(core)
    low, high = min(core), max(core)
    spread_pct = (high - low) / point if point else 0.0

    n = len(rows)
    if n >= min_high and not selection.get("widened"):
        confidence = "high"
    elif n >= min_med:
        confidence = "medium"
    else:
        confidence = "low"
    if spread_pct > 0.45 and confidence == "high":
        confidence = "medium"

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
            "assumptions_used": [
                "mileage_adjust_per_mile", "mileage_adjust_cap",
                "market_trend_pct_per_year", "buyer_premium_pct",
                "comp_max_age_days", "comp_mileage_window", "comp_year_window",
            ],
            "explanation": (
                f"Trimmed median of {len(core)} mileage-adjusted comps drawn from "
                f"{n} documented completed sales ({selection.get('used')}). "
                f"Adjusted range ${low:,.0f}-${high:,.0f}."
            ),
            "caveats": [
                "No adjustment for options, service history, accident history, "
                "paint or colour. Those routinely move a 911 by more than the "
                "mileage adjustment applied here.",
            ],
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

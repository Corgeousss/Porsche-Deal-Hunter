"""Cost model, net-profit projection and maximum purchase price.

The arithmetic, stated plainly:

    expected_resale = comp_value * (1 - resale_haircut)

    costs(P) = P
             + repairs + detail_and_photography
             + transport
             + title_and_admin
             + sale_fee(expected_resale)
             + carrying (days_to_sell * carrying_cost_per_day)
             + risk_reserve (risk_pct * P)

    net(P) = expected_resale - costs(P)

Because both the purchase price and the risk reserve scale with P, solving
net(P) = target for P gives the maximum you can pay:

    P_max = (expected_resale - fixed_costs - target) / (1 + risk_pct)

Every input is either a documented comp (via valuation.py) or a named entry in
the assumption register, and the breakdown returned here labels which.
"""

from __future__ import annotations

import json
import sqlite3

from . import db as _db
from . import geo
from . import valuation as _valuation
from .assumptions import BY_KEY


def repairs_key_for(generation: str | None) -> str:
    fam = (generation or "").split(".")[0]
    return {"996": "repairs_996", "997": "repairs_997", "991": "repairs_991"}.get(
        fam, "repairs_default")


def _line(conn, key: str, amount: float, note: str = "") -> dict:
    a = BY_KEY.get(key)
    return {
        "key": key,
        "amount": round(amount, 2),
        "basis": (a.basis if a else note),
        "verified": bool(a.verified) if a else True,
        "note": note,
    }


def evaluate(conn: sqlite3.Connection, listing: sqlite3.Row | dict, *,
             destination_state: str, valuation: dict | None = None,
             repairs_override: float | None = None,
             transport_override: float | None = None,
             days_override: float | None = None,
             store: bool = True, include_synthetic: bool = False) -> dict:
    subject = dict(listing)
    listing_id = subject.get("id")
    val = valuation or _valuation.value_listing(
        conn, subject, store=store, include_synthetic=include_synthetic)

    asking = subject.get("price")
    target = _db.get_assumption(conn, "target_net_profit")

    # A real quote recorded against this specific car beats the generic default.
    stored = _db.get_override(conn, listing_id)
    if repairs_override is None:
        repairs_override = stored.get("repairs")
    if transport_override is None:
        transport_override = stored.get("transport")
    if days_override is None:
        days_override = stored.get("days_to_sell")

    if val["status"] != "ok":
        out = {
            "status": val["status"],
            "asking_price": asking,
            "expected_resale": None, "total_cost": None, "net_profit": None,
            "max_purchase_price": None, "meets_threshold": False,
            "detail": {
                "explanation": val["detail"].get("explanation"),
                "valuation_status": val["status"],
                "n_comps": val["n_comps"],
            },
            "valuation": val,
        }
        if store:
            _store(conn, listing_id, val.get("valuation_id"), out)
        return out

    haircut = _db.get_assumption(conn, "resale_haircut_pct")
    comp_value = val["point_value"]
    expected_resale = comp_value * (1 - haircut)

    # --- cost lines ---------------------------------------------------------
    lines: list[dict] = []

    rk = repairs_key_for(subject.get("generation"))
    if repairs_override is not None:
        lines.append({"key": "repairs", "amount": round(float(repairs_override), 2),
                      "basis": "operator-supplied repair estimate for this car",
                      "verified": True, "note": ""})
        repairs = float(repairs_override)
    else:
        repairs = _db.get_assumption(conn, rk)
        lines.append(_line(conn, rk, repairs,
                           "flat generation default -- replace with a real quote"))

    detail_cost = _db.get_assumption(conn, "detail_and_photography")
    lines.append(_line(conn, "detail_and_photography", detail_cost))

    if transport_override is not None:
        transport = float(transport_override)
        transport_note = "operator-supplied transport quote"
        lines.append({"key": "transport", "amount": round(transport, 2),
                      "basis": transport_note, "verified": True, "note": ""})
    else:
        miles, basis = geo.estimate_road_miles(subject.get("seller_state"), destination_state)
        if miles is None:
            miles = _db.get_assumption(conn, "transport_fallback_miles")
            basis = (f"{basis} -- using the {miles:,.0f} mile fallback so an "
                     f"unknown location never looks cheap")
        base = _db.get_assumption(conn, "transport_base")
        per_mile = _db.get_assumption(conn, "transport_per_mile")
        transport = base + miles * per_mile
        lines.append({"key": "transport", "amount": round(transport, 2),
                      "basis": (f"${base:,.0f} base + {miles:,.0f} mi x ${per_mile}/mi "
                                f"({basis})"),
                      "verified": False, "note": "UNVERIFIED -- get a broker quote"})

    admin = _db.get_assumption(conn, "title_and_admin")
    lines.append(_line(conn, "title_and_admin", admin))

    fee_pct = _db.get_assumption(conn, "sale_fee_pct")
    fee_cap = _db.get_assumption(conn, "sale_fee_cap")
    sale_fee = min(expected_resale * fee_pct, fee_cap)
    lines.append({"key": "sale_fee", "amount": round(sale_fee, 2),
                  "basis": f"{fee_pct:.1%} of ${expected_resale:,.0f} resale, capped at ${fee_cap:,.0f}",
                  "verified": False, "note": "UNVERIFIED -- depends on sales channel"})

    days = days_override if days_override is not None else _db.get_assumption(conn, "days_to_sell")
    per_day = _db.get_assumption(conn, "carrying_cost_per_day")
    carrying = days * per_day
    lines.append({"key": "carrying", "amount": round(carrying, 2),
                  "basis": f"{days:,.0f} days x ${per_day}/day (storage, insurance, capital)",
                  "verified": False, "note": "UNVERIFIED"})

    fixed_costs = sum(l["amount"] for l in lines)
    risk_pct = _db.get_assumption(conn, "risk_reserve_pct")

    # --- max purchase price -------------------------------------------------
    max_purchase = (expected_resale - fixed_costs - target) / (1 + risk_pct)

    # --- profit at the actual asking price ----------------------------------
    if asking is not None:
        risk_reserve = asking * risk_pct
        total_cost = asking + fixed_costs + risk_reserve
        net_profit = expected_resale - total_cost
    else:
        risk_reserve = total_cost = net_profit = None

    meets = bool(net_profit is not None and net_profit >= target)

    # --- why does it look undervalued? -------------------------------------
    explanation = _explain(subject, val, comp_value, expected_resale, asking,
                           net_profit, max_purchase, target, fixed_costs, lines)

    out = {
        "status": "ok",
        "asking_price": asking,
        "comp_value": round(comp_value, 2),
        "expected_resale": round(expected_resale, 2),
        "fixed_costs": round(fixed_costs, 2),
        "risk_reserve": round(risk_reserve, 2) if risk_reserve is not None else None,
        "total_cost": round(total_cost, 2) if total_cost is not None else None,
        "net_profit": round(net_profit, 2) if net_profit is not None else None,
        "max_purchase_price": round(max_purchase, 2),
        "target_net_profit": target,
        "meets_threshold": meets,
        "detail": {
            "cost_lines": lines,
            "risk_reserve_pct": risk_pct,
            "resale_haircut_pct": haircut,
            "destination_state": destination_state,
            "explanation": explanation,
            "unverified_lines": [l["key"] for l in lines if not l["verified"]],
            "valuation_confidence": val["confidence"],
            "valuation_n_comps": val["n_comps"],
        },
        "valuation": val,
    }
    if store:
        _store(conn, listing_id, val.get("valuation_id"), out)
    return out


def _explain(subject, val, comp_value, expected_resale, asking, net_profit,
             max_purchase, target, fixed_costs, lines) -> list[str]:
    """Plain-language reasons, stated as arithmetic the operator can check."""
    out = []
    conf = val["confidence"]
    out.append(
        f"Comp-derived market value ${comp_value:,.0f} from {val['n_comps']} documented "
        f"completed sales ({conf} confidence; adjusted range "
        f"${val['low_value']:,.0f}-${val['high_value']:,.0f})."
    )
    if asking is not None:
        gap = comp_value - asking
        pct = gap / comp_value if comp_value else 0
        if gap > 0:
            out.append(
                f"Asking ${asking:,.0f} is ${gap:,.0f} ({pct:.0%}) below that comp value. "
                f"That discount is the entire source of the margin -- it is not a "
                f"claim that the car is a bargain, only that the ask sits below "
                f"what similar cars have sold for."
            )
        else:
            out.append(
                f"Asking ${asking:,.0f} is at or above the comp value ${comp_value:,.0f}. "
                f"There is no discount to work with here."
            )
        out.append(
            f"After a {(1 - expected_resale / comp_value):.0%} resale haircut and "
            f"${fixed_costs:,.0f} of fixed costs, net at the asking price is "
            f"${net_profit:,.0f} against a ${target:,.0f} target."
        )
    else:
        out.append("No asking price recorded, so only the maximum bid is computed.")

    out.append(
        f"Maximum purchase price to still clear ${target:,.0f} net: ${max_purchase:,.0f}."
    )

    biggest = max((l for l in lines), key=lambda l: l["amount"], default=None)
    if biggest:
        out.append(f"Largest single cost line is {biggest['key']} at ${biggest['amount']:,.0f}.")

    unverified = [l["key"] for l in lines if not l["verified"]]
    if unverified:
        out.append(
            "UNVERIFIED ASSUMPTIONS in this calculation: " + ", ".join(unverified) +
            ". Every one is a placeholder until you replace it with a real quote."
        )
    if conf in ("low", "medium"):
        out.append(
            f"Valuation confidence is {conf}. Add more documented comps for this "
            f"generation and variant before acting on this number."
        )
    missing = [f for f in ("vin", "mileage", "transmission", "seller_state")
               if not subject.get(f)]
    if missing:
        out.append("Missing listing data that would change this: " + ", ".join(missing) + ".")
    return out


def _store(conn, listing_id, valuation_id, out: dict) -> None:
    if listing_id is None:
        return
    conn.execute(
        """INSERT INTO deals (listing_id, valuation_id, computed_at, asking_price,
                              expected_resale, total_cost, net_profit,
                              max_purchase_price, meets_threshold, status, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (listing_id, valuation_id, _db.utcnow(), out.get("asking_price"),
         out.get("expected_resale"), out.get("total_cost"), out.get("net_profit"),
         out.get("max_purchase_price"), int(out.get("meets_threshold", False)),
         out["status"], json.dumps(out["detail"])),
    )
    conn.commit()


def evaluate_all(conn: sqlite3.Connection, destination_state: str,
                 only_active: bool = True,
                 include_synthetic: bool = False) -> list[dict]:
    sql = "SELECT * FROM listings"
    if only_active:
        sql += " WHERE status='active'"
    results = []
    for row in conn.execute(sql).fetchall():
        res = evaluate(conn, row, destination_state=destination_state,
                       include_synthetic=include_synthetic)
        res["listing"] = dict(row)
        results.append(res)
    return results

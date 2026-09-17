"""Cost model, net-profit projection and maximum purchase price.

The arithmetic, stated plainly:

    expected_resale = comp_value * (1 - resale_haircut)

    costs(P) = P * (1 + k)  +  fixed_costs

where k is every rate that scales with the purchase price:

    k = purchase_tax_pct            (0 if you hold a resale exemption)
      + auction_buyer_premium_pct   (auction listings only)
      + risk_reserve_pct

and fixed_costs is everything that does not:

    ppi + repairs + detail_and_photography + transport + title_and_admin
      + dealer_doc_fee (dealer sellers only)
      + sale_fee(expected_resale) + carrying(days * per_day)

    net(P) = expected_resale - costs(P)

Solving net(P) = target for P gives the maximum you can pay:

    P_max = (expected_resale - fixed_costs - target) / (1 + k)

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


def _all_verified(conn, *keys: str) -> bool:
    """A composite cost line is only verified when every assumption feeding it
    is verified."""
    for k in keys:
        row = conn.execute("SELECT verified FROM assumptions WHERE key=?", (k,)).fetchone()
        if row is None or not row["verified"]:
            return False
    return True


def repairs_key_for(generation: str | None) -> str:
    fam = (generation or "").split(".")[0]
    return {"996": "repairs_996", "997": "repairs_997", "991": "repairs_991"}.get(
        fam, "repairs_default")


def _line(conn, key: str, amount: float, note: str = "") -> dict:
    """Build a cost line. `verified` comes from the DATABASE, not the shipped
    defaults -- otherwise confirming an assumption would never take effect."""
    row = conn.execute("SELECT * FROM assumptions WHERE key=?", (key,)).fetchone()
    a = BY_KEY.get(key)
    if row is not None:
        basis, verified = row["basis"], bool(row["verified"])
    else:
        basis, verified = (a.basis if a else note), (bool(a.verified) if a else True)
    return {
        "key": key,
        "amount": round(amount, 2),
        "basis": basis,
        "verified": verified,
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
            "underwriting_status": "unvalued",
            "underwriting_blockers": ["no usable comparable sales"],
            "before_purchase_tax": None,
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
    applied_notes: list[str] = []

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
        tv = _all_verified(conn, "transport_base", "transport_per_mile")
        lines.append({"key": "transport", "amount": round(transport, 2),
                      "basis": (f"${base:,.0f} base + {miles:,.0f} mi x ${per_mile}/mi "
                                f"({basis})"),
                      "verified": tv,
                      "note": "" if tv else "UNVERIFIED -- get a broker quote"})

    admin = _db.get_assumption(conn, "title_and_admin")
    lines.append(_line(conn, "title_and_admin", admin))

    # Pre-purchase inspection: always, on every car.
    ppi = _db.get_assumption(conn, "ppi_cost")
    lines.append(_line(conn, "ppi_cost", ppi))

    # Dealer documentation fee: only when the seller is a dealer.
    seller_type = (subject.get("seller_type") or "unknown").lower()
    if seller_type == "dealer":
        doc_fee = _db.get_assumption(conn, "dealer_doc_fee")
        lines.append(_line(conn, "dealer_doc_fee", doc_fee,
                           "applied because seller_type is 'dealer'"))
    elif seller_type == "unknown":
        applied_notes.append(
            "Seller type is unknown, so no dealer documentation fee was charged. "
            "If this is a dealer, add ~$"
            f"{_db.get_assumption(conn, 'dealer_doc_fee'):,.0f}.")

    fee_pct = _db.get_assumption(conn, "sale_fee_pct")
    fee_cap = _db.get_assumption(conn, "sale_fee_cap")
    sale_fee = min(expected_resale * fee_pct, fee_cap)
    fv = _all_verified(conn, "sale_fee_pct", "sale_fee_cap")
    lines.append({"key": "sale_fee", "amount": round(sale_fee, 2),
                  "basis": f"{fee_pct:.1%} of ${expected_resale:,.0f} resale, capped at ${fee_cap:,.0f}",
                  "verified": fv,
                  "note": "" if fv else "UNVERIFIED -- depends on sales channel"})

    days = days_override if days_override is not None else _db.get_assumption(conn, "days_to_sell")
    per_day = _db.get_assumption(conn, "carrying_cost_per_day")
    carrying = days * per_day
    cv = (_all_verified(conn, "carrying_cost_per_day")
          and (days_override is not None or _all_verified(conn, "days_to_sell")))
    lines.append({"key": "carrying", "amount": round(carrying, 2),
                  "basis": f"{days:,.0f} days x ${per_day}/day (storage, insurance, capital)",
                  "verified": cv, "note": "" if cv else "UNVERIFIED"})

    fixed_costs = sum(l["amount"] for l in lines)

    # --- rates that scale with the purchase price ---------------------------
    risk_pct = _db.get_assumption(conn, "risk_reserve_pct")
    tax_status = _db.assumption_status(conn, "purchase_tax_pct")
    tax_unset = tax_status == "unset"
    # An unset tax rate is NOT an exemption. We do not invent a rate, so the
    # figure is computed before tax and labelled as such -- and the deal can
    # never reach 'underwritten' while it stays unset.
    tax_pct = 0.0 if tax_unset else _db.get_assumption(conn, "purchase_tax_pct")
    listing_type = (subject.get("listing_type") or "unknown").lower()
    auction_pct = (_db.get_assumption(conn, "auction_buyer_premium_pct")
                   if listing_type == "auction" else 0.0)
    if listing_type == "unknown":
        applied_notes.append(
            "Listing type is unknown, so no auction buyer premium was charged. "
            "If this is an auction, add "
            f"{_db.get_assumption(conn, 'auction_buyer_premium_pct'):.1%} of the "
            "hammer price.")
    if tax_unset:
        applied_notes.append(
            "PURCHASE TAX IS NOT SET, so this figure is BEFORE PURCHASE TAX. "
            "This is not an exemption -- the tool will not assume your tax "
            "position. Set it either way: "
            "`assumptions --set purchase_tax_pct 0 --verified` if you have a "
            "confirmed resale exemption, or your actual combined rate.")
    elif tax_pct == 0:
        applied_notes.append(
            "Purchase tax is set to 0% and marked confirmed (resale/dealer "
            "exemption).")

    def _rate_verified(key: str) -> bool:
        row = conn.execute("SELECT verified FROM assumptions WHERE key=?",
                           (key,)).fetchone()
        return bool(row["verified"]) if row else False

    rate_lines = [
        {"key": "purchase_tax", "rate": tax_pct,
         "basis": ("NOT SET -- this figure is before purchase tax" if tax_unset
                   else f"{tax_pct:.2%} of the purchase price"),
         "verified": (not tax_unset) and tax_status == "verified",
         "unset": tax_unset},
        {"key": "auction_buyer_premium", "rate": auction_pct,
         "basis": (f"{auction_pct:.1%} of the purchase price (auction listing)"
                   if auction_pct else "not an auction listing -- not applied"),
         "verified": _rate_verified("auction_buyer_premium_pct")},
        {"key": "risk_reserve", "rate": risk_pct,
         "basis": BY_KEY["risk_reserve_pct"].basis,
         "verified": _rate_verified("risk_reserve_pct")},
    ]
    k = tax_pct + auction_pct + risk_pct

    # --- max purchase price -------------------------------------------------
    max_purchase = (expected_resale - fixed_costs - target) / (1 + k)

    # --- profit at the actual asking price ----------------------------------
    if asking is not None:
        for rl in rate_lines:
            rl["amount"] = round(asking * rl["rate"], 2)
        risk_reserve = asking * risk_pct
        proportional = asking * k
        total_cost = asking + proportional + fixed_costs
        net_profit = expected_resale - total_cost
    else:
        for rl in rate_lines:
            rl["amount"] = None
        risk_reserve = proportional = total_cost = net_profit = None

    meets = bool(net_profit is not None and net_profit >= target)

    # --- preliminary vs fully underwritten ---------------------------------
    # A deal is only 'underwritten' when every cost input feeding it is a real
    # confirmed number. Anything else is preliminary: a screen, not a decision.
    unverified_cost_keys = [l["key"] for l in lines if not l["verified"]]
    unverified_rate_keys = [r["key"] for r in rate_lines
                            if not r["verified"] and (r["rate"] or r.get("unset"))]
    blocking = []
    if tax_unset:
        blocking.append("purchase_tax_pct is not set")
    if unverified_cost_keys:
        blocking.append(f"{len(unverified_cost_keys)} unverified cost lines")
    if unverified_rate_keys:
        blocking.append(f"{len(unverified_rate_keys)} unverified rates")
    if val["confidence"] in ("low", "none"):
        blocking.append(f"valuation confidence is {val['confidence']}")

    underwriting_status = "underwritten" if not blocking else "preliminary"

    # --- why does it look undervalued? -------------------------------------
    explanation = _explain(subject, val, comp_value, expected_resale, asking,
                           net_profit, max_purchase, target, fixed_costs, lines)
    explanation.extend(applied_notes)
    if underwriting_status == "preliminary":
        explanation.append(
            "STATUS: PRELIMINARY, not fully underwritten. Blocking: "
            + "; ".join(blocking) + ". This is a screen, not a confirmed profit.")
    else:
        explanation.append(
            "STATUS: FULLY UNDERWRITTEN. Every cost input is a confirmed number "
            "and purchase tax is set.")

    out = {
        "status": "ok",
        "asking_price": asking,
        "comp_value": round(comp_value, 2),
        "expected_resale": round(expected_resale, 2),
        "fixed_costs": round(fixed_costs, 2),
        "proportional_cost": round(proportional, 2) if proportional is not None else None,
        "proportional_rate": round(k, 6),
        "risk_reserve": round(risk_reserve, 2) if risk_reserve is not None else None,
        "total_cost": round(total_cost, 2) if total_cost is not None else None,
        "net_profit": round(net_profit, 2) if net_profit is not None else None,
        "max_purchase_price": round(max_purchase, 2),
        "target_net_profit": target,
        "meets_threshold": meets,
        "underwriting_status": underwriting_status,
        "underwriting_blockers": blocking,
        "before_purchase_tax": tax_unset,
        "detail": {
            "cost_lines": lines,
            "rate_lines": rate_lines,
            "risk_reserve_pct": risk_pct,
            "purchase_tax_pct": tax_pct,
            "auction_buyer_premium_pct": auction_pct,
            "resale_haircut_pct": haircut,
            "destination_state": destination_state,
            "explanation": explanation,
            "unverified_lines": ([l["key"] for l in lines if not l["verified"]]
                                 + [r["key"] for r in rate_lines
                                    if not r["verified"] and r["rate"]]),
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

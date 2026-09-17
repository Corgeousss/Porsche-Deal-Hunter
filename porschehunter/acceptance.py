"""Acceptance test: one real listing, underwritten end to end, or INSUFFICIENT DATA.

This is the report you would actually act on. It either produces a complete,
traceable underwriting -- valuation arithmetic, exact maximum offer, total
investment, net profit -- or it refuses and names precisely what access is
missing.

It will not lower its standards to produce output. The bar:
  * the subject must be a real listing with a real source URL
  * at least MIN_COMPS usable comps, each one a verified transaction the
    operator is permitted to use
  * no synthetic rows anywhere in the database
"""

from __future__ import annotations

import datetime as _dt

from . import comps as _comps
from . import db as _db
from . import deal as _deal
from . import valuation as _valuation

MIN_COMPS = 3


def _line(w=76):
    return "=" * w


def gather(conn, listing_id: int, destination_state: str) -> dict:
    row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if row is None:
        return {"status": "no_listing", "listing_id": listing_id}

    synthetic = conn.execute(
        "SELECT COUNT(*) c FROM comps WHERE is_synthetic=1").fetchone()["c"]

    val = _valuation.value_listing(conn, row, store=True)
    res = _deal.evaluate(conn, row, destination_state=destination_state,
                         valuation=val)
    return {"status": "ok", "listing": dict(row), "valuation": val, "deal": res,
            "synthetic": synthetic}


def _gap_report(conn, listing, val) -> list[str]:
    """Exactly what access is missing, in terms the operator can act on."""
    gen = listing.get("generation") or "(unknown generation)"
    out = []
    excl = (val.get("detail", {}).get("selection", {}) or {}).get("excluded", {}) or {}
    n_usable = val.get("n_comps", 0)

    total_stored = conn.execute(
        "SELECT COUNT(*) c FROM comps WHERE is_synthetic=0").fetchone()["c"]
    total_usable = conn.execute(
        f"SELECT COUNT(*) c FROM comps WHERE is_synthetic=0 AND "
        f"{_comps.usable_sql_clause()}").fetchone()["c"]

    out.append(f"Comps in database:        {total_stored} stored, {total_usable} usable")
    out.append(f"Matching this {gen} car:  {n_usable} usable "
               f"(need {MIN_COMPS})")
    if excl.get("total"):
        out.append(f"Excluded on provenance:   {excl['total']}")
        for r in excl.get("reasons", []):
            out.append(f"    {r['n']}x  {r['why']}")

    if val.get("status") == "missing_inputs":
        out.append("Listing fields missing:   "
                   + ", ".join(val["detail"].get("missing_fields", [])))

    out.append("")
    out.append("WHAT IS NEEDED TO CLEAR THIS")
    out.append("-" * 76)
    out.append(
        f"{max(0, MIN_COMPS - n_usable)} more verified completed sales for a "
        f"{gen}, each with a sale price someone actually paid, a sale date, a "
        f"source URL, and a recorded permission basis.")
    out.append("")
    out.append("Routes to that data, in order of how quickly they can be opened:")
    out.append(
        "  1. CLASSIC.COM licensed API -- sales history and comparable-sales "
        "functionality, taxonomy included. Licensed, negotiated via "
        "datasupport@classic.com. Docs: https://support.classic.com/classic.com-api")
    out.append(
        "  2. MarketCheck -- listings across dealer, private-party and auction "
        "inventory. Note their Past Inventory covers DEALER listings only and "
        "represents removals, not verified transactions, so it does not by "
        "itself satisfy this bar. Docs: https://docs.marketcheck.com/docs/api/cars")
    out.append(
        "  3. Your own completed transactions -- permission_basis=own_transaction. "
        "Immediate, free, and the highest-quality comp you will ever have.")
    out.append(
        "  4. Any sale where the counterparty tells you the price directly "
        "-- permission_basis=seller_disclosed.")
    out.append("")
    out.append(
        "Auction results published on sites without a data licence are NOT a "
        "route here. Aggregating them into this database needs permission from "
        "the site; record that permission in --permission-note if you obtain it.")
    return out


def report(conn, listing_id: int, destination_state: str) -> tuple[str, bool]:
    """Returns (report_text, passed)."""
    data = gather(conn, listing_id, destination_state)
    out = ["ACCEPTANCE TEST -- Porsche 911 acquisition underwriting", _line(),
           f"Generated:  {_db.utcnow()}",
           f"Database:   {conn.execute('PRAGMA database_list').fetchone()[2]}"]

    if data["status"] == "no_listing":
        out.append("")
        out.append(f"INSUFFICIENT DATA: no listing #{listing_id} in the database.")
        out.append("Add a real listing first: `porschehunter add <url> ...`")
        return "\n".join(out), False

    L, val, res = data["listing"], data["valuation"], data["deal"]

    if data["synthetic"]:
        out.append("")
        out.append(f"ABORTED: {data['synthetic']} synthetic rows in this database. "
                   "An acceptance test must run on real data only.")
        return "\n".join(out), False

    miles_s = f"{L['mileage']:,} mi" if L["mileage"] else "UNKNOWN"
    price_s = f"${L['price']:,.0f}" if L["price"] else "UNKNOWN"
    out += ["", "SUBJECT VEHICLE", "-" * 76,
            f"  Listing:      #{L['id']}  {L['title'] or '(no title)'}",
            f"  Source:       {L['source_key']}",
            f"  Source URL:   {L['url']}",
            f"  Retrieved:    {L['fetched_at']}  (first seen {L['first_seen_at']})",
            f"  Provenance:   {L['data_source_note'] or '(not recorded)'}",
            f"  Spec:         {L['year'] or '?'} {L['generation'] or '?'} "
            f"{L['variant'] or '?'} {L['body_style'] or '?'} / "
            f"{L['transmission'] or '?'}",
            f"  Odometer:     {miles_s}",
            f"  VIN:          {L['vin'] or 'UNKNOWN'}",
            f"  Asking:       {price_s}",
            f"  Location:     {L['seller_city'] or '?'}, {L['seller_state'] or '?'}"
            f"  seller: {L['seller_type'] or 'unknown'}"]

    missing = [m["field"] for m in conn.execute(
        "SELECT field FROM missing_fields WHERE listing_id=? ORDER BY field", (L["id"],))]
    if missing:
        out.append(f"  MISSING:      {', '.join(missing)}")

    # --- comps ---
    out += ["", "COMPARABLE TRANSACTIONS", "-" * 76]
    if val["status"] != "ok":
        out.append("  INSUFFICIENT DATA -- no valuation produced.")
        out.append("")
        out += _gap_report(conn, L, val)
        out += ["", _line(), "RESULT: INSUFFICIENT DATA",
                "No maximum offer, total investment or net profit is stated,",
                "because there is no verified basis on which to state one.", _line()]
        return "\n".join(out), False

    comps_used = val["detail"]["comps"]
    out.append(f"  {len(comps_used)} usable comps "
               f"({val['confidence']} confidence). Selection: "
               f"{val['detail']['selection'].get('used')}")
    out.append("")
    out.append(f"  {'date':12} {'yr':5} {'variant':13} {'miles':>8} {'sold':>10} "
               f"{'adjusted':>10}  basis")
    for c in comps_used:
        miles = f"{c['mileage']:,}" if c["mileage"] else "?"
        out.append(f"  {c['sale_date']:12} {str(c['year'] or '?'):5} "
                   f"{(c['variant'] or '?'):13} {miles:>8} "
                   f"${c['sale_price']:>9,.0f} ${c['adjusted_value']:>9,.0f}  "
                   f"{c['price_basis']}/{c['permission_basis']}")
        out.append(f"       {c['source_url']}")
        for a in c["adjustments"]:
            if a["amount"]:
                out.append(f"         {a['kind']:>14}: {a['amount']:+,.0f}  ({a['basis']})")

    out += ["", "VALUATION", "-" * 76,
            f"  Method:            {val['method']} (trimmed median of adjusted comps)",
            f"  Comp value:        ${val['point_value']:,.0f}",
            f"  Adjusted range:    ${val['low_value']:,.0f} - ${val['high_value']:,.0f}",
            f"  Confidence:        {val['confidence']} on {val['n_comps']} comps"]

    # --- underwriting ---
    d = res["detail"]
    out += ["", "UNDERWRITING", "-" * 76,
            f"  Expected resale:   ${res['expected_resale']:,.0f}   "
            f"(comp value less {d['resale_haircut_pct']:.0%} haircut)"]
    out.append("")
    out.append(f"  {'cost line':26} {'amount':>10}  status")
    for l in d["cost_lines"]:
        out.append(f"  {l['key']:26} ${l['amount']:>9,.0f}  "
                   f"{'verified' if l['verified'] else 'UNVERIFIED'}")
    for r in d["rate_lines"]:
        if r["amount"]:
            out.append(f"  {r['key']:26} ${r['amount']:>9,.0f}  "
                       f"{'verified' if r['verified'] else 'UNVERIFIED'} "
                       f"({r['rate']:.2%} of purchase)")
    out.append(f"  {'purchase price':26} ${res['asking_price'] or 0:>9,.0f}  actual ask")
    out.append(f"  {'TOTAL INVESTMENT':26} ${res['total_cost'] or 0:>9,.0f}")

    target = res["target_net_profit"]
    out += ["", "RESULT", "-" * 76,
            f"  Expected resale:       ${res['expected_resale']:,.0f}",
            f"  Total investment:      ${res['total_cost'] or 0:,.0f}",
            f"  PROJECTED NET PROFIT:  ${res['net_profit'] or 0:,.0f}  "
            f"(target ${target:,.0f})",
            f"  MAXIMUM OFFER:         ${res['max_purchase_price']:,.0f}  "
            f"-- pay more than this and the ${target:,.0f} target is not met",
            f"  Meets threshold:       {'YES' if res['meets_threshold'] else 'NO'}"]

    unver = d["unverified_lines"]
    out += ["", "STATUS OF THIS NUMBER", "-" * 76]
    if unver:
        out.append(f"  PROVISIONAL -- NOT CONFIRMED PROFIT.")
        out.append(f"  {len(unver)} cost inputs are unverified placeholders: "
                   f"{', '.join(unver)}")
        out.append("  Replace them with real quotes (`sheet`, then `assumptions --set` "
                   "or `override`) before treating this as a decision.")
    else:
        out.append("  All cost inputs are marked verified.")
    out.append(f"  Valuation confidence: {val['confidence']} "
               f"({val['n_comps']} verified comps)")
    out.append("  The model does not price options, service history, accident "
               "history or paint condition.")
    out.append(_line())
    return "\n".join(out), bool(res["meets_threshold"]) or res["status"] == "ok"

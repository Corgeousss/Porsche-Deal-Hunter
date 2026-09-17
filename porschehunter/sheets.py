"""One-page input sheet for the mechanic, and the cost-assumption screen.

The sheet is the thing you hand (or email) to your friend. It lists every cost
line the underwriting model uses, shows what the model is currently assuming,
and leaves a blank for the real number. Anything they fill in goes back via
`assumptions --set` (global) or `override` (that one car).
"""

from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path

from . import db as _db

# (section, assumption_key, question asked of the mechanic)
SHEET_ROWS = [
    ("Inspection", "ppi_cost",
     "Pre-purchase inspection: your charge for a full PPI on a 911 "
     "(compression/leakdown, bore scope where applicable, code scan, lift check)?"),

    ("Repairs - by category", "repairs_996",
     "996: typical reconditioning budget. Note IMS/RMS and bore-score exposure "
     "separately if you price it that way."),
    ("Repairs - by category", "repairs_997",
     "997: typical reconditioning budget (note .1 vs .2 if they differ)."),
    ("Repairs - by category", "repairs_991",
     "991: typical reconditioning budget."),
    ("Repairs - by category", "repairs_default",
     "Any other 911 generation: typical reconditioning budget."),

    ("Cosmetics", "detail_and_photography",
     "Paint correction + full detail + a listing-quality photo set: your charge?"),

    ("Shipping", "transport_base",
     "Enclosed transport: the fixed/booking component of a typical quote."),
    ("Shipping", "transport_per_mile",
     "Enclosed transport: your typical rate per road mile."),

    ("Selling costs", "sale_fee_pct",
     "Seller-side fee as a fraction of sale price (auction commission, "
     "marketplace fee or consignment). 0.05 = 5%."),
    ("Selling costs", "sale_fee_cap",
     "Cap on that percentage fee, if the channel has one."),
    ("Selling costs", "title_and_admin",
     "Title, temporary registration, notary, document shipping."),

    ("Holding costs", "days_to_sell",
     "Realistic days from purchase to funds received."),
    ("Holding costs", "carrying_cost_per_day",
     "Storage + insurance + cost of capital, per day, while we hold it."),

    ("Transaction costs", "purchase_tax_pct",
     "Transaction/use tax rate on the PURCHASE. Enter 0 ONLY if we hold a "
     "resale/dealer exemption -- confirm with the accountant, this is material."),
    ("Transaction costs", "dealer_doc_fee",
     "Typical dealer documentation/processing fee when buying from a dealer."),
    ("Transaction costs", "auction_buyer_premium_pct",
     "Buyer premium we pay when buying at auction. 0.05 = 5%."),

    ("Risk", "risk_reserve_pct",
     "Reserve for undisclosed faults found on arrival, as a fraction of the "
     "purchase price. 0.06 = 6%."),
    ("Risk", "resale_haircut_pct",
     "Discount off comparable market value to achieve a timely sale."),
]


def build_rows(conn) -> list[dict]:
    current = {r["key"]: r for r in _db.all_assumptions(conn)}
    rows = []
    for section, key, question in SHEET_ROWS:
        a = current.get(key)
        if a is None:
            continue
        rows.append({
            "section": section,
            "key": key,
            "question": question,
            "model_currently_assumes": a["value"],
            "unit": a["unit"],
            "status": "VERIFIED" if a["verified"] else "UNVERIFIED - PLACEHOLDER",
            "your_number": "",
            "notes": "",
        })
    return rows


def write_csv(conn, path: Path) -> Path:
    rows = build_rows(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "section", "key", "question", "model_currently_assumes", "unit",
            "status", "your_number", "notes"])
        w.writeheader()
        w.writerows(rows)
    return path


def write_html(conn, path: Path, target: float | None = None) -> Path:
    import html as _html
    rows = build_rows(conn)
    target = target if target is not None else _db.get_assumption(conn, "target_net_profit")
    n_unver = sum(1 for r in rows if r["status"].startswith("UNVERIFIED"))
    today = _dt.date.today().isoformat()

    css = """
    @page{size:letter;margin:12mm}
    body{font:11px/1.35 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;margin:0}
    h1{font-size:16px;margin:0 0 2px}
    .sub{color:#555;font-size:10px;margin-bottom:8px}
    .note{background:#fff4e5;border:1px solid #e8c48a;padding:6px 8px;
          border-radius:4px;margin-bottom:8px;font-size:10px}
    table{border-collapse:collapse;width:100%}
    th,td{border:1px solid #ccc;padding:3px 5px;vertical-align:top}
    th{background:#f0f0f0;text-align:left;font-size:10px}
    td.q{width:40%}
    td.fill{width:16%;background:#fcfcf2}
    .sec{background:#e8eaed;font-weight:600}
    .un{color:#8a5a00;font-weight:600;font-size:9px}
    .ve{color:#137333;font-weight:600;font-size:9px}
    .num{text-align:right;font-variant-numeric:tabular-nums}
    """
    out = [f"<!doctype html><meta charset=utf-8><title>911 Cost Input Sheet</title>",
           f"<style>{css}</style>",
           "<h1>Porsche 911 acquisition &mdash; cost input sheet</h1>",
           f"<div class=sub>Prepared {today} &middot; target net profit "
           f"${target:,.0f} per car, after ALL costs below</div>",
           f"<div class=note><b>What I need:</b> your real number in the "
           f"&ldquo;Your number&rdquo; column for anything you can price. "
           f"{n_unver} of these are currently placeholders I made up so the model "
           f"would run &mdash; they are not estimates from you and should not be "
           f"treated as quotes. Anything you leave blank stays flagged as "
           f"unverified in our underwriting.</div>",
           "<table><tr><th>Cost line</th><th class=q>Question</th>"
           "<th>Model assumes</th><th>Unit</th><th>Status</th>"
           "<th>Your number</th><th>Notes</th></tr>"]
    section = None
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            out.append(f"<tr class=sec><td colspan=7>{_html.escape(section)}</td></tr>")
        cls = "un" if r["status"].startswith("UNVERIFIED") else "ve"
        short = "UNVERIFIED" if r["status"].startswith("UNVERIFIED") else "VERIFIED"
        val = r["model_currently_assumes"]
        val_s = f"{val:,.4g}" if isinstance(val, (int, float)) else str(val)
        out.append(
            f"<tr><td><code>{_html.escape(r['key'])}</code></td>"
            f"<td class=q>{_html.escape(r['question'])}</td>"
            f"<td class=num>{val_s}</td><td>{_html.escape(r['unit'])}</td>"
            f"<td class='{cls}'>{short}</td><td class=fill></td><td class=fill></td></tr>")
    out.append("</table>")
    out.append("<div class=note style='margin-top:8px'><b>Per-car work:</b> "
               "for a specific car we can override the generic repair and "
               "transport numbers with a real quote &mdash; that is what actually "
               "moves a deal from &ldquo;maybe&rdquo; to &ldquo;buy&rdquo;. "
               "Quote per car where you can.</div>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(out))
    return path

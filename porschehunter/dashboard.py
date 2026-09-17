"""Minimal read-only dashboard. Standard library http.server, no frameworks.

Shows: real listings in the database, their comp-derived value, the projected
net profit, the maximum bid, source links, refresh timestamps, missing fields
and every unverified assumption in play.
"""

from __future__ import annotations

import html
import http.server
import json
import socketserver
import urllib.parse
from pathlib import Path

from . import db as _db
from . import deal as _deal

CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f6f7;color:#16181d}
header{background:#16181d;color:#fff;padding:14px 20px}
header h1{margin:0;font-size:17px;font-weight:600}
header .sub{color:#9aa0a6;font-size:12px;margin-top:3px}
main{padding:18px 20px;max-width:1180px}
.banner{background:#fff4e5;border:1px solid #f0c48a;padding:10px 12px;border-radius:5px;margin-bottom:14px}
.banner.bad{background:#fdecea;border-color:#e6a49c}
.card{background:#fff;border:1px solid #dfe1e5;border-radius:6px;padding:14px;margin-bottom:12px}
.card.hit{border-left:4px solid #1a7f37}
.card.cold{opacity:.85}
.card h3{margin:0 0 6px;font-size:15px}
.meta{color:#5f6368;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #eceef0}
th{color:#5f6368;font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600}
.pill.go{background:#e6f4ea;color:#137333}.pill.no{background:#f1f3f4;color:#5f6368}
.pill.warn{background:#fef7e0;color:#976800}
ul.why{margin:8px 0 0;padding-left:18px;color:#3c4043}
ul.why li{margin:2px 0}
a{color:#1a5fb4}
.k{color:#5f6368}
code{background:#f1f3f4;padding:1px 4px;border-radius:3px}
"""


def _m(v):
    return "&mdash;" if v is None else f"${v:,.0f}"


def _esc(v):
    return html.escape(str(v)) if v is not None else "&mdash;"


def render(conn, destination_state: str, include_synthetic: bool = False) -> str:
    results = _deal.evaluate_all(conn, destination_state=destination_state,
                                 include_synthetic=include_synthetic)
    results.sort(key=lambda r: -(r.get("net_profit") or -1e9))

    n_comps = conn.execute("SELECT COUNT(*) c FROM comps WHERE is_synthetic=0").fetchone()["c"]
    n_synth = conn.execute("SELECT COUNT(*) c FROM comps WHERE is_synthetic=1").fetchone()["c"]
    target = _db.get_assumption(conn, "target_net_profit")
    unverified = [r for r in _db.all_assumptions(conn) if not r["verified"]]
    hits = [r for r in results if r["meets_threshold"]]

    out = [f"<!doctype html><meta charset=utf-8><title>911 Deal Hunter</title><style>{CSS}</style>",
           "<header><h1>Porsche 911 Deal Hunter</h1>",
           f"<div class=sub>{len(results)} active listings &middot; {n_comps} documented comps "
           f"&middot; target net {_m(target)} &middot; destination {html.escape(destination_state)}"
           f" &middot; generated {_db.utcnow()}</div></header><main>"]

    if n_comps == 0:
        out.append("<div class='banner bad'><b>No comparable sales recorded.</b> "
                   "Nothing can be valued. Add documented completed sales with "
                   "<code>comps add</code> &mdash; this tool will not estimate a value "
                   "without real transactions behind it.</div>")
    if include_synthetic:
        out.append("<div class='banner bad'><b>PREVIEW MODE &mdash; SYNTHETIC TEST DATA IN USE.</b> "
                   "Synthetic comps are being fed into these valuations so the layout can be "
                   "inspected. Nothing on this page reflects a real vehicle or a real sale. "
                   "Never run the dashboard this way for an actual buying decision.</div>")
    elif n_synth:
        out.append(f"<div class='banner bad'><b>{n_synth} synthetic test comps are in this "
                   "database.</b> They are excluded from every valuation, but this database "
                   "is not production-clean.</div>")
    out.append(f"<div class=banner><b>{len(unverified)} unverified assumptions</b> feed these "
               "numbers (repair budgets, transport rates, fees, carrying cost, the $/mile "
               "mileage adjustment). Every profit figure below is a screen, not a quote. "
               "See the assumption table at the bottom.</div>")

    out.append(f"<h2 style='font-size:15px'>Opportunities &ge; {_m(target)} net ({len(hits)})</h2>")
    if not hits:
        out.append("<div class=card>No listing currently clears the threshold.</div>")

    for r in results:
        L = r["listing"]
        cls = "card hit" if r["meets_threshold"] else "card cold"
        pill = ("<span class='pill go'>MEETS TARGET</span>" if r["meets_threshold"]
                else f"<span class='pill no'>{_esc(r['status'])}</span>")
        title = L["title"] or f"{L['year'] or ''} 911 {L['variant'] or ''}".strip() or L["url"]
        missing = [m["field"] for m in conn.execute(
            "SELECT field FROM missing_fields WHERE listing_id=? ORDER BY field", (L["id"],))]
        photo = conn.execute(
            "SELECT url FROM listing_photos WHERE listing_id=? ORDER BY position LIMIT 1",
            (L["id"],)).fetchone()

        out.append(f"<div class='{cls}'><h3>#{L['id']} {_esc(title)} {pill}</h3>")
        miles_txt = f"{L['mileage']:,} mi" if L["mileage"] else None
        out.append(f"<div class=meta>{_esc(L['generation'])} &middot; "
                   f"{_esc(miles_txt)} &middot; "
                   f"{_esc(L['transmission'])} &middot; "
                   f"{_esc(L['seller_city'])} {_esc(L['seller_state'])} &middot; "
                   f"VIN {_esc(L['vin'])}</div>")
        out.append(f"<div class=meta>source <b>{_esc(L['source_key'])}</b> &middot; "
                   f"<a href='{html.escape(L['url'])}' target=_blank rel=noopener>listing</a> "
                   f"&middot; first seen {_esc(L['first_seen_at'])} &middot; "
                   f"refreshed {_esc(L['fetched_at'])}</div>")
        if photo:
            out.append(f"<div class=meta>photo: <a href='{html.escape(photo['url'])}' "
                       f"target=_blank rel=noopener>{html.escape(photo['url'][:70])}</a></div>")
        if missing:
            out.append(f"<div class=meta>missing: <span class='pill warn'>"
                       f"{', '.join(missing)}</span></div>")

        if r["status"] == "ok":
            out.append("<table><tr><th>ask</th><th>comp value</th><th>expected resale</th>"
                       "<th>costs</th><th>net profit</th><th>max bid</th><th>confidence</th></tr>"
                       f"<tr class=num><td>{_m(r['asking_price'])}</td>"
                       f"<td>{_m(r['comp_value'])}</td><td>{_m(r['expected_resale'])}</td>"
                       f"<td>{_m(r['total_cost'])}</td><td><b>{_m(r['net_profit'])}</b></td>"
                       f"<td><b>{_m(r['max_purchase_price'])}</b></td>"
                       f"<td>{_esc(r['detail']['valuation_confidence'])} "
                       f"({r['detail']['valuation_n_comps']} comps)</td></tr></table>")
            out.append("<table><tr><th>cost line</th><th class=num>amount</th><th>basis</th></tr>")
            for line in r["detail"]["cost_lines"]:
                tag = "" if line["verified"] else " <span class='pill warn'>UNVERIFIED</span>"
                out.append(f"<tr><td>{_esc(line['key'])}{tag}</td>"
                           f"<td class=num>{_m(line['amount'])}</td>"
                           f"<td class=k>{_esc(line['basis'])}</td></tr>")
            out.append(f"<tr><td>risk reserve</td><td class=num>{_m(r['risk_reserve'])}</td>"
                       f"<td class=k>{r['detail']['risk_reserve_pct']:.1%} of the purchase price"
                       f" <span class='pill warn'>UNVERIFIED</span></td></tr></table>")
            out.append("<ul class=why>" + "".join(
                f"<li>{_esc(x)}</li>" for x in r["detail"]["explanation"]) + "</ul>")

            comps = (r["valuation"]["detail"].get("comps") or [])[:8]
            if comps:
                out.append("<table><tr><th>comps used (documented completed sales)</th>"
                           "<th>date</th><th class=num>sold</th><th class=num>adjusted</th>"
                           "<th class=num>miles</th></tr>")
                for c in comps:
                    comp_miles = f"{c['mileage']:,}" if c["mileage"] else None
                    out.append(
                        f"<tr><td><a href='{html.escape(c['source_url'])}' target=_blank "
                        f"rel=noopener>{_esc(c['venue'])} #{c['comp_id']}</a> "
                        f"{_esc(c['year'])} {_esc(c['variant'])}</td>"
                        f"<td>{_esc(c['sale_date'])}</td>"
                        f"<td class=num>{_m(c['sale_price'])}</td>"
                        f"<td class=num>{_m(c['adjusted_value'])}</td>"
                        f"<td class=num>{_esc(comp_miles)}</td></tr>")
                out.append("</table>")
        else:
            out.append(f"<div class=meta><b>Not evaluated:</b> "
                       f"{_esc(r['detail'].get('explanation'))}</div>")
        out.append("</div>")

    # --- source access panel ------------------------------------------------
    out.append("<h2 style='font-size:15px'>Source access</h2><div class=card><table>"
               "<tr><th>source</th><th>access</th><th>authorized</th><th>enabled</th>"
               "<th>last success</th><th>limitations</th></tr>")
    for s in conn.execute("SELECT * FROM sources ORDER BY key"):
        out.append(f"<tr><td>{_esc(s['key'])}</td><td>{_esc(s['access_method'])}</td>"
                   f"<td>{_esc(s['authorized'])}</td><td>{'yes' if s['enabled'] else 'no'}</td>"
                   f"<td>{_esc(s['last_success_at'] or 'never')}</td>"
                   f"<td class=k>{_esc(s['limitations'])}</td></tr>")
    out.append("</table></div>")

    # --- assumptions --------------------------------------------------------
    out.append("<h2 style='font-size:15px'>Assumptions</h2><div class=card><table>"
               "<tr><th>key</th><th class=num>value</th><th>unit</th><th>status</th>"
               "<th>basis</th></tr>")
    for a in _db.all_assumptions(conn):
        tag = ("<span class='pill go'>verified</span>" if a["verified"]
               else "<span class='pill warn'>UNVERIFIED</span>")
        out.append(f"<tr><td>{_esc(a['key'])}</td><td class=num>{a['value']:,.4g}</td>"
                   f"<td>{_esc(a['unit'])}</td><td>{tag}</td>"
                   f"<td class=k>{_esc(a['basis'])}</td></tr>")
    out.append("</table></div></main>")
    return "".join(out)


def serve(db_path, host="127.0.0.1", port=8000, destination_state="OH",
          include_synthetic: bool = False):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            conn = _db.connect(db_path)
            try:
                if path == "/api/deals.json":
                    body = json.dumps(
                        _deal.evaluate_all(conn, destination_state=destination_state,
                                           include_synthetic=include_synthetic),
                        default=str, indent=2).encode()
                    ctype = "application/json"
                elif path in ("/", "/index.html"):
                    body = render(conn, destination_state, include_synthetic).encode()
                    ctype = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer((host, port), Handler) as httpd:
        print(f"Dashboard on http://{host}:{port}  (destination state: {destination_state})")
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")

"""Command line interface. `python -m porschehunter <command>`"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import acceptance as _acceptance
from . import comps as _comps
from . import db as _db
from . import deal as _deal
from . import valuation as _valuation
from . import vin as _vin
from .assumptions import BY_KEY
from . import sheets as _sheets
from . import validate as _validate
from . import searches as _searches
from . import recon as _recon
from .sources import classic_com as _classic
from .sources import jsonld as _jsonld
from .sources import manual as _manual
from .sources import marketcheck as _marketcheck
from .sources import rss as _rss

DEST_STATE_HELP = ("Two-letter state where your friend's shop is. Drives the "
                   "transport estimate.")


def _conn(args):
    return _db.connect(args.db)


def _money(v):
    return "-" if v is None else f"${v:,.0f}"


# ---------------------------------------------------------------------------
def cmd_init(args):
    conn = _conn(args)
    _db.init_db(conn)
    Path("data").mkdir(exist_ok=True)
    tmpl = Path("data/comps_template.csv")
    if not tmpl.exists():
        _comps.write_template(tmpl)
    print(f"Initialised {args.db}")
    print(f"Comp import template written to {tmpl} (header only -- no sample rows, by design).")
    print("\nNext: record real completed sales with `comps add`, then `sources` to see access status.")
    return 0


def cmd_sources(args):
    conn = _conn(args)
    rows = conn.execute("SELECT * FROM sources ORDER BY authorized, key").fetchall()
    for r in rows:
        flag = {"yes": "OK", "yes_with_credentials": "KEY", "operator_enabled": "OPT-IN",
                "manual_only": "MANUAL", "prohibited": "BLOCKED"}.get(r["authorized"], "?")
        state = "enabled" if r["enabled"] else "disabled"
        print(f"[{flag:7}] {r['key']:22} {r['name']}  ({state})")
        print(f"          access: {r['access_method']}   cost: {r['cost_notes']}")
        print(f"          limits: {r['limitations']}")
        lv = r["live_verified_at"]
        print(f"          last success: {r['last_success_at'] or 'never'}"
              f"   last attempt: {r['last_attempt_at'] or 'never'}")
        if lv:
            print(f"          LIVE-VERIFIED: {lv} -- {r['live_verified_note']}")
        elif r["access_method"] in ("api", "rss", "jsonld"):
            print("          LIVE-VERIFIED: NO -- this connector has never "
                  "completed a real call to a real endpoint.")
        if r["last_error"]:
            print(f"          last error: {r['last_error'][:160]}")
        print()
    return 0


def cmd_enable(args):
    conn = _conn(args)
    row = conn.execute("SELECT * FROM sources WHERE key=?", (args.source,)).fetchone()
    if row is None:
        print(f"Unknown source {args.source}", file=sys.stderr)
        return 1
    if row["authorized"] == "prohibited" and args.on:
        print(f"Refusing to enable {args.source}: its terms prohibit automated "
              f"extraction. Use manual entry.", file=sys.stderr)
        return 1
    conn.execute("UPDATE sources SET enabled=? WHERE key=?", (int(args.on), args.source))
    conn.commit()
    print(f"{args.source} {'enabled' if args.on else 'disabled'}")
    return 0


# ---------------------------------------------------------------------------
def cmd_add(args):
    conn = _conn(args)
    lid, created, report = _manual.add_listing(
        conn, args.url, title=args.title, year=args.year, generation=args.generation,
        variant=args.variant, body_style=args.body, transmission=args.transmission,
        mileage=args.mileage, vin=args.vin, price=args.price,
        seller_type=args.seller_type, seller_city=args.city, seller_state=args.state,
        seller_zip=args.zip, listing_type=args.listing_type, notes=args.notes,
        photos=args.photo or [], decode_vin=not args.no_vin_decode)
    print(f"{'Added' if created else 'Updated'} listing #{lid} (source: {report['source_key']})")
    for n in report.get("notices", []):
        print(f"  ! {n}")
    if report.get("missing_fields"):
        print(f"  missing: {', '.join(report['missing_fields'])}")
    return 0


def cmd_fetch(args):
    conn = _conn(args)
    source = args.source
    run_id = _db.start_run(conn, source)
    try:
        if source == "dealer_jsonld":
            only_911 = not args.all_models
            if args.domain:
                res = _jsonld.ingest_domain(conn, args.domain,
                                            max_pages=args.max_pages,
                                            only_911=only_911)
            else:
                res = _jsonld.ingest(conn, args.url or [], only_911=only_911)
        elif source == "craigslist_rss":
            res = _rss.ingest(conn)
        elif source == "marketcheck":
            res = _marketcheck.ingest(conn, kind=args.kind,
                                      year_min=args.year_min, year_max=args.year_max,
                                      max_rows=args.max_rows)
        elif source == "manual":
            res = _manual.ingest(conn, args.url or [])
        else:
            print(f"Source '{source}' has no automated connector -- use `add` with a URL.",
                  file=sys.stderr)
            _db.finish_run(conn, run_id, "skipped", message="no connector")
            return 1
    except Exception as exc:
        _db.finish_run(conn, run_id, "error", message=f"{type(exc).__name__}: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _db.finish_run(conn, run_id, res.status, res.seen, res.new, res.updated, res.message)
    print(f"{source}: seen={res.seen} new={res.new} updated={res.updated} status={res.status}")
    if res.message:
        print(res.message.strip())
    return 0 if res.status != "error" else 1


# ---------------------------------------------------------------------------
def cmd_comps_add(args):
    conn = _conn(args)
    try:
        cid = _comps.add_comp(
            conn, generation=args.generation, variant=args.variant, year=args.year,
            body_style=args.body, transmission=args.transmission, mileage=args.mileage,
            sale_price=args.price, sale_date=args.date, venue=args.venue,
            source_url=args.url, vin=args.vin, condition_note=args.note,
            includes_fees=args.includes_fees, recorded_by=args.by,
            price_basis=args.price_basis, permission_basis=args.permission_basis,
            permission_note=args.permission_note)
    except _comps.CompRejected as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1
    usable = (args.price_basis == _comps.USABLE_PRICE_BASIS
              and args.permission_basis in _comps.USABLE_PERMISSION_BASES)
    print(f"Recorded comp #{cid}: {args.generation} {args.variant or ''} "
          f"{_money(float(args.price))} on {args.date}")
    print(f"  price_basis={args.price_basis}  permission_basis={args.permission_basis}")
    if usable:
        print("  -> USABLE in valuations.")
    else:
        print("  -> NOT usable in valuations. " +
              ("This is not a verified transaction price. "
               if args.price_basis != _comps.USABLE_PRICE_BASIS else "") +
              ("No permission basis recorded. "
               if args.permission_basis not in _comps.USABLE_PERMISSION_BASES else ""))
    return 0


def cmd_comps_import(args):
    conn = _conn(args)
    n, errors = _comps.import_csv(conn, Path(args.path))
    print(f"Imported {n} comps from {args.path}")
    for e in errors:
        print(f"  rejected {e}", file=sys.stderr)
    return 0 if not errors else 1


def cmd_comps_coverage(args):
    conn = _conn(args)
    rows = _comps.coverage(conn)
    if not rows:
        print("No comps recorded. Nothing can be valued until you add documented "
              "completed sales (`comps add` or `comps import`).")
        return 0
    print(f"{'gen':8} {'variant':16} {'usable':>6} {'stored':>6} "
          f"{'not-a-sale':>10} {'no-perm':>7}  oldest      newest")
    tot_u = tot_s = 0
    for r in rows:
        tot_u += r["n_usable"] or 0
        tot_s += r["n_stored"] or 0
        print(f"{r['generation']:8} {(r['variant'] or '-'):16} "
              f"{r['n_usable'] or 0:>6} {r['n_stored']:>6} "
              f"{r['n_not_a_sale'] or 0:>10} {r['n_no_permission'] or 0:>7}  "
              f"{r['oldest']}  {r['newest']}")
    print(f"\n{tot_u} of {tot_s} stored comps are usable in valuations.")
    if tot_s and not tot_u:
        print("None are usable. A comp needs price_basis=verified_transaction AND "
              "a permission_basis other than 'unknown'.")
    return 0


# ---------------------------------------------------------------------------
def cmd_value(args):
    conn = _conn(args)
    row = conn.execute("SELECT * FROM listings WHERE id=?", (args.listing_id,)).fetchone()
    if row is None:
        print(f"No listing #{args.listing_id}", file=sys.stderr)
        return 1
    res = _valuation.value_listing(conn, row)
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_scan(args):
    conn = _conn(args)
    results = _deal.evaluate_all(conn, destination_state=args.destination)
    target = _db.get_assumption(conn, "target_net_profit")

    underwritten = [r for r in results
                    if r.get("underwriting_status") == "underwritten"
                    and r["meets_threshold"]]
    preliminary = [r for r in results
                   if r.get("underwriting_status") == "preliminary"
                   and r["meets_threshold"]]
    below = [r for r in results if r["status"] == "ok" and not r["meets_threshold"]]
    unvalued = [r for r in results if r["status"] != "ok"]

    print(f"Evaluated {len(results)} active listings against a "
          f"{_money(target)} net-profit target.\n")

    def show(r, tag):
        L = r["listing"]
        print(f"\n[{tag}] #{L['id']} {L['title'] or L['url']}")
        print(f"  ask {_money(r['asking_price'])} | comp value {_money(r['comp_value'])} "
              f"| resale {_money(r['expected_resale'])}")
        tax_note = "  (BEFORE PURCHASE TAX)" if r.get("before_purchase_tax") else ""
        print(f"  NET {_money(r['net_profit'])}{tax_note}   "
              f"max bid {_money(r['max_purchase_price'])}")
        print(f"  {L['url']}")
        for line in r["detail"]["explanation"]:
            print(f"    - {line}")

    if underwritten:
        print(f"=== {len(underwritten)} FULLY UNDERWRITTEN OPPORTUNITIES ===")
        print("Every cost input confirmed, purchase tax set, comps sufficient.")
        for r in sorted(underwritten, key=lambda x: -(x["net_profit"] or 0)):
            show(r, "UNDERWRITTEN")

    if preliminary:
        print(f"\n=== {len(preliminary)} PRELIMINARY OPPORTUNITIES ===")
        print("These clear the target ON PAPER but are NOT fully underwritten. "
              "Treat as leads to investigate, not confirmed profit.")
        for r in sorted(preliminary, key=lambda x: -(x["net_profit"] or 0)):
            show(r, "PRELIMINARY")
            print(f"    BLOCKING: {'; '.join(r['underwriting_blockers'])}")

    if not underwritten and not preliminary:
        print("No listing clears the profit threshold.")

    if below:
        print(f"\n=== {len(below)} BELOW TARGET ===")
        for r in below:
            L = r["listing"]
            print(f"  #{L['id']} {L['title'] or L['url']}: net "
                  f"{_money(r['net_profit'])} vs {_money(target)} target")

    if unvalued:
        print(f"\n=== {len(unvalued)} UNVALUED ===")
        print("No verified comparable sales, so no valuation was produced.")
        for r in unvalued:
            L = r["listing"]
            print(f"  #{L['id']} {L['year'] or '????'} 911 {L['variant'] or '?'} "
                  f"{_money(L['price'])} {L['mileage'] or '?'} mi "
                  f"{L['seller_state'] or '?'}")
            print(f"      {L['url']}")
            print(f"      {r['detail'].get('explanation')}")
    return 0


def cmd_listings(args):
    """Real listings as they are: spec, price, location, source URL, and an
    explicit valuation status. Nothing is valued that cannot be valued."""
    conn = _conn(args)
    sql = "SELECT * FROM listings"
    params = []
    if args.generation:
        sql += " WHERE generation LIKE ?"
        params.append(args.generation + "%")
    sql += " ORDER BY last_seen_at DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("No listings in the database.")
        print("Add one:   porschehunter add <listing-url> --title ... --price ...")
        print("Or fetch:  porschehunter fetch marketcheck --kind fsbo")
        return 0

    n_unvalued = 0
    for r in rows:
        val = conn.execute(
            """SELECT * FROM valuations WHERE listing_id=?
               ORDER BY computed_at DESC LIMIT 1""", (r["id"],)).fetchone()
        if val is None or val["status"] != "ok":
            status = "UNVALUED - no usable comparable sales"
            n_unvalued += 1
        else:
            status = (f"valued ${val['point_value']:,.0f} "
                      f"({val['confidence']}, {val['n_comps']} comps)")

        miles = f"{r['mileage']:,} mi" if r["mileage"] else "mileage unknown"
        price = f"${r['price']:,.0f}" if r["price"] else "price unknown"
        loc = ", ".join(x for x in (r["seller_city"], r["seller_state"]) if x) or "location unknown"
        print(f"\n#{r['id']}  {r['year'] or '????'} 911 {r['variant'] or '(variant unknown)'}"
              f"  [{r['generation'] or 'gen?'}]")
        print(f"    price:    {price}")
        print(f"    mileage:  {miles}")
        print(f"    location: {loc}    seller: {r['seller_type'] or 'unknown'}")
        print(f"    VIN:      {r['vin'] or 'unknown'}")
        print(f"    source:   {r['source_key']}   retrieved {r['fetched_at']}")
        print(f"    URL:      {r['url']}")
        print(f"    status:   {status}")
        missing = [m["field"] for m in conn.execute(
            "SELECT field FROM missing_fields WHERE listing_id=? ORDER BY field",
            (r["id"],))]
        if missing:
            print(f"    missing:  {', '.join(missing)}")

    print(f"\n{len(rows)} listing(s); {n_unvalued} unvalued.")
    if n_unvalued:
        print("Unvalued means no verified comparable sales exist for that car. "
              "No valuation is shown because none can be computed.")
    return 0


def cmd_override(args):
    conn = _conn(args)
    row = conn.execute("SELECT id FROM listings WHERE id=?", (args.listing_id,)).fetchone()
    if row is None:
        print(f"No listing #{args.listing_id}", file=sys.stderr)
        return 1
    if args.repairs is None and args.transport is None and args.days is None:
        cur = _db.get_override(conn, args.listing_id)
        if not cur:
            print(f"No overrides recorded for listing #{args.listing_id}.")
            return 0
        for k in ("repairs", "transport", "days_to_sell", "note", "updated_at"):
            if cur.get(k) is not None:
                print(f"  {k:14} {cur[k]}")
        return 0
    _db.set_override(conn, args.listing_id, repairs=args.repairs,
                     transport=args.transport, days_to_sell=args.days, note=args.note)
    print(f"Recorded real quotes for listing #{args.listing_id}. These replace the "
          f"generic placeholders and are treated as verified.")
    return 0


def cmd_vin(args):
    conn = _conn(args)
    res = _vin.decode_and_store(conn, args.vin, force=args.force)
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_assumptions(args):
    conn = _conn(args)
    if args.set:
        key, value = args.set
        try:
            _db.set_assumption(conn, key, float(value), basis=args.basis,
                               verified=args.verified)
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{key} = {value}" + ("  [marked VERIFIED]" if args.verified else ""))
        return 0
    rows = _db.all_assumptions(conn)
    tags = {"verified": "VERIFIED  ", "unset": "NOT SET   ",
            "placeholder": "UNVERIFIED"}
    for r in rows:
        status = r["status"] if "status" in r.keys() else (
            "verified" if r["verified"] else "placeholder")
        print(f"[{tags.get(status, 'UNVERIFIED')}] {r['key']:32} "
              f"{r['value']:>12,.4g} {r['unit']}")
        print(f"             {r['basis']}")
    unset = _db.unset_assumptions(conn)
    unver = sum(1 for r in rows
                if not r["verified"] and r["key"] not in unset)
    print()
    if unset:
        print(f"{len(unset)} assumption(s) are NOT SET and have no honest default: "
              f"{', '.join(unset)}.")
        print("No deal can be fully underwritten until you set them. For tax:")
        print("  porschehunter assumptions --set purchase_tax_pct 0 "
              "--basis \"resale exemption confirmed <date>\" --verified")
        print("  porschehunter assumptions --set purchase_tax_pct 0.065 "
              "--basis \"OH combined rate confirmed <date>\" --verified")
    print(f"{unver} assumptions are unverified placeholders. Replace them with "
          f"your own quotes before trusting any profit figure.")
    return 0


def cmd_status(args):
    conn = _conn(args)
    n_listings = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    n_comps = conn.execute("SELECT COUNT(*) c FROM comps WHERE is_synthetic=0").fetchone()["c"]
    n_synth = conn.execute("SELECT COUNT(*) c FROM comps WHERE is_synthetic=1").fetchone()["c"]
    print(f"listings: {n_listings}   documented comps: {n_comps}")
    if n_synth:
        print(f"  WARNING: {n_synth} synthetic test comps present in this database. "
              f"They are excluded from valuations, but this DB is not production-clean.")
    print("\nsource access:")
    for r in conn.execute("SELECT * FROM sources ORDER BY key"):
        print(f"  {r['key']:22} enabled={bool(r['enabled'])!s:<5} "
              f"live_verified={r['live_verified_at'] or 'NEVER':<26} "
              f"last_success={r['last_success_at'] or 'never'}")
    n_lv = conn.execute(
        "SELECT COUNT(*) c FROM sources WHERE live_verified_at IS NOT NULL").fetchone()["c"]
    if n_lv == 0:
        print("  -> No connector has completed a real endpoint call. "
              "Run `validate` on a networked machine.")
    print("\nrecent runs:")
    for r in conn.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 10"):
        print(f"  {r['started_at']} {r['source_key']:22} {r['status']:8} "
              f"seen={r['items_seen']} new={r['items_new']}")
    missing = conn.execute(
        """SELECT field, COUNT(*) c FROM missing_fields GROUP BY field ORDER BY c DESC"""
    ).fetchall()
    if missing:
        print("\nmissing data across listings:")
        for m in missing:
            print(f"  {m['field']:16} {m['c']}")
    return 0


def cmd_validate(args):
    conn = _conn(args)
    _db.init_db(conn)
    result = _validate.run(conn, listing_url=args.listing_url,
                           destination_state=args.destination,
                           allow_dirty=args.allow_dirty,
                           skip_network=args.offline)
    print(_validate.format_report(result, conn))
    return 0 if result["counts"]["fail"] == 0 else 1


def cmd_classic(args):
    if args.classic_cmd == "init-config":
        path = Path(args.path)
        if path.exists() and not args.force:
            print(f"{path} already exists. Use --force to overwrite.", file=sys.stderr)
            return 1
        _classic.write_template(path)
        print(f"Wrote template to {path}")
        print("Fill it in from https://support.classic.com/classic.com-api after "
              "agreeing terms with datasupport@classic.com, then set "
              '"confirmed": true.')
        print(f"Also export {_classic.API_KEY_ENV}=<your licensed key>.")
        return 0
    pf = _classic.preflight(Path(args.path))
    print(f"CLASSIC.COM adapter ready: {pf['ready']}")
    print(f"  credential present: {pf['credential']}")
    print(f"  endpoint config:    {pf['config']}")
    for b in pf["blockers"]:
        print(f"  BLOCKER: {b}")
    return 0


def cmd_sheet(args):
    conn = _conn(args)
    out = Path(args.out)
    if out.suffix.lower() == ".csv":
        _sheets.write_csv(conn, out)
    else:
        _sheets.write_html(conn, out)
    rows = _sheets.build_rows(conn)
    unver = sum(1 for r in rows if r["status"].startswith("UNVERIFIED"))
    print(f"Wrote {out} ({len(rows)} cost lines, {unver} currently unverified "
          f"placeholders).")
    print("Send this to your mechanic. Feed answers back with "
          "`assumptions --set <key> <value> --verified --basis \"...\"`.")
    return 0


def cmd_acceptance(args):
    conn = _conn(args)
    text, passed = _acceptance.report(conn, args.listing_id, args.destination)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
        print(f"\nWritten to {args.out}")
    return 0 if passed else 2


def cmd_marketcheck(args):
    if args.mc_cmd == "init-fieldmap":
        path = Path(args.path)
        if path.exists() and not args.force:
            print(f"{path} already exists. Use --force to overwrite.", file=sys.stderr)
            return 1
        _marketcheck.write_fieldmap_template(path)
        print(f"Wrote candidate field map to {path}")
        print("These field names are NOT confirmed. Run "
              "`porschehunter marketcheck probe` once you have a key.")
        return 0

    # probe -- one real call
    try:
        info = _marketcheck.probe(args.kind)
    except _marketcheck.NotConfigured as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    except _marketcheck.AuthError as exc:
        print(f"AUTH FAILED: {exc}", file=sys.stderr)
        return 1
    except _marketcheck.MarketCheckError as exc:
        print(f"CALL FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Endpoint:      {info['endpoint']}")
    print(f"Retrieved at:  {info['retrieved_at']}")
    print(f"Raw response:  {info['saved_to']}")
    print(f"Total reported: {info['total_reported']}")
    print(f"Items found in this page: {info['items_found']} "
          f"(via envelope.items='{info['items_path_used']}')")
    print(f"\nResponse top-level keys: {', '.join(info['response_top_level_keys'])}")
    print(f"\nFirst listing's actual keys:\n  {', '.join(info['first_item_keys'])}")
    print(f"\nMapped fields that RESOLVED ({len(info['resolved'])}):")
    for k, v in info["resolved"].items():
        print(f"  {k:16} = {str(v)[:60]}")
    if info["unresolved"]:
        print(f"\nMapped fields that did NOT resolve ({len(info['unresolved'])}):")
        for k in info["unresolved"]:
            print(f"  {k}")
        print("\nFix these paths in data/marketcheck_fields.json using the "
              "actual keys listed above.")
    if not info["fieldmap_confirmed"]:
        print('\nWhen the map is right, set "confirmed": true in '
              "data/marketcheck_fields.json. Ingestion stays blocked until then.")
    else:
        print("\nField map is marked confirmed. `fetch marketcheck` will run.")
    return 0


def cmd_alerts(args):
    conn = _conn(args)
    if args.alerts_cmd == "run":
        res = _searches.run_alerts(conn, destination_state=args.destination)
        print(f"Alerts recomputed for {res['searches_evaluated']} saved search(es); "
              f"{res['created']} new notification(s).")
        for name, n in res["per_search"].items():
            print(f"  {name}: {n}")
        print("\nNote: alerts only update when this runs. Nothing polls in the "
              "background.")
        return 0
    # list
    unread = _searches.unread_count(conn)
    items = _searches.list_notifications(conn, unread_only=args.unread, limit=args.limit)
    print(f"{unread} unread notification(s).")
    for n in items:
        flag = " " if n["read"] else "*"
        print(f" {flag} [{n['kind']}] {n['title']}  ({n['created_at']})")
        if n["body"]:
            print(f"      {n['body']}")
    return 0


def cmd_searches(args):
    conn = _conn(args)
    rows = _searches.list_searches(conn)
    if not rows:
        print("No saved searches. Create them in the dashboard (Save search).")
        return 0
    for s in rows:
        alerts = ", ".join(k for k, v in (s["notify"] or {}).items() if v) or "none"
        print(f"#{s['id']}  {s['name']}")
        print(f"      filters: {json.dumps(s['filters'])}")
        print(f"      sort: {s['sort'] or 'newest'}   alerts: {alerts}   "
              f"last run: {s['last_run_at'] or 'never'}")
    return 0


def cmd_recon(args):
    conn = _conn(args)
    if args.amount is not None:
        if not args.category:
            print("--category is required when --amount is given", file=sys.stderr)
            return 1
        try:
            _recon.set_cost(conn, args.listing_id, args.category, args.amount,
                            basis=args.basis, note=args.note)
        except _recon.ReconError as exc:
            print(f"rejected: {exc}", file=sys.stderr)
            return 1
    summ = _recon.summary(conn, args.listing_id)
    print(f"Reconditioning for listing #{args.listing_id}: total {_money(summ['total'])} "
          f"({summ['weakest_basis'] or 'no lines yet'})")
    for line in summ["lines"]:
        print(f"  {line['category']:18} {_money(line['amount'])}  [{line['basis']}]"
              + (f"  {line['note']}" if line["note"] else ""))
    if not summ["lines"]:
        print("  (none recorded -- enter them in the dashboard Recon form, or with "
              "`recon <id> --category <k> --amount <n> --basis quote`)")
    return 0


def cmd_serve(args):
    from . import dashboard
    dashboard.serve(args.db, host=args.host, port=args.port,
                    destination_state=args.destination,
                    include_synthetic=args.preview_synthetic)
    return 0


# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="porschehunter",
                                description="Porsche 911 acquisition screening tool")
    p.add_argument("--db", default=str(_db.DEFAULT_DB_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    sub.add_parser("sources", help="show data source access status").set_defaults(func=cmd_sources)
    sub.add_parser("status", help="refresh timestamps, coverage, missing data").set_defaults(func=cmd_status)

    e = sub.add_parser("enable", help="enable/disable a source")
    e.add_argument("source")
    e.add_argument("--off", dest="on", action="store_false", default=True)
    e.set_defaults(func=cmd_enable)

    a = sub.add_parser("add", help="add a listing by URL (manual entry)")
    a.add_argument("url")
    a.add_argument("--title"); a.add_argument("--year", type=int)
    a.add_argument("--generation"); a.add_argument("--variant")
    a.add_argument("--body"); a.add_argument("--transmission")
    a.add_argument("--mileage", type=int); a.add_argument("--vin")
    a.add_argument("--price", type=float)
    a.add_argument("--seller-type", choices=["private", "dealer", "auction_house", "unknown"])
    a.add_argument("--city"); a.add_argument("--state"); a.add_argument("--zip")
    a.add_argument("--listing-type", choices=["fixed", "auction", "unknown"])
    a.add_argument("--notes"); a.add_argument("--photo", action="append")
    a.add_argument("--no-vin-decode", action="store_true")
    a.set_defaults(func=cmd_add)

    f = sub.add_parser("fetch", help="run an automated connector")
    f.add_argument("source", choices=["dealer_jsonld", "craigslist_rss", "marketcheck", "manual"])
    f.add_argument("--url", action="append",
                   help="dealer_jsonld: one listing URL (repeatable)")
    f.add_argument("--domain",
                   help="dealer_jsonld: discover listings from this allowlisted "
                        "domain's sitemap (robots.txt enforced, rate limited)")
    f.add_argument("--max-pages", type=int, default=40,
                   help="dealer_jsonld --domain: cap on pages fetched")
    f.add_argument("--all-models", action="store_true",
                   help="dealer_jsonld: ingest every Porsche, not just 911s "
                        "(default: 911s only, since this is a 911 tool)")
    f.add_argument("--kind", default="active", choices=list(_marketcheck.KINDS),
                   help="marketcheck: active=dealer, fsbo=private party, "
                        "auction=auction inventory")
    f.add_argument("--year-min", type=int); f.add_argument("--year-max", type=int)
    f.add_argument("--max-rows", type=int, default=200)
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("comps", help="documented completed sales")
    csub = c.add_subparsers(dest="comps_cmd", required=True)
    ca = csub.add_parser("add")
    ca.add_argument("--generation", required=True)
    ca.add_argument("--price", required=True, type=float)
    ca.add_argument("--date", required=True, help="YYYY-MM-DD, the date the sale completed")
    ca.add_argument("--url", required=True, help="link to the completed sale")
    ca.add_argument("--venue", default="other", choices=sorted(_comps.VENUES))
    ca.add_argument("--variant"); ca.add_argument("--year", type=int)
    ca.add_argument("--body"); ca.add_argument("--transmission")
    ca.add_argument("--mileage", type=int); ca.add_argument("--vin")
    ca.add_argument("--note"); ca.add_argument("--by")
    ca.add_argument("--includes-fees", action="store_true",
                    help="set when the price already includes the buyer premium")
    ca.add_argument("--price-basis", default="unknown", choices=sorted(_comps.PRICE_BASES),
                    help="Is this a price someone actually paid? Only "
                         "'verified_transaction' is usable in valuations. "
                         "A removed listing's final ask is 'last_asking'.")
    ca.add_argument("--permission-basis", default="unknown",
                    choices=sorted(_comps.PERMISSION_BASES),
                    help="On what basis may you use this record? 'unknown' is "
                         "stored but excluded from valuations.")
    ca.add_argument("--permission-note",
                    help="required with --permission-basis operator_asserts_permission")
    ca.set_defaults(func=cmd_comps_add)
    ci = csub.add_parser("import"); ci.add_argument("path"); ci.set_defaults(func=cmd_comps_import)
    cc = csub.add_parser("coverage"); cc.set_defaults(func=cmd_comps_coverage)

    v = sub.add_parser("value", help="value one listing from comps")
    v.add_argument("listing_id", type=int)
    v.set_defaults(func=cmd_value)

    s = sub.add_parser("scan", help="score every active listing and flag opportunities")
    s.add_argument("--destination", required=True, help=DEST_STATE_HELP)
    s.set_defaults(func=cmd_scan)

    l = sub.add_parser("listings", help="show real listings and their valuation status")
    l.add_argument("--limit", type=int, default=50)
    l.add_argument("--generation", help="filter, e.g. 997 or 991.2")
    l.set_defaults(func=cmd_listings)

    ov = sub.add_parser("override",
                        help="record real per-car quotes that replace the placeholders")
    ov.add_argument("listing_id", type=int)
    ov.add_argument("--repairs", type=float, help="actual repair quote for this car (USD)")
    ov.add_argument("--transport", type=float, help="actual broker transport quote (USD)")
    ov.add_argument("--days", type=float, help="expected days to sell this car")
    ov.add_argument("--note")
    ov.set_defaults(func=cmd_override)

    vn = sub.add_parser("vin", help="decode a VIN via NHTSA vPIC")
    vn.add_argument("vin"); vn.add_argument("--force", action="store_true")
    vn.set_defaults(func=cmd_vin)

    asm = sub.add_parser("assumptions", help="list or change cost assumptions")
    asm.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"))
    asm.add_argument("--basis"); asm.add_argument("--verified", action="store_true")
    asm.set_defaults(func=cmd_assumptions)

    va = sub.add_parser("validate",
                        help="run the live real-data validation workflow")
    va.add_argument("--listing-url", help="one real listing URL from an authorized, "
                                          "allowlisted source to ingest end to end")
    va.add_argument("--destination", default="OH", help=DEST_STATE_HELP)
    va.add_argument("--allow-dirty", action="store_true",
                    help="continue even if the database contains synthetic rows "
                         "(results are then NOT production output)")
    va.add_argument("--offline", action="store_true",
                    help="skip all network steps and report them as skipped")
    va.set_defaults(func=cmd_validate)

    cm = sub.add_parser("classic-com", help="CLASSIC.COM licensed API adapter")
    cmsub = cm.add_subparsers(dest="classic_cmd", required=True)
    cmi = cmsub.add_parser("init-config")
    cmi.add_argument("--path", default=str(_classic.CONFIG_PATH))
    cmi.add_argument("--force", action="store_true")
    cmi.set_defaults(func=cmd_classic)
    cms = cmsub.add_parser("status")
    cms.add_argument("--path", default=str(_classic.CONFIG_PATH))
    cms.set_defaults(func=cmd_classic)

    mk = sub.add_parser("marketcheck", help="MarketCheck API setup and schema probe")
    mksub = mk.add_subparsers(dest="mc_cmd", required=True)
    mkp = mksub.add_parser("probe",
                           help="make ONE real call and report the actual response shape")
    mkp.add_argument("--kind", default="active", choices=list(_marketcheck.KINDS))
    mkp.set_defaults(func=cmd_marketcheck)
    mkf = mksub.add_parser("init-fieldmap")
    mkf.add_argument("--path", default=str(_marketcheck.FIELDMAP_PATH))
    mkf.add_argument("--force", action="store_true")
    mkf.set_defaults(func=cmd_marketcheck)

    sh = sub.add_parser("sheet", help="export the mechanic cost input sheet")
    sh.add_argument("--out", default="data/cost_input_sheet.html",
                    help=".html for a printable one-pager, .csv to fill in digitally")
    sh.set_defaults(func=cmd_sheet)

    ac = sub.add_parser("acceptance",
                        help="full underwriting report for one real listing, "
                             "or INSUFFICIENT DATA with the exact gap")
    ac.add_argument("listing_id", type=int)
    ac.add_argument("--destination", required=True, help=DEST_STATE_HELP)
    ac.add_argument("--out", help="also write the report to this file")
    ac.set_defaults(func=cmd_acceptance)

    al = sub.add_parser("alerts", help="recompute or list saved-search notifications")
    alsub = al.add_subparsers(dest="alerts_cmd", required=True)
    alr = alsub.add_parser("run", help="recompute alerts for all saved searches (manual; no polling)")
    alr.add_argument("--destination", default="OH", help=DEST_STATE_HELP)
    alr.set_defaults(func=cmd_alerts)
    all_ = alsub.add_parser("list", help="show the notification feed")
    all_.add_argument("--unread", action="store_true")
    all_.add_argument("--limit", type=int, default=50)
    all_.set_defaults(func=cmd_alerts)

    ss = sub.add_parser("searches", help="list saved searches")
    ss.set_defaults(func=cmd_searches)

    rc = sub.add_parser("recon", help="record/show reconditioning cost estimates for a car")
    rc.add_argument("listing_id", type=int)
    rc.add_argument("--category", choices=sorted(_recon.CATEGORY_KEYS))
    rc.add_argument("--amount", type=float)
    rc.add_argument("--basis", default="estimate", choices=list(_recon.BASES))
    rc.add_argument("--note")
    rc.set_defaults(func=cmd_recon)

    sv = sub.add_parser("serve", help="run the dashboard")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--destination", required=True, help=DEST_STATE_HELP)
    sv.add_argument("--preview-synthetic", action="store_true",
                    help="LAYOUT PREVIEW ONLY: feed synthetic test comps into the "
                         "valuations so the page can be inspected. Shows a red banner. "
                         "Never use this to make a buying decision.")
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)

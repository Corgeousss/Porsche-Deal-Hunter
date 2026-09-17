"""Live real-data validation workflow.

This module exists to answer one question honestly: **does this actually work
against real endpoints, right now, on this machine?**

Rules it enforces:
  * Every step that touches the network records the exact URL called, the HTTP
    status and the retrieval timestamp, into `validation_steps`.
  * A source is marked `live_verified_at` ONLY by a successful live call here.
    Unit tests cannot set it. Nothing else sets it.
  * Synthetic fixtures are refused. If the target database contains any
    synthetic row, validation aborts unless you explicitly pass allow_dirty,
    and the report states the count either way.
  * A step that cannot run is `skip`, never `pass`. Nothing is assumed working.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import comps as _comps
from . import db as _db
from . import deal as _deal
from . import http_util
from . import valuation as _valuation
from . import vin as _vin
from .sources import classic_com as _classic
from .sources import jsonld as _jsonld
from .sources import marketcheck as _marketcheck

# A VIN used only to exercise the decoder end to end. It is a real-world
# structure, and vPIC's answer -- whatever it is -- is recorded verbatim.
PROBE_VIN = "WP0AB2A99BS721234"


class Recorder:
    def __init__(self, conn: sqlite3.Connection, profile: str):
        self.conn = conn
        self.run_id = int(conn.execute(
            "INSERT INTO validation_runs (started_at, profile) VALUES (?,?)",
            (_db.utcnow(), profile)).lastrowid)
        conn.commit()
        self.results: list[dict] = []

    def step(self, name: str, status: str, detail: str = "",
             endpoint: str | None = None, http_status: int | None = None,
             retrieved_at: str | None = None) -> dict:
        row = {"step": name, "status": status, "detail": detail,
               "endpoint": endpoint, "http_status": http_status,
               "retrieved_at": retrieved_at}
        self.conn.execute(
            """INSERT INTO validation_steps (run_id, step, status, endpoint,
                   retrieved_at, http_status, detail, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (self.run_id, name, status, endpoint, retrieved_at, http_status,
             detail, _db.utcnow()))
        self.conn.commit()
        self.results.append(row)
        return row

    def finish(self) -> dict:
        counts = {s: sum(1 for r in self.results if r["status"] == s)
                  for s in ("pass", "fail", "skip")}
        summary = json.dumps(counts)
        self.conn.execute(
            """UPDATE validation_runs SET finished_at=?, passed=?, failed=?,
                   skipped=?, summary=? WHERE id=?""",
            (_db.utcnow(), counts["pass"], counts["fail"], counts["skip"],
             summary, self.run_id))
        self.conn.commit()
        return {"run_id": self.run_id, "counts": counts, "steps": self.results}


def mark_live_verified(conn, source_key: str, note: str) -> None:
    """The ONLY way a source becomes live-verified."""
    conn.execute(
        "UPDATE sources SET live_verified_at=?, live_verified_note=? WHERE key=?",
        (_db.utcnow(), note, source_key))
    conn.commit()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_synthetic_clean(conn, rec: Recorder, allow_dirty: bool) -> bool:
    n = conn.execute("SELECT COUNT(*) c FROM comps WHERE is_synthetic=1").fetchone()["c"]
    if n == 0:
        rec.step("synthetic_data_absent", "pass",
                 "0 synthetic rows. Results are real-data only.")
        return True
    if allow_dirty:
        rec.step("synthetic_data_absent", "fail",
                 f"{n} synthetic rows present. Continuing because allow_dirty was "
                 f"set, but these results MUST NOT be treated as production output.")
        return True
    rec.step("synthetic_data_absent", "fail",
             f"{n} synthetic rows present in this database. Validation aborted. "
             f"Use a clean database (PORSCHE_DB=data/real.db) or pass "
             f"--allow-dirty to override and accept non-production results.")
    return False


def check_network(rec: Recorder, url: str = "https://vpic.nhtsa.dot.gov/api/") -> bool:
    """Is outbound HTTPS possible at all from this machine?"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": http_util.USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.status
            resp.read(2048)
        rec.step("network_egress", "pass",
                 f"reached {url} in {time.time()-t0:.1f}s",
                 endpoint=url, http_status=code, retrieved_at=_db.utcnow())
        return True
    except Exception as exc:
        rec.step("network_egress", "fail",
                 f"{type(exc).__name__}: {exc}. No live endpoint check can pass "
                 f"from this machine until outbound HTTPS to this host works.",
                 endpoint=url, retrieved_at=_db.utcnow())
        return False


def check_vpic_live(conn, rec: Recorder, vin: str = PROBE_VIN) -> bool:
    """A real call to the free NHTSA vPIC decoder. No key required."""
    url = _vin.VPIC_URL.format(vin=vin)
    try:
        result = _vin.decode_via_vpic(vin)
        retrieved = _db.utcnow()
    except Exception as exc:
        rec.step("vpic_live_call", "fail", f"{type(exc).__name__}: {exc}",
                 endpoint=url, retrieved_at=_db.utcnow())
        return False
    make = result.get("Make") or "(empty)"
    rec.step("vpic_live_call", "pass",
             f"decoded {vin}: Make={make}, Model={result.get('Model') or '(empty)'}, "
             f"ModelYear={result.get('ModelYear') or '(empty)'}",
             endpoint=url, http_status=200, retrieved_at=retrieved)
    mark_live_verified(conn, "nhtsa_vpic",
                       f"decodevinvalues returned Make={make} at {retrieved}")
    return True


def check_robots(rec: Recorder, listing_url: str | None) -> bool:
    if not listing_url:
        rec.step("source_permission_robots", "skip",
                 "No --listing-url supplied, so no robots.txt check was made.")
        return False
    try:
        allowed = http_util.robots_allows(listing_url)
    except Exception as exc:
        rec.step("source_permission_robots", "fail",
                 f"could not evaluate robots.txt: {type(exc).__name__}: {exc}",
                 endpoint=listing_url)
        return False
    if not allowed:
        rec.step("source_permission_robots", "fail",
                 f"robots.txt does not permit {http_util.USER_AGENT} to fetch this "
                 f"URL. This is a hard stop -- use manual entry for this listing.",
                 endpoint=listing_url, retrieved_at=_db.utcnow())
        return False
    rec.step("source_permission_robots", "pass",
             "robots.txt permits this User-Agent to fetch this URL.",
             endpoint=listing_url, retrieved_at=_db.utcnow())
    return True


def check_allowlist(rec: Recorder, listing_url: str | None) -> bool:
    if not listing_url:
        rec.step("source_domain_allowlist", "skip", "No --listing-url supplied.")
        return False
    if not _jsonld.domain_allowed(listing_url):
        rec.step("source_domain_allowlist", "fail",
                 f"domain is not in {_jsonld.ALLOWLIST_PATH}. Add it only after "
                 f"reading that site's terms.", endpoint=listing_url)
        return False
    rec.step("source_domain_allowlist", "pass", "domain is on the operator allowlist.",
             endpoint=listing_url)
    return True


def check_ingest(conn, rec: Recorder, listing_url: str | None) -> int | None:
    """Ingest one real listing from an authorized source."""
    if not listing_url:
        rec.step("ingest_real_listing", "skip", "No --listing-url supplied.")
        return None
    try:
        listing_id, created, report = _jsonld.ingest_url(conn, listing_url)
        retrieved = _db.utcnow()
    except Exception as exc:
        rec.step("ingest_real_listing", "fail", f"{type(exc).__name__}: {exc}",
                 endpoint=listing_url, retrieved_at=_db.utcnow())
        return None
    if listing_id is None:
        rec.step("ingest_real_listing", "fail",
                 f"parsed, but not a Porsche: {report.get('notices')}",
                 endpoint=listing_url, retrieved_at=retrieved)
        return None
    row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    rec.step("ingest_real_listing", "pass",
             f"listing #{listing_id} {'created' if created else 'updated'}: "
             f"{row['year']} {row['generation']} {row['variant']} "
             f"price={row['price']} mileage={row['mileage']} vin={row['vin']} "
             f"| source_url={row['url']} | fetched_at={row['fetched_at']}",
             endpoint=listing_url, http_status=200, retrieved_at=retrieved)
    mark_live_verified(conn, "dealer_jsonld",
                       f"parsed schema.org Vehicle from {listing_url} at {retrieved}")
    return listing_id


def check_vin_normalization(conn, rec: Recorder, listing_id: int | None) -> bool:
    if listing_id is None:
        rec.step("vin_normalization", "skip", "No ingested listing to check.")
        return False
    row = conn.execute("SELECT vin FROM listings WHERE id=?", (listing_id,)).fetchone()
    raw = row["vin"]
    if not raw:
        rec.step("vin_normalization", "fail",
                 "The ingested listing carried no VIN, so normalization could not "
                 "be exercised on real data. Record the VIN before underwriting.")
        return False
    summary = _vin.offline_summary(raw)
    if not summary["valid_format"]:
        rec.step("vin_normalization", "fail", f"VIN {raw} failed format validation.")
        return False
    detail = (f"{raw}: check_digit_ok={summary['check_digit_ok']}, "
              f"porsche_wmi={summary['is_porsche_wmi']}, "
              f"model_year={summary['model_year']}, generation={summary['generation']}")
    if summary["warnings"]:
        detail += " | warnings: " + "; ".join(summary["warnings"])
    rec.step("vin_normalization", "pass" if summary["check_digit_ok"] else "fail", detail)
    return summary["check_digit_ok"]


def check_duplicate_handling(conn, rec: Recorder, listing_url: str | None,
                             listing_id: int | None) -> bool:
    """Re-ingest the same URL and prove it updates rather than duplicates."""
    if not listing_url or listing_id is None:
        rec.step("duplicate_handling", "skip", "Nothing ingested to re-ingest.")
        return False
    before = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    hist_before = conn.execute(
        "SELECT COUNT(*) c FROM price_history WHERE listing_id=?", (listing_id,)
    ).fetchone()["c"]
    try:
        second_id, created, _ = _jsonld.ingest_url(conn, listing_url)
    except Exception as exc:
        rec.step("duplicate_handling", "fail", f"{type(exc).__name__}: {exc}",
                 endpoint=listing_url, retrieved_at=_db.utcnow())
        return False
    after = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    hist_after = conn.execute(
        "SELECT COUNT(*) c FROM price_history WHERE listing_id=?", (listing_id,)
    ).fetchone()["c"]
    ok = (after == before) and (second_id == listing_id) and not created
    rec.step("duplicate_handling", "pass" if ok else "fail",
             f"re-ingested the same URL: listing count {before}->{after}, "
             f"id {listing_id}->{second_id}, created={created}, "
             f"price_history rows {hist_before}->{hist_after} "
             f"(unchanged price should not add a row)",
             endpoint=listing_url, retrieved_at=_db.utcnow())
    return ok


def check_valuation(conn, rec: Recorder, listing_id: int | None) -> dict | None:
    if listing_id is None:
        rec.step("valuation", "skip", "No ingested listing to value.")
        return None
    row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    val = _valuation.value_listing(conn, row, store=True)
    if val["status"] != "ok":
        excl = val["detail"].get("selection", {}).get("excluded", {}) or {}
        rec.step("valuation", "fail",
                 f"INSUFFICIENT DATA -- {val['detail'].get('explanation')} "
                 f"(usable comps: {val['n_comps']}, excluded on provenance: "
                 f"{excl.get('total', 0)})")
        return val
    rec.step("valuation", "pass",
             f"${val['point_value']:,.0f} from {val['n_comps']} verified comps "
             f"({val['confidence']} confidence, range ${val['low_value']:,.0f}-"
             f"${val['high_value']:,.0f})")
    return val


def check_underwriting(conn, rec: Recorder, listing_id: int | None,
                       destination_state: str, valuation: dict | None) -> dict | None:
    if listing_id is None:
        rec.step("underwriting", "skip", "No ingested listing to underwrite.")
        return None
    row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    res = _deal.evaluate(conn, row, destination_state=destination_state,
                         valuation=valuation)
    if res["status"] != "ok":
        rec.step("underwriting", "fail",
                 f"INSUFFICIENT DATA -- cannot underwrite: {res['detail'].get('explanation')}")
        return res
    rec.step("underwriting", "pass",
             f"ask ${res['asking_price'] or 0:,.0f} | resale ${res['expected_resale']:,.0f} "
             f"| total cost ${res['total_cost'] or 0:,.0f} | net ${res['net_profit'] or 0:,.0f} "
             f"| max bid ${res['max_purchase_price']:,.0f} "
             f"| PROVISIONAL: {len(res['detail']['unverified_lines'])} unverified cost inputs")
    return res


def check_dashboard(conn, rec: Recorder, destination_state: str) -> bool:
    from . import dashboard
    try:
        html = dashboard.render(conn, destination_state)
    except Exception as exc:
        rec.step("dashboard_render", "fail", f"{type(exc).__name__}: {exc}")
        return False
    ok = "<html" in html.lower() or "<!doctype" in html.lower()
    rec.step("dashboard_render", "pass" if ok else "fail",
             f"rendered {len(html):,} bytes of HTML from real data only")
    return ok


def check_provider_readiness(rec: Recorder) -> None:
    """Report -- without calling anything -- whether the licensed providers are
    configured. These are blockers, not failures of the code."""
    pf = _classic.preflight()
    rec.step("classic_com_readiness", "pass" if pf["ready"] else "skip",
             ("credential and endpoint config present" if pf["ready"]
              else " | ".join(b.split(".")[0] for b in pf["blockers"])))
    try:
        _marketcheck.api_key()
        rec.step("marketcheck_readiness", "pass", "MARKETCHECK_API_KEY is set")
    except _marketcheck.NotConfigured:
        rec.step("marketcheck_readiness", "skip", "MARKETCHECK_API_KEY is not set")


# ---------------------------------------------------------------------------
def run(conn, *, listing_url: str | None = None, destination_state: str = "OH",
        allow_dirty: bool = False, skip_network: bool = False) -> dict:
    rec = Recorder(conn, profile="real_data_validation")

    if not check_synthetic_clean(conn, rec, allow_dirty):
        return rec.finish()

    check_provider_readiness(rec)

    online = False if skip_network else check_network(rec)

    if online:
        check_vpic_live(conn, rec)
    else:
        rec.step("vpic_live_call", "skip",
                 "Outbound HTTPS is unavailable, so no live endpoint call was "
                 "attempted. NO CONNECTOR CAN BE CALLED LIVE-VERIFIED FROM HERE.")

    listing_id = None
    if online and listing_url:
        if check_allowlist(rec, listing_url) and check_robots(rec, listing_url):
            listing_id = check_ingest(conn, rec, listing_url)
            check_duplicate_handling(conn, rec, listing_url, listing_id)
        else:
            rec.step("ingest_real_listing", "skip",
                     "Permission checks did not pass; nothing was fetched.")
    else:
        for s in ("source_domain_allowlist", "source_permission_robots",
                  "ingest_real_listing", "duplicate_handling"):
            rec.step(s, "skip",
                     "offline" if not online else "no --listing-url supplied")

    check_vin_normalization(conn, rec, listing_id)
    val = check_valuation(conn, rec, listing_id)
    check_underwriting(conn, rec, listing_id, destination_state, val)
    check_dashboard(conn, rec, destination_state)

    return rec.finish()


def format_report(result: dict, conn=None) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    out = [f"Validation run #{result['run_id']}",
           "=" * 72]
    for r in result["steps"]:
        out.append(f"[{icon[r['status']]}] {r['step']}")
        if r["endpoint"]:
            out.append(f"       endpoint:     {r['endpoint']}")
        if r["http_status"]:
            out.append(f"       http status:  {r['http_status']}")
        if r["retrieved_at"]:
            out.append(f"       retrieved at: {r['retrieved_at']}")
        if r["detail"]:
            for i, line in enumerate(_wrap(r["detail"], 66)):
                out.append(f"       {'detail:       ' if i == 0 else '              '}{line}")
    c = result["counts"]
    out.append("=" * 72)
    out.append(f"{c['pass']} passed, {c['fail']} failed, {c['skip']} skipped")
    if conn is not None:
        verified = conn.execute(
            "SELECT key, live_verified_at FROM sources WHERE live_verified_at IS NOT NULL"
        ).fetchall()
        out.append("")
        if verified:
            out.append("Live-verified sources (proven by a real endpoint call):")
            for v in verified:
                out.append(f"  {v['key']}  at {v['live_verified_at']}")
        else:
            out.append("Live-verified sources: NONE. No connector in this repository "
                       "has yet completed a real call to a real endpoint.")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]

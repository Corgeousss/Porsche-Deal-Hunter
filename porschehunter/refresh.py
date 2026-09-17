"""One refresh cycle: re-read every approved dealer, update price histories,
discover new cars, then recompute deduplicated alerts.

Designed to be driven by Windows Task Scheduler (see scheduler/). Honesty notes:

* This does the work only when it is actually invoked. It is not a daemon; if the
  machine is off or the scheduled task is not running, nothing is monitored, and
  the app never claims otherwise.
* Progress is persisted per domain in ``refresh_state`` and domains are processed
  oldest-success-first, so an interrupted run resumes sensibly on the next tick.
* Every request still goes through the JSON-LD connector, which enforces
  robots.txt (fetched with our real UA), the per-host rate limit and the
  911-only model guard.
"""

from __future__ import annotations

from . import db as _db
from . import searches as _searches
from .sources import jsonld as _jsonld


def _active_count(conn, domain: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM listings WHERE status='active' AND url LIKE ?",
        (f"%{domain}%",)).fetchone()["c"]


def refresh_all(conn, destination_state: str = "OH", max_pages: int = 60,
                trigger: str = "manual", only_domains: list[str] | None = None) -> dict:
    """Refresh every allowlisted dealer domain, then recompute alerts.

    Returns a summary dict. Safe to call repeatedly; deduplication in the
    connector (URL upsert, price-history diff) and in alerts prevents churn.
    """
    _db.init_db(conn)
    now = _db.utcnow()
    run = conn.execute(
        "INSERT INTO refresh_runs (started_at, trigger, status) VALUES (?,?, 'running')",
        (now, trigger))
    run_id = int(run.lastrowid)
    conn.commit()

    domains = only_domains or sorted(_jsonld.load_allowlist())
    # Oldest success first so an interrupted cycle catches up the stalest sources.
    def staleness(d):
        row = conn.execute("SELECT last_success FROM refresh_state WHERE domain=?",
                           (d,)).fetchone()
        return (row["last_success"] or "") if row else ""
    domains.sort(key=staleness)

    per_domain, new_total, done = [], 0, 0
    for domain in domains:
        started = _db.utcnow()
        conn.execute(
            """INSERT INTO refresh_state (domain, last_attempt) VALUES (?,?)
               ON CONFLICT(domain) DO UPDATE SET last_attempt=excluded.last_attempt""",
            (domain, started))
        conn.commit()
        try:
            res = _jsonld.ingest_domain(conn, domain, max_pages=max_pages, only_911=True)
            status = res.status
            err = res.message if status == "error" else None
            new_total += res.new
            done += 1
            conn.execute(
                """UPDATE refresh_state SET last_success=?, last_status=?, last_error=?,
                       new_count=?, updated_count=?, rejected_count=?,
                       quarantined_count=?, active_count=? WHERE domain=?""",
                (_db.utcnow() if status != "error" else None, status, err,
                 res.new, res.updated, res.rejected, res.quarantined,
                 _active_count(conn, domain), domain))
            per_domain.append({"domain": domain, "status": status, "new": res.new,
                               "updated": res.updated, "rejected": res.rejected,
                               "quarantined": res.quarantined})
        except Exception as exc:  # never let one dealer kill the whole cycle
            conn.execute(
                "UPDATE refresh_state SET last_status='error', last_error=? WHERE domain=?",
                (f"{type(exc).__name__}: {exc}", domain))
            per_domain.append({"domain": domain, "status": "error",
                               "error": f"{type(exc).__name__}: {exc}"})
        conn.commit()

    alerts = _searches.run_alerts(conn, destination_state)
    conn.execute(
        """UPDATE refresh_runs SET finished_at=?, domains_done=?, new_total=?,
               alerts_created=?, status='ok' WHERE id=?""",
        (_db.utcnow(), done, new_total, alerts["created"], run_id))
    conn.commit()
    return {"run_id": run_id, "domains": per_domain, "new_total": new_total,
            "alerts_created": alerts["created"], "domains_done": done}


def status(conn) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM refresh_state ORDER BY last_success IS NULL DESC, last_success")]
    last_run = conn.execute(
        "SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"per_domain": rows, "last_run": dict(last_run) if last_run else None}

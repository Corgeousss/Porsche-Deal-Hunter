"""Saved searches and the in-app notification feed (Task 3).

Honesty constraints baked in here:

* Alerts are computed only when `run_alerts()` is called -- i.e. on a manual
  refresh. Nothing here polls in the background, and the UI must not claim it
  does.
* Every alert carries a `dedup_key`; re-running over unchanged data inserts
  nothing, so the operator is never notified twice for the same fact.
* A "meets the $8,000 profit target" alert fires only for a car that is
  genuinely, fully underwritten -- never for an unvalued or merely preliminary
  lead.
"""

from __future__ import annotations

import json

from . import db as _db
from . import filters as _filters


# ---------------------------------------------------------------------------
# Saved-search CRUD
# ---------------------------------------------------------------------------
def save_search(conn, name: str, filters: dict, sort: str | None = None,
                notify: dict | None = None) -> int:
    now = _db.utcnow()
    cur = conn.execute(
        """INSERT INTO saved_searches (name, filters, sort, notify, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
               filters=excluded.filters, sort=excluded.sort,
               notify=excluded.notify, updated_at=excluded.updated_at""",
        (name, json.dumps(filters or {}), sort, json.dumps(notify or {}), now, now))
    conn.commit()
    row = conn.execute("SELECT id FROM saved_searches WHERE name=?", (name,)).fetchone()
    return int(row["id"])


def list_searches(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM saved_searches ORDER BY name"):
        out.append({
            "id": r["id"], "name": r["name"],
            "filters": json.loads(r["filters"] or "{}"),
            "sort": r["sort"],
            "notify": json.loads(r["notify"] or "{}"),
            "last_run_at": r["last_run_at"],
        })
    return out


def get_search(conn, search_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM saved_searches WHERE id=?", (search_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"],
            "filters": json.loads(r["filters"] or "{}"), "sort": r["sort"],
            "notify": json.loads(r["notify"] or "{}"), "last_run_at": r["last_run_at"]}


def delete_search(conn, search_id: int) -> None:
    conn.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Notification feed
# ---------------------------------------------------------------------------
def _add_notification(conn, *, kind, title, body, dedup_key,
                      search_id=None, search_name=None, listing_id=None) -> bool:
    """Insert one notification unless its dedup_key already exists.

    Returns True if a new row was actually created."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO notifications
               (created_at, search_id, search_name, listing_id, kind, title, body,
                dedup_key, read)
           VALUES (?,?,?,?,?,?,?,?,0)""",
        (_db.utcnow(), search_id, search_name, listing_id, kind, title, body, dedup_key))
    return cur.rowcount > 0


def list_notifications(conn, unread_only: bool = False, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read=0"
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (limit,))]


def unread_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM notifications WHERE read=0").fetchone()["c"]


def mark_read(conn, notification_id: int | None = None) -> None:
    if notification_id is None:
        conn.execute("UPDATE notifications SET read=1 WHERE read=0")
    else:
        conn.execute("UPDATE notifications SET read=1 WHERE id=?", (notification_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Alert computation (manual refresh only)
# ---------------------------------------------------------------------------
def run_alerts(conn, destination_state: str = "OH") -> dict:
    """Recompute alerts for every saved search. Deduplicated. Returns a summary.

    This is the ONLY place notifications are generated, and it runs only when
    called (a manual refresh / the `alerts run` command). It does not poll.
    """
    created = 0
    per_search = {}
    searches = list_searches(conn)
    # Enrich once, reuse across searches.
    enriched = _filters.enrich(conn, destination_state)
    now = _db.utcnow()

    for s in searches:
        notify = s["notify"] or {}
        if not any(notify.values()):
            continue
        matched = _filters.apply_filters(enriched, s["filters"])
        last_run = s["last_run_at"]
        made = 0

        for v in matched:
            lid = v["id"]
            label = v.get("title") or f"{v.get('year') or ''} 911 {v.get('variant') or ''}".strip()
            price = v.get("price")

            if notify.get("new_match"):
                # "Newly discovered" = first seen after the search last ran.
                if last_run is None or (v.get("first_seen_at") or "") > last_run:
                    if _add_notification(
                            conn, kind="new_match",
                            title=f"New match: {label}",
                            body=f"${price:,.0f}" if price else "price unknown",
                            dedup_key=f"new:{s['id']}:{lid}",
                            search_id=s["id"], search_name=s["name"], listing_id=lid):
                        made += 1

            thresh = notify.get("below_price") or s["filters"].get("price_max")
            if notify.get("below_threshold") and thresh and price is not None \
                    and price <= float(thresh):
                # dedup on price so a further drop re-notifies, an unchanged price does not
                if _add_notification(
                        conn, kind="below_threshold",
                        title=f"Under ${float(thresh):,.0f}: {label}",
                        body=f"now ${price:,.0f}",
                        dedup_key=f"below:{s['id']}:{lid}:{int(price)}",
                        search_id=s["id"], search_name=s["name"], listing_id=lid):
                    made += 1

            if notify.get("price_drop") and v.get("price_reduced"):
                drop = v.get("price_reduction_total") or 0
                min_drop = float(notify.get("drop_min") or 0)
                if drop >= min_drop and drop > 0:
                    if _add_notification(
                            conn, kind="price_drop",
                            title=f"Price drop ${drop:,.0f}: {label}",
                            body=f"now ${price:,.0f}" if price else "",
                            dedup_key=f"drop:{lid}:{int(price or 0)}",
                            search_id=s["id"], search_name=s["name"], listing_id=lid):
                        made += 1

            if notify.get("auction_soon"):
                hrs = v.get("auction_hours_left")
                window = float(notify.get("auction_hours") or 48)
                if hrs is not None and 0 <= hrs <= window:
                    if _add_notification(
                            conn, kind="auction_soon",
                            title=f"Auction ending soon: {label}",
                            body=f"~{hrs:.0f}h left",
                            dedup_key=f"auction:{lid}",
                            search_id=s["id"], search_name=s["name"], listing_id=lid):
                        made += 1

            # Only a genuinely, fully underwritten car can trigger this.
            if notify.get("meets_profit") and v.get("meets_threshold") \
                    and v.get("underwriting_status") == "underwritten":
                if _add_notification(
                        conn, kind="meets_profit",
                        title=f"Meets ${_db.get_assumption(conn,'target_net_profit'):,.0f} target: {label}",
                        body=f"net ${v.get('net_profit'):,.0f}" if v.get("net_profit") else "",
                        dedup_key=f"profit:{lid}",
                        search_id=s["id"], search_name=s["name"], listing_id=lid):
                    made += 1

        conn.execute("UPDATE saved_searches SET last_run_at=? WHERE id=?", (now, s["id"]))
        per_search[s["name"]] = made
        created += made

    conn.commit()
    return {"created": created, "per_search": per_search,
            "searches_evaluated": len(searches)}

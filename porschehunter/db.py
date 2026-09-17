"""SQLite access layer. Standard library only."""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path

from . import assumptions as _assumptions

DEFAULT_DB_PATH = Path(os.environ.get("PORSCHE_DB", "data/porsche.db"))
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Source registry -- the factual record of what this platform may talk to.
# `authorized` values:
#   yes                    -- public, documented, no credential needed
#   yes_with_credentials   -- commercial API, needs a paid key
#   operator_enabled       -- publicly published data, but the operator must
#                             opt the specific domain in and accept its terms
#   manual_only            -- no authorized programmatic access; paste URLs
#   prohibited             -- terms explicitly forbid automated extraction
# ---------------------------------------------------------------------------
SOURCE_REGISTRY = [
    dict(
        key="manual",
        name="Manual URL / operator entry",
        access_method="manual",
        authorized="yes",
        requires_credentials=0,
        cost_notes="Free.",
        terms_url=None,
        limitations=(
            "Operator pastes a listing URL and types the details they can see. "
            "This is the only path used for Facebook Marketplace, Cars & Bids, "
            "Bring a Trailer and any site that has not been opted in."
        ),
        enabled=1,
    ),
    dict(
        key="nhtsa_vpic",
        name="NHTSA vPIC VIN decoder",
        access_method="api",
        authorized="yes",
        requires_credentials=0,
        cost_notes="Free. No registration, no API key.",
        terms_url="https://vpic.nhtsa.dot.gov/api/",
        limitations=(
            "US government VIN decoder. Returns make/model/year/engine/plant. "
            "Does NOT return options, condition, mileage or price. "
            "Porsche trim resolution is coarse."
        ),
        enabled=1,
    ),
    dict(
        key="marketcheck",
        name="MarketCheck Automotive API",
        access_method="api",
        authorized="yes_with_credentials",
        requires_credentials=1,
        cost_notes=(
            "Commercial, paid. PRICING NOT VERIFIED IN THIS REPO -- request a "
            "quote naming the exact endpoints you need. Assume nothing."
        ),
        terms_url="https://docs.marketcheck.com/docs/api/cars",
        limitations=(
            "Documents dealer active, Private Party and Auction inventory "
            "search, Past Inventory (sold/expired/removed), History by VIN, "
            "VIN decode and price prediction. CRITICAL: Past Inventory is "
            "DEALER ONLY (US/CA) and represents REMOVALS, not confirmed "
            "transactions -- imported as price_basis='inferred_from_removal' "
            "and excluded from valuations. Ask MarketCheck directly which "
            "endpoint, if any, returns a price a buyer actually paid."
        ),
        enabled=0,
    ),
    dict(
        key="classic_com",
        name="CLASSIC.COM (licensed third-party API)",
        access_method="api",
        authorized="yes_with_credentials",
        requires_credentials=1,
        cost_notes=(
            "Licensed, negotiated directly with datasupport@classic.com. Not "
            "self-serve. PRICING NOT KNOWN -- ask them; assume nothing."
        ),
        terms_url="https://support.classic.com/classic.com-api",
        limitations=(
            "Official API for licensed third parties covering taxonomy, sales "
            "history and comparable sales. This is the correct primary route "
            "for verified completed-sale comps. NOTE: their historical database "
            "preserves removed listings at their final 'Last Asking' price, "
            "which is NOT a transaction -- the adapter fails closed and stores "
            "anything it cannot prove sold as price_basis='last_asking', "
            "excluded from valuations. Adapter written; authentication "
            "DISABLED until licensed credentials and endpoint config exist."
        ),
        enabled=0,
    ),
    dict(
        key="bring_a_trailer",
        name="Bring a Trailer",
        access_method="manual",
        authorized="manual_only",
        requires_credentials=0,
        cost_notes="Free to read. No public API and no data-licensing product.",
        terms_url="https://bringatrailer.com/terms-of-service/",
        limitations=(
            "No authorized programmatic access and no data-licensing product. "
            "Use as a HUMAN REFERENCE only. Systematically aggregating their "
            "auction results into this database needs permission from BaT -- "
            "manual entry does not make it acceptable. If you obtain "
            "permission, record it via --permission-basis "
            "operator_asserts_permission --permission-note. Otherwise license "
            "the same transactions through CLASSIC.COM."
        ),
        enabled=1,
    ),
    dict(
        key="cars_and_bids",
        name="Cars & Bids",
        access_method="manual",
        authorized="prohibited",
        requires_credentials=0,
        cost_notes="Free to read.",
        terms_url="https://carsandbids.com/terms",
        limitations=(
            "Terms of Use prohibit manual or automated extraction, "
            "aggregation or reproduction of site content for commercial "
            "purposes. This platform therefore never fetches the site. "
            "Use it as a human reference only; any figure you record by hand "
            "is your own decision under their terms."
        ),
        enabled=0,
    ),
    dict(
        key="facebook_marketplace",
        name="Facebook Marketplace",
        access_method="manual",
        authorized="manual_only",
        requires_credentials=0,
        cost_notes="Free to read while logged in.",
        terms_url="https://www.facebook.com/legal/terms",
        limitations=(
            "No public Marketplace listing API. Meta's commerce APIs are "
            "partner-gated and aimed at posting inventory, not reading it. "
            "Manual URL entry only."
        ),
        enabled=1,
    ),
    dict(
        key="craigslist_rss",
        name="Craigslist search RSS",
        access_method="rss",
        authorized="operator_enabled",
        requires_credentials=0,
        cost_notes="Free.",
        terms_url="https://www.craigslist.org/about/terms.of.use/en",
        limitations=(
            "Craigslist publishes RSS on search-result pages and endorses RSS "
            "readers, but its terms prohibit scrapers and bulk harvesting. "
            "This connector is therefore OFF by default, polls slowly, and is "
            "only appropriate for a small number of personal saved searches. "
            "The feed gives title, price, URL and post date -- no VIN, no "
            "mileage, no photos. Read the terms and decide for yourself."
        ),
        enabled=0,
    ),
    dict(
        key="dealer_jsonld",
        name="Dealer sites publishing schema.org Vehicle data",
        access_method="jsonld",
        authorized="operator_enabled",
        requires_credentials=0,
        cost_notes="Free.",
        terms_url=None,
        limitations=(
            "Reads only the schema.org/Vehicle JSON-LD a dealer site "
            "deliberately publishes for search engines. Each domain must be "
            "added to the allowlist by the operator; robots.txt is checked "
            "and obeyed on every request, one page at a time, rate limited. "
            "No login, no paywall, no anti-bot circumvention -- ever."
        ),
        enabled=0,
    ),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    def cols(table):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    existing = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "comps" in existing:
        have = cols("comps")
        if "price_basis" not in have:
            conn.execute("ALTER TABLE comps ADD COLUMN price_basis TEXT NOT NULL DEFAULT 'unknown'")
        if "permission_basis" not in have:
            conn.execute("ALTER TABLE comps ADD COLUMN permission_basis TEXT NOT NULL DEFAULT 'unknown'")
        if "permission_note" not in have:
            conn.execute("ALTER TABLE comps ADD COLUMN permission_note TEXT")
    if "assumptions" in existing:
        if "status" not in cols("assumptions"):
            conn.execute("ALTER TABLE assumptions ADD COLUMN status TEXT NOT NULL "
                         "DEFAULT 'placeholder'")
            conn.execute("UPDATE assumptions SET status='verified' WHERE verified=1")
    if "sources" in existing:
        have = cols("sources")
        if "live_verified_at" not in have:
            conn.execute("ALTER TABLE sources ADD COLUMN live_verified_at TEXT")
        if "live_verified_note" not in have:
            conn.execute("ALTER TABLE sources ADD COLUMN live_verified_note TEXT")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    _migrate(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    now = utcnow()
    for s in SOURCE_REGISTRY:
        conn.execute(
            """INSERT INTO sources (key, name, access_method, authorized,
                                    requires_credentials, cost_notes, terms_url,
                                    limitations, enabled)
               VALUES (:key, :name, :access_method, :authorized,
                       :requires_credentials, :cost_notes, :terms_url,
                       :limitations, :enabled)
               ON CONFLICT(key) DO UPDATE SET
                   name=excluded.name,
                   access_method=excluded.access_method,
                   authorized=excluded.authorized,
                   requires_credentials=excluded.requires_credentials,
                   cost_notes=excluded.cost_notes,
                   terms_url=excluded.terms_url,
                   limitations=excluded.limitations""",
            s,
        )
    for a in _assumptions.DEFAULTS:
        conn.execute(
            """INSERT INTO assumptions (key, value, unit, basis, verified, status,
                                        updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   unit=excluded.unit, basis=excluded.basis""",
            (a.key, a.value, a.unit, a.basis, int(a.verified), a.status, now),
        )
    conn.commit()


def get_assumption(conn: sqlite3.Connection, key: str) -> float:
    row = conn.execute("SELECT value FROM assumptions WHERE key=?", (key,)).fetchone()
    if row is None:
        return _assumptions.default_value(key)
    return float(row["value"])


def all_assumptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM assumptions ORDER BY key").fetchall()


def set_assumption(conn: sqlite3.Connection, key: str, value: float,
                   basis: str | None = None, verified: bool | None = None) -> None:
    row = conn.execute("SELECT * FROM assumptions WHERE key=?", (key,)).fetchone()
    if row is None:
        raise KeyError(f"unknown assumption: {key}")
    new_verified = int(verified) if verified is not None else row["verified"]
    # Any explicit set clears 'unset': the operator has now supplied a number.
    new_status = "verified" if new_verified else "placeholder"
    conn.execute(
        """UPDATE assumptions SET value=?, basis=?, verified=?, status=?,
               updated_at=? WHERE key=?""",
        (
            float(value),
            basis if basis is not None else row["basis"],
            new_verified,
            new_status,
            utcnow(),
            key,
        ),
    )
    conn.commit()


def assumption_status(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT status FROM assumptions WHERE key=?", (key,)).fetchone()
    if row is None:
        return _assumptions.BY_KEY[key].status
    return row["status"]


def unset_assumptions(conn: sqlite3.Connection) -> list[str]:
    return [r["key"] for r in conn.execute(
        "SELECT key FROM assumptions WHERE status='unset' ORDER BY key")]


# ---------------------------------------------------------------------------
# Source run bookkeeping
# ---------------------------------------------------------------------------
def start_run(conn: sqlite3.Connection, source_key: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_runs (source_key, started_at, status) VALUES (?,?,'running')",
        (source_key, utcnow()),
    )
    conn.execute("UPDATE sources SET last_attempt_at=? WHERE key=?", (utcnow(), source_key))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, seen: int = 0,
               new: int = 0, updated: int = 0, message: str | None = None) -> None:
    now = utcnow()
    conn.execute(
        """UPDATE source_runs SET finished_at=?, status=?, items_seen=?,
           items_new=?, items_updated=?, message=? WHERE id=?""",
        (now, status, seen, new, updated, message, run_id),
    )
    row = conn.execute("SELECT source_key FROM source_runs WHERE id=?", (run_id,)).fetchone()
    if row:
        if status == "ok":
            conn.execute(
                "UPDATE sources SET last_success_at=?, last_error=NULL WHERE key=?",
                (now, row["source_key"]),
            )
        elif status == "error":
            conn.execute(
                "UPDATE sources SET last_error=? WHERE key=?", (message, row["source_key"])
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
LISTING_FIELDS = (
    "source_key source_listing_id url title year model generation variant "
    "body_style transmission drivetrain engine exterior_color interior_color "
    "mileage mileage_unit vin price currency listing_type auction_ends_at "
    "seller_type seller_name seller_city seller_state seller_zip status "
    "data_source_note notes"
).split()

# Fields whose absence materially weakens a valuation.
IMPORTANT_FIELDS = ["year", "generation", "variant", "mileage", "vin", "price",
                    "transmission", "seller_state", "body_style"]


def upsert_listing(conn: sqlite3.Connection, data: dict, raw: dict | None = None) -> tuple[int, bool]:
    """Insert or update a listing keyed on its URL. Returns (id, created)."""
    now = utcnow()
    url = data["url"]
    existing = conn.execute("SELECT * FROM listings WHERE url=?", (url,)).fetchone()
    payload = {k: data.get(k) for k in LISTING_FIELDS}
    payload["url"] = url
    payload.setdefault("model", "911")
    if payload.get("model") is None:
        payload["model"] = "911"
    if payload.get("mileage_unit") is None:
        payload["mileage_unit"] = "mi"
    if payload.get("currency") is None:
        payload["currency"] = "USD"
    if payload.get("status") is None:
        payload["status"] = "active"

    if existing is None:
        cols = LISTING_FIELDS + ["first_seen_at", "last_seen_at", "fetched_at", "raw"]
        vals = [payload[f] for f in LISTING_FIELDS] + [
            now, now, now, json.dumps(raw) if raw is not None else None
        ]
        cur = conn.execute(
            f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        listing_id, created = int(cur.lastrowid), True
    else:
        listing_id, created = int(existing["id"]), False
        # Only overwrite with non-null incoming values; never erase known data.
        sets, vals = [], []
        for f in LISTING_FIELDS:
            if f == "url":
                continue
            if payload.get(f) is not None:
                sets.append(f"{f}=?")
                vals.append(payload[f])
        sets += ["last_seen_at=?", "fetched_at=?"]
        vals += [now, now]
        if raw is not None:
            sets.append("raw=?")
            vals.append(json.dumps(raw))
        vals.append(listing_id)
        conn.execute(f"UPDATE listings SET {','.join(sets)} WHERE id=?", vals)

    price = payload.get("price")
    if price is not None:
        last = conn.execute(
            "SELECT price FROM price_history WHERE listing_id=? ORDER BY observed_at DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        if last is None or last["price"] != price:
            conn.execute(
                """INSERT INTO price_history (listing_id, observed_at, price, currency, source_key)
                   VALUES (?,?,?,?,?)""",
                (listing_id, now, price, payload.get("currency") or "USD", payload["source_key"]),
            )

    _refresh_missing_fields(conn, listing_id)
    conn.commit()
    return listing_id, created


def _refresh_missing_fields(conn: sqlite3.Connection, listing_id: int) -> None:
    row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    conn.execute("DELETE FROM missing_fields WHERE listing_id=?", (listing_id,))
    now = utcnow()
    for field in IMPORTANT_FIELDS:
        if row[field] in (None, "", 0):
            conn.execute(
                "INSERT OR IGNORE INTO missing_fields (listing_id, field, noted_at) VALUES (?,?,?)",
                (listing_id, field, now),
            )


def add_photos(conn: sqlite3.Connection, listing_id: int, urls: list[str]) -> int:
    now = utcnow()
    n = 0
    start = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS p FROM listing_photos WHERE listing_id=?",
        (listing_id,),
    ).fetchone()["p"]
    for i, u in enumerate(urls, start=int(start) + 1):
        cur = conn.execute(
            "INSERT OR IGNORE INTO listing_photos (listing_id, url, position, added_at) VALUES (?,?,?,?)",
            (listing_id, u, i, now),
        )
        n += cur.rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Per-listing overrides
# ---------------------------------------------------------------------------
def set_override(conn: sqlite3.Connection, listing_id: int, *, repairs=None,
                 transport=None, days_to_sell=None, note=None) -> None:
    existing = conn.execute(
        "SELECT * FROM listing_overrides WHERE listing_id=?", (listing_id,)).fetchone()
    cur = dict(existing) if existing else {}
    conn.execute(
        """INSERT INTO listing_overrides (listing_id, repairs, transport,
                                          days_to_sell, note, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(listing_id) DO UPDATE SET
               repairs=excluded.repairs, transport=excluded.transport,
               days_to_sell=excluded.days_to_sell, note=excluded.note,
               updated_at=excluded.updated_at""",
        (listing_id,
         repairs if repairs is not None else cur.get("repairs"),
         transport if transport is not None else cur.get("transport"),
         days_to_sell if days_to_sell is not None else cur.get("days_to_sell"),
         note if note is not None else cur.get("note"),
         utcnow()),
    )
    conn.commit()


def get_override(conn: sqlite3.Connection, listing_id) -> dict:
    if listing_id is None:
        return {}
    row = conn.execute(
        "SELECT * FROM listing_overrides WHERE listing_id=?", (listing_id,)).fetchone()
    return dict(row) if row else {}

"""Comparable completed sales.

The single hard rule of this module: a comp is a transaction that actually
happened and can be re-opened by a human. Every row therefore requires a real
http(s) source URL, a sale date and a sale price. There is no code path that
generates, estimates or interpolates a comp. If you have no comps, the
valuation engine says so rather than producing a number.

Where comps come from in practice:
  * Bring a Trailer completed auctions  -- recorded by hand from the result page
  * Cars & Bids completed auctions      -- recorded by hand (their terms forbid extraction)
  * CLASSIC.COM sold listings           -- recorded by hand, or via their alerts
  * Dealer 'sold' pages / your own sales
  * MarketCheck sales-history endpoints, if your plan includes them
"""

from __future__ import annotations

import csv
import datetime as _dt
import sqlite3
from pathlib import Path

from . import db as _db
from . import generations as _gens
from . import vin as _vin

VENUES = {"bring_a_trailer", "cars_and_bids", "classic_com", "pcarmarket",
          "dealer", "private", "auction_house", "marketcheck", "own_sale", "other"}

# --- Is the number a price someone actually paid? --------------------------
# Only VERIFIED_TRANSACTION is usable by the valuation engine. The other two
# real-world cases exist because aggregators carry them and they are routinely
# mistaken for sales:
#   last_asking           -- CLASSIC.COM preserves a removed listing's final
#                            asking price when no sold price was provided.
#   inferred_from_removal -- a listing disappeared from dealer inventory. That
#                            is a REMOVAL, not a transaction. It may have been
#                            sold, withdrawn, traded or relisted elsewhere.
PRICE_BASES = {
    "verified_transaction": "A documented completed sale at a stated price.",
    "last_asking": "Final asking price of a removed listing. NOT a sale price.",
    "inferred_from_removal": "Listing was removed; a sale is INFERRED. NOT a sale price.",
    "unknown": "Provenance not established.",
}
USABLE_PRICE_BASIS = "verified_transaction"

# --- Are we permitted to hold and use this record? -------------------------
# 'unknown' is excluded from the valuation engine. This is what stops the tool
# from quietly accumulating a database of someone else's auction results.
PERMISSION_BASES = {
    "licensed_api": "Received under a data licence that permits this use.",
    "own_transaction": "Your own purchase or sale.",
    "seller_disclosed": "The counterparty told you directly.",
    "public_record": "Government or other genuinely public record.",
    "operator_asserts_permission": "You have confirmed you may use this record. "
                                   "Record how in --permission-note.",
    "unknown": "No basis established -- excluded from valuations.",
}
USABLE_PERMISSION_BASES = {"licensed_api", "own_transaction", "seller_disclosed",
                           "public_record", "operator_asserts_permission"}


class CompRejected(ValueError):
    pass


def _parse_date(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise CompRejected(f"Could not read sale date {value!r}. Use YYYY-MM-DD.")


def add_comp(conn: sqlite3.Connection, *, generation: str, sale_price: float,
             sale_date: str, source_url: str, venue: str = "other",
             variant: str | None = None, year: int | None = None,
             mileage: int | None = None, transmission: str | None = None,
             body_style: str | None = None, vin: str | None = None,
             condition_note: str | None = None, includes_fees: bool = False,
             source_key: str | None = None, recorded_by: str | None = None,
             price_basis: str = "unknown", permission_basis: str = "unknown",
             permission_note: str | None = None,
             is_synthetic: bool = False) -> int:
    """Record one completed sale. Raises CompRejected on bad input.

    price_basis and permission_basis both default to 'unknown', which means the
    row is stored but is NOT usable by the valuation engine. That default is
    deliberate: a figure has to earn its way into a valuation.
    """
    if not is_synthetic:
        if not str(source_url).lower().startswith(("http://", "https://")):
            raise CompRejected(
                "A comp needs a real http(s) URL pointing at the completed sale. "
                "If you cannot link it, do not record it."
            )
    sale_date = _parse_date(sale_date)
    today = _dt.date.today()
    if _dt.date.fromisoformat(sale_date) > today:
        raise CompRejected(f"Sale date {sale_date} is in the future.")
    try:
        sale_price = float(sale_price)
    except (TypeError, ValueError):
        raise CompRejected(f"Sale price {sale_price!r} is not a number.")
    if sale_price <= 0:
        raise CompRejected("Sale price must be positive.")

    generation = (generation or "").strip()
    if generation not in _gens.VALID:
        raise CompRejected(
            f"Unknown generation {generation!r}. Valid: {sorted(_gens.VALID)}"
        )
    if venue not in VENUES:
        raise CompRejected(f"Unknown venue {venue!r}. Valid: {sorted(VENUES)}")
    if price_basis not in PRICE_BASES:
        raise CompRejected(
            f"Unknown price_basis {price_basis!r}. Valid: {sorted(PRICE_BASES)}")
    if permission_basis not in PERMISSION_BASES:
        raise CompRejected(
            f"Unknown permission_basis {permission_basis!r}. "
            f"Valid: {sorted(PERMISSION_BASES)}")
    if permission_basis == "operator_asserts_permission" and not permission_note:
        raise CompRejected(
            "operator_asserts_permission requires --permission-note describing "
            "how you are permitted to use this record.")

    source_key = source_key or (venue if venue in
                                {"bring_a_trailer", "cars_and_bids", "classic_com", "marketcheck"}
                                else "manual")

    cur = conn.execute(
        """INSERT INTO comps (generation, variant, year, body_style, transmission,
                              mileage, sale_price, currency, sale_date, venue,
                              source_key, source_url, vin, condition_note,
                              includes_fees, price_basis, permission_basis,
                              permission_note, is_synthetic, recorded_at, recorded_by)
           VALUES (?,?,?,?,?,?,?, 'USD', ?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_url, vin, sale_date) DO UPDATE SET
               sale_price=excluded.sale_price, mileage=excluded.mileage,
               condition_note=excluded.condition_note,
               price_basis=excluded.price_basis,
               permission_basis=excluded.permission_basis,
               permission_note=excluded.permission_note""",
        (generation, _gens.normalize_variant(variant) or variant,
         int(year) if year else None,
         _gens.normalize_body_style(body_style) or body_style,
         _gens.normalize_transmission(transmission) or transmission,
         int(mileage) if mileage else None, sale_price, sale_date, venue,
         source_key, source_url, _vin.normalize(vin), condition_note,
         int(includes_fees), price_basis, permission_basis, permission_note,
         int(is_synthetic), _db.utcnow(), recorded_by),
    )
    conn.commit()
    return int(cur.lastrowid)


CSV_COLUMNS = ["generation", "variant", "year", "body_style", "transmission",
               "mileage", "sale_price", "sale_date", "venue", "source_url",
               "vin", "condition_note", "includes_fees",
               "price_basis", "permission_basis", "permission_note"]


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerow(CSV_COLUMNS)


def import_csv(conn: sqlite3.Connection, path: Path,
               allow_synthetic: bool = False) -> tuple[int, list[str]]:
    """Import comps from a CSV. Returns (imported, errors)."""
    imported, errors = 0, []
    with Path(path).open(newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            try:
                add_comp(
                    conn,
                    generation=(row.get("generation") or "").strip(),
                    variant=(row.get("variant") or "").strip() or None,
                    year=int(row["year"]) if (row.get("year") or "").strip() else None,
                    body_style=(row.get("body_style") or "").strip() or None,
                    transmission=(row.get("transmission") or "").strip() or None,
                    mileage=int(float(row["mileage"])) if (row.get("mileage") or "").strip() else None,
                    sale_price=(row.get("sale_price") or "").replace("$", "").replace(",", ""),
                    sale_date=(row.get("sale_date") or "").strip(),
                    venue=(row.get("venue") or "other").strip() or "other",
                    source_url=(row.get("source_url") or "").strip(),
                    vin=(row.get("vin") or "").strip() or None,
                    condition_note=(row.get("condition_note") or "").strip() or None,
                    includes_fees=str(row.get("includes_fees", "")).strip().lower()
                                  in ("1", "true", "yes", "y"),
                    price_basis=(row.get("price_basis") or "unknown").strip() or "unknown",
                    permission_basis=(row.get("permission_basis") or "unknown").strip() or "unknown",
                    permission_note=(row.get("permission_note") or "").strip() or None,
                    is_synthetic=allow_synthetic,
                )
                imported += 1
            except CompRejected as exc:
                errors.append(f"line {i}: {exc}")
            except ValueError as exc:
                errors.append(f"line {i}: {exc}")
    return imported, errors


def usable_sql_clause(alias: str = "") -> str:
    """The SQL predicate defining a comp the valuation engine may use."""
    a = f"{alias}." if alias else ""
    perms = ",".join(f"'{p}'" for p in sorted(USABLE_PERMISSION_BASES))
    return (f"{a}price_basis = '{USABLE_PRICE_BASIS}' "
            f"AND {a}permission_basis IN ({perms})")


def coverage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per generation: how many comps are stored, and how many are actually
    usable. The gap between the two is the honest picture of what is blocking
    this tool from valuing anything."""
    return conn.execute(
        f"""SELECT generation, variant, COUNT(*) AS n_stored,
                   SUM(CASE WHEN {usable_sql_clause()} THEN 1 ELSE 0 END) AS n_usable,
                   SUM(CASE WHEN price_basis!='verified_transaction' THEN 1 ELSE 0 END)
                       AS n_not_a_sale,
                   SUM(CASE WHEN permission_basis='unknown' THEN 1 ELSE 0 END)
                       AS n_no_permission,
                   MIN(sale_date) AS oldest, MAX(sale_date) AS newest
            FROM comps WHERE is_synthetic=0
            GROUP BY generation, variant ORDER BY generation, n_usable DESC"""
    ).fetchall()

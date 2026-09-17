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
             is_synthetic: bool = False) -> int:
    """Record one documented completed sale. Raises CompRejected on bad input."""
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

    source_key = source_key or (venue if venue in
                                {"bring_a_trailer", "cars_and_bids", "classic_com", "marketcheck"}
                                else "manual")

    cur = conn.execute(
        """INSERT INTO comps (generation, variant, year, body_style, transmission,
                              mileage, sale_price, currency, sale_date, venue,
                              source_key, source_url, vin, condition_note,
                              includes_fees, is_synthetic, recorded_at, recorded_by)
           VALUES (?,?,?,?,?,?,?, 'USD', ?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_url, vin, sale_date) DO UPDATE SET
               sale_price=excluded.sale_price, mileage=excluded.mileage,
               condition_note=excluded.condition_note""",
        (generation, _gens.normalize_variant(variant) or variant,
         int(year) if year else None,
         _gens.normalize_body_style(body_style) or body_style,
         _gens.normalize_transmission(transmission) or transmission,
         int(mileage) if mileage else None, sale_price, sale_date, venue,
         source_key, source_url, _vin.normalize(vin), condition_note,
         int(includes_fees), int(is_synthetic), _db.utcnow(), recorded_by),
    )
    conn.commit()
    return int(cur.lastrowid)


CSV_COLUMNS = ["generation", "variant", "year", "body_style", "transmission",
               "mileage", "sale_price", "sale_date", "venue", "source_url",
               "vin", "condition_note", "includes_fees"]


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
                    is_synthetic=allow_synthetic,
                )
                imported += 1
            except CompRejected as exc:
                errors.append(f"line {i}: {exc}")
            except ValueError as exc:
                errors.append(f"line {i}: {exc}")
    return imported, errors


def coverage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """How many usable comps exist per generation -- the honest picture of
    whether this platform can value anything yet."""
    return conn.execute(
        """SELECT generation, variant, COUNT(*) AS n,
                  MIN(sale_date) AS oldest, MAX(sale_date) AS newest
           FROM comps WHERE is_synthetic=0
           GROUP BY generation, variant ORDER BY generation, n DESC"""
    ).fetchall()

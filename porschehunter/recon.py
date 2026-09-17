"""Per-car reconditioning cost workflow (Task 6).

The operator's friend enters real numbers for each category, tagged as an
estimate, a written quote, or a paid invoice. The total feeds the cost-side of
the screen. A recorded recon total for a car beats the generic repair
assumption (see filters._cost_estimate and deal.py overrides).
"""

from __future__ import annotations

from . import db as _db

# (key, label). The list the friend fills in.
CATEGORIES = [
    ("ppi", "Pre-purchase inspection"),
    ("engine_mechanical", "Engine & mechanical repairs"),
    ("transmission", "Transmission issues"),
    ("tires_brakes", "Tires & brakes"),
    ("suspension", "Suspension"),
    ("paint_body", "Paint & bodywork"),
    ("interior", "Interior repairs"),
    ("detailing", "Detailing"),
    ("photography", "Photography"),
    ("shipping", "Shipping"),
    ("other", "Other costs"),
]
CATEGORY_KEYS = {k for k, _ in CATEGORIES}

BASES = ("estimate", "quote", "invoice")


class ReconError(ValueError):
    pass


def set_cost(conn, listing_id: int, category: str, amount: float,
             basis: str = "estimate", note: str | None = None) -> None:
    if category not in CATEGORY_KEYS:
        raise ReconError(f"unknown category '{category}'. "
                         f"One of: {', '.join(sorted(CATEGORY_KEYS))}")
    if basis not in BASES:
        raise ReconError(f"basis must be one of {BASES}, got '{basis}'")
    if amount is None or float(amount) < 0:
        raise ReconError("amount must be a non-negative number")
    exists = conn.execute(
        "SELECT id FROM listings WHERE id=?", (listing_id,)).fetchone()
    if exists is None:
        raise ReconError(f"no listing #{listing_id}")
    conn.execute(
        """INSERT INTO recon_costs (listing_id, category, amount, basis, note, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(listing_id, category) DO UPDATE SET
               amount=excluded.amount, basis=excluded.basis,
               note=excluded.note, updated_at=excluded.updated_at""",
        (listing_id, category, float(amount), basis, note, _db.utcnow()))
    conn.commit()


def delete_cost(conn, listing_id: int, category: str) -> None:
    conn.execute("DELETE FROM recon_costs WHERE listing_id=? AND category=?",
                 (listing_id, category))
    conn.commit()


def get_costs(conn, listing_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM recon_costs WHERE listing_id=? ORDER BY category", (listing_id,))]


def summary(conn, listing_id: int) -> dict:
    """Total plus the weakest basis present (an estimate makes the whole total
    an estimate for underwriting purposes)."""
    rows = get_costs(conn, listing_id)
    total = sum(float(r["amount"]) for r in rows)
    bases = {r["basis"] for r in rows}
    if not rows:
        weakest = None
    elif "estimate" in bases:
        weakest = "estimate"
    elif "quote" in bases:
        weakest = "quote"
    else:
        weakest = "invoice"
    return {
        "listing_id": listing_id,
        "total": round(total, 2),
        "n_lines": len(rows),
        "weakest_basis": weakest,
        "fully_invoiced": bool(rows) and bases == {"invoice"},
        "lines": rows,
    }

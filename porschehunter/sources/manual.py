"""Manual entry -- the fallback for every marketplace without authorized
programmatic access (Facebook Marketplace, Cars & Bids, Bring a Trailer, and
any dealer site not opted in).

The operator supplies the URL and whatever fields they can read on the page.
Nothing is inferred beyond parsing the operator's own title text and decoding
the VIN they typed. Missing fields stay missing and are recorded as missing.
"""

from __future__ import annotations

import urllib.parse

from .. import db as _db
from .. import generations as _gens
from .. import model_guard as _guard
from .. import vin as _vin
from .base import IngestResult


class NotA911(ValueError):
    """Raised when a manually-entered listing is not a Porsche 911."""

# Maps a hostname to the registered source key, so a pasted URL is filed
# against the marketplace it actually came from.
HOST_SOURCE_MAP = {
    "bringatrailer.com": "bring_a_trailer",
    "carsandbids.com": "cars_and_bids",
    "www.classic.com": "classic_com",
    "classic.com": "classic_com",
    "facebook.com": "facebook_marketplace",
    "www.facebook.com": "facebook_marketplace",
    "m.facebook.com": "facebook_marketplace",
    "craigslist.org": "craigslist_rss",
}


def source_key_for_url(url: str) -> str:
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    if host in HOST_SOURCE_MAP:
        return HOST_SOURCE_MAP[host]
    for suffix, key in HOST_SOURCE_MAP.items():
        if host.endswith("." + suffix) or host == suffix:
            return key
    return "manual"


def add_listing(conn, url: str, *, title=None, year=None, generation=None,
                variant=None, body_style=None, transmission=None, mileage=None,
                vin=None, price=None, seller_type=None, seller_city=None,
                seller_state=None, seller_zip=None, listing_type=None,
                exterior_color=None, notes=None, photos=None,
                decode_vin=True) -> tuple[int, bool, dict]:
    """Record a listing the operator typed in. Returns (id, created, report)."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("A listing needs a real http(s) URL so it stays traceable.")

    source_key = source_key_for_url(url)
    src = conn.execute("SELECT * FROM sources WHERE key=?", (source_key,)).fetchone()
    report = {"source_key": source_key, "notices": []}
    if src is not None and src["authorized"] == "prohibited":
        report["notices"].append(
            f"{src['name']} terms prohibit automated extraction. This record is "
            "an operator-entered reference only; nothing is fetched from the site."
        )

    # Fill blanks from the operator's own title text -- never from the network.
    year = year or _gens.parse_year(title)
    variant = variant or _gens.normalize_variant(title)
    body_style = body_style or _gens.normalize_body_style(title)
    transmission = transmission or _gens.normalize_transmission(title)

    vin_norm = _vin.normalize(vin)
    if vin and not vin_norm:
        report["notices"].append(f"VIN '{vin}' is not a valid 17-character VIN; stored as unknown.")

    if vin_norm:
        off = _vin.offline_summary(vin_norm)
        report["vin_offline"] = off
        report["notices"].extend(off["warnings"])
        if year is None:
            year = off.get("model_year")

    # 911-only rule. The operator asserting a URL is a positive signal, but a
    # non-911 model named in the title/variant is a hard reject.
    verdict, reason = _guard.classify_911(
        title=title, variant=variant, operator_asserted=True)
    if verdict == _guard.VERDICT_REJECTED:
        raise NotA911(
            f"This is not a Porsche 911 ({reason}). This tool is 911-only; "
            "the listing was not added.")

    if generation is None:
        generation, ambiguous = _gens.from_year(year)
        if ambiguous:
            report["notices"].append(
                f"Model year {year} spans two generations; '{generation}' assumed. "
                "Set --generation explicitly to override."
            )
    if generation and generation not in _gens.VALID:
        report["notices"].append(f"Unrecognised generation '{generation}'.")

    listing_id, created = _db.upsert_listing(conn, {
        "source_key": source_key,
        "url": url,
        "title": title,
        "year": year,
        "generation": generation,
        "variant": variant,
        "body_style": body_style,
        "transmission": transmission,
        "mileage": mileage,
        "vin": vin_norm,
        "price": price,
        "listing_type": listing_type,
        "seller_type": seller_type,
        "seller_city": seller_city,
        "seller_state": seller_state,
        "seller_zip": seller_zip,
        "exterior_color": exterior_color,
        "notes": notes,
        "data_source_note": "operator-entered from the listing page",
    })

    if photos:
        _db.add_photos(conn, listing_id, photos)

    if vin_norm and decode_vin:
        res = _vin.decode_and_store(conn, vin_norm, model_year=year)
        report["vin_decode_status"] = res["status"]
        if res["status"] == "offline_only":
            report["notices"].append(
                "NHTSA vPIC was unreachable, so only the offline VIN checks ran. "
                "Re-run `vin decode` when you have network access."
            )
        elif res["status"] == "decoded":
            row = res["row"]
            if row.get("make") and "PORSCHE" not in (row["make"] or "").upper():
                report["notices"].append(
                    f"vPIC says this VIN is a {row['make']}, not a Porsche. Check the VIN."
                )
            # The VIN is authoritative: if it decodes to a non-911 model, the
            # row is quarantined out of the active 911 inventory (audit trail
            # kept) even though the operator asserted it was a 911.
            v2, reason2 = _guard.classify_911(
                title=title, variant=variant, vin_model=row.get("model"),
                operator_asserted=True)
            if v2 == _guard.VERDICT_REJECTED:
                conn.execute(
                    "UPDATE listings SET status='quarantined', quarantine_reason=? WHERE id=?",
                    (reason2, listing_id))
                conn.commit()
                report["notices"].append(f"Quarantined (not a 911): {reason2}.")
                report["verdict"] = "quarantine"

    missing = [r["field"] for r in conn.execute(
        "SELECT field FROM missing_fields WHERE listing_id=? ORDER BY field", (listing_id,))]
    report["missing_fields"] = missing
    return listing_id, created, report


def ingest(conn, urls: list[str]) -> IngestResult:
    """Bulk-add bare URLs with no details -- placeholders to fill in later."""
    res = IngestResult(source_key="manual")
    for u in urls:
        try:
            lid, created, _ = add_listing(conn, u, decode_vin=False)
        except ValueError as exc:
            res.status = "error"
            res.message += f"{u}: {exc}\n"
            continue
        res.seen += 1
        res.new += int(created)
        res.updated += int(not created)
        res.listing_ids.append(lid)
    return res

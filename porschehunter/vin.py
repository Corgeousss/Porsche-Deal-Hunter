"""Porsche VIN handling: offline structural checks + NHTSA vPIC lookup.

The offline part is arithmetic on the VIN itself, so it works with no network
and invents nothing. The authoritative decode comes from NHTSA vPIC, which is
free, needs no key and is recorded with a timestamp in `vin_decodes`.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import db as _db
from . import generations as _gens

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# 10th character -> model year (ISO 3779 / North American convention).
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"


def normalize(vin: str | None) -> str | None:
    if not vin:
        return None
    v = re.sub(r"[^A-Za-z0-9]", "", vin).upper()
    return v if VIN_RE.match(v) else None


def is_valid_check_digit(vin: str) -> bool:
    """North American VIN check digit (position 9). Porsche US cars comply."""
    translit = {**{str(d): d for d in range(10)}}
    for i, ch in enumerate("ABCDEFGHJKLMNPRSTUVWXYZ"):
        translit[ch] = [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9][i]
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    total = sum(translit[c] * w for c, w in zip(vin, weights))
    rem = total % 11
    expected = "X" if rem == 10 else str(rem)
    return vin[8] == expected


def model_year_from_vin(vin: str, assume_recent: bool = True) -> int | None:
    """Decode the 10th character. The code cycles every 30 years, so this
    returns the most recent matching year when `assume_recent` is set."""
    code = vin[9]
    if code not in _YEAR_CODES:
        return None
    idx = _YEAR_CODES.index(code)
    base = 1980 + idx
    while assume_recent and base + 30 <= 2026:
        base += 30
    return base


def offline_summary(vin_raw: str | None) -> dict:
    """Everything derivable from the VIN string alone. No network, no guessing
    about equipment -- structure only."""
    vin = normalize(vin_raw)
    out = {
        "vin": vin,
        "valid_format": vin is not None,
        "check_digit_ok": None,
        "is_porsche_wmi": None,
        "model_year": None,
        "generation": None,
        "generation_ambiguous": False,
        "warnings": [],
    }
    if vin is None:
        if vin_raw:
            out["warnings"].append("VIN is not 17 valid characters.")
        return out
    out["check_digit_ok"] = is_valid_check_digit(vin)
    if not out["check_digit_ok"]:
        out["warnings"].append(
            "VIN check digit does not validate -- likely a transcription error "
            "or a non-US-spec VIN. Confirm against the car before acting."
        )
    wmi = vin[:3]
    out["is_porsche_wmi"] = wmi.startswith("WP0") or wmi.startswith("WP1")
    if not out["is_porsche_wmi"]:
        out["warnings"].append(f"World Manufacturer Identifier '{wmi}' is not a Porsche WMI.")
    y = model_year_from_vin(vin)
    out["model_year"] = y
    gen, ambiguous = _gens.from_year(y)
    out["generation"] = gen
    out["generation_ambiguous"] = ambiguous
    if ambiguous:
        out["warnings"].append(
            f"Model year {y} spans two generations; confirm from the car or the decode."
        )
    return out


VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"


def decode_via_vpic(vin: str, model_year: int | None = None, timeout: int = 20) -> dict:
    """Call the free NHTSA vPIC decoder. Raises on network/HTTP failure."""
    url = VPIC_URL.format(vin=urllib.parse.quote(vin))
    if model_year:
        url += f"&modelyear={model_year}"
    req = urllib.request.Request(url, headers={"User-Agent": "porsche-deal-hunter/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    results = payload.get("Results") or []
    if not results:
        raise ValueError("vPIC returned no results")
    return results[0]


def decode_and_store(conn, vin_raw: str, model_year: int | None = None,
                     force: bool = False) -> dict:
    """Decode a VIN and cache the result. Returns a dict with `status`:
    cached | decoded | offline_only | invalid."""
    vin = normalize(vin_raw)
    if vin is None:
        return {"status": "invalid", "offline": offline_summary(vin_raw)}

    if not force:
        row = conn.execute("SELECT * FROM vin_decodes WHERE vin=?", (vin,)).fetchone()
        if row is not None and not row["error_text"]:
            return {"status": "cached", "row": dict(row), "offline": offline_summary(vin)}

    offline = offline_summary(vin)
    try:
        r = decode_via_vpic(vin, model_year or offline.get("model_year"))
    except Exception as exc:  # network blocked, timeout, HTTP error
        conn.execute(
            """INSERT INTO vin_decodes (vin, decoded_at, decoder, error_text, raw)
               VALUES (?,?,'nhtsa_vpic',?,NULL)
               ON CONFLICT(vin) DO UPDATE SET decoded_at=excluded.decoded_at,
                                              error_text=excluded.error_text""",
            (vin, _db.utcnow(), f"{type(exc).__name__}: {exc}"),
        )
        conn.commit()
        return {"status": "offline_only", "offline": offline, "error": str(exc)}

    err = (r.get("ErrorCode") or "").strip()
    engine = " ".join(
        str(r.get(k)) for k in ("DisplacementL", "EngineCylinders", "EngineConfiguration")
        if r.get(k)
    ).strip() or None
    my = r.get("ModelYear")
    conn.execute(
        """INSERT INTO vin_decodes (vin, decoded_at, decoder, make, model, model_year,
                                    trim, body_class, engine, plant, error_text, raw)
           VALUES (?,?,'nhtsa_vpic',?,?,?,?,?,?,?,?,?)
           ON CONFLICT(vin) DO UPDATE SET
               decoded_at=excluded.decoded_at, make=excluded.make, model=excluded.model,
               model_year=excluded.model_year, trim=excluded.trim,
               body_class=excluded.body_class, engine=excluded.engine,
               plant=excluded.plant, error_text=excluded.error_text, raw=excluded.raw""",
        (
            vin, _db.utcnow(), r.get("Make") or None, r.get("Model") or None,
            int(my) if str(my).isdigit() else None,
            r.get("Trim") or None, r.get("BodyClass") or None, engine,
            r.get("PlantCity") or None,
            err if err not in ("", "0") else None,
            json.dumps(r),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM vin_decodes WHERE vin=?", (vin,)).fetchone()
    return {"status": "decoded", "row": dict(row), "offline": offline}

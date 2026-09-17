"""Single source of truth for "is this actually a Porsche 911?".

This application is EXCLUSIVELY for Porsche 911s. Every ingestion source --
dealer JSON-LD, MarketCheck, manual/Facebook-assisted imports, and anything
added later -- must run each candidate through :func:`classify_911` before it
can enter the active 911 inventory.

The model is established from the strongest signal available, not from the mere
presence of "911" in a URL or stock number (a dealer stock number like
``id-65391149`` contains the digits 911; a Cayman can be advertised with
"Carrera S wheels"):

1. **VIN decode (authoritative).** NHTSA vPIC returns the model for the VIN. If
   it says 911 -> confirmed; if it names Cayman/Boxster/etc -> rejected.
2. **Structured title / model / variant text.** A non-911 model name -> rejected;
   a positive 911 signal (911 / Carrera / Targa as a real token) -> confirmed.
3. **Otherwise -> quarantine.** The model could not be confidently established,
   so the listing is held out of the active 911 inventory (with an audit trail)
   rather than imported as a 911.
"""

from __future__ import annotations

import re

# A real 911 token. "911" must have digit boundaries so a stock/ID number that
# merely contains those digits does not count.
_NINE11_RE = re.compile(r"(?<!\d)911(?!\d)|carrera|targa", re.IGNORECASE)

# Every other Porsche line. These are hard exclusions.
_NOT_911_RE = re.compile(
    r"\b(cayman|boxster|macan|cayenne|panamera|taycan|718|914|924|928|944|968|"
    r"356|550|959|918|carrera\s+gt)\b",
    re.IGNORECASE)

VERDICT_CONFIRMED = "confirmed"
VERDICT_REJECTED = "rejected"
VERDICT_QUARANTINE = "quarantine"


def is_non_911_model(text: str | None) -> bool:
    """True if the text names a Porsche model that is not the 911."""
    return bool(_NOT_911_RE.search(text or ""))


def is_911(text: str | None) -> bool:
    """True if the text positively identifies a 911 and names no other model."""
    if is_non_911_model(text):
        return False
    return bool(_NINE11_RE.search(text or ""))


def looks_like_911_url(url: str) -> bool:
    """URL-level pre-filter for sitemap discovery (cheap, not authoritative)."""
    return is_911(url)


def classify_911(*, title: str | None = None, variant: str | None = None,
                 model: str | None = None, vin_model: str | None = None,
                 url: str | None = None,
                 operator_asserted: bool = False) -> tuple[str, str]:
    """Return (verdict, reason). verdict is confirmed | rejected | quarantine.

    ``vin_model`` is the model string from a VIN decode (NHTSA vPIC), the
    strongest signal. ``operator_asserted`` is set for manual / assisted imports
    where a human has stated this is a 911 -- that counts as a positive signal so
    a sparse hand entry is not needlessly quarantined, but a hard non-911 model
    name (in the entry or the VIN) still rejects it.
    """
    # 1. VIN decode is authoritative when present.
    if vin_model:
        vm = vin_model.strip()
        if vm:
            if is_non_911_model(vm):
                return VERDICT_REJECTED, f"VIN decodes to '{vm}', not a 911"
            if _NINE11_RE.search(vm):
                return VERDICT_CONFIRMED, f"VIN decodes to model '{vm}'"

    # 2. Structured text. A non-911 model name anywhere is a hard reject.
    struct = " ".join(x for x in (title, model, variant) if x)
    if is_non_911_model(struct):
        return VERDICT_REJECTED, "title/model names a non-911 Porsche model"

    # 3. A positive 911 signal in the structured fields confirms it.
    if _NINE11_RE.search(title or "") or _NINE11_RE.search(variant or "") \
            or _NINE11_RE.search(model or ""):
        return VERDICT_CONFIRMED, "title/model/variant identifies a 911"

    # 4. Operator vouches for it and nothing contradicts -> accept.
    if operator_asserted:
        return VERDICT_CONFIRMED, "operator-asserted 911 (no conflicting signal)"

    # 5. Could not establish the model confidently.
    return VERDICT_QUARANTINE, "model could not be confidently established as a 911"


def audit_db(conn, apply: bool = True) -> dict:
    """Re-verify every active listing and quarantine any that is not a confirmed
    911. Uses the stored VIN decode (authoritative) plus structured text.

    A quarantined row is kept (status='quarantined' + quarantine_reason) so there
    is an audit trail; it is excluded from active search and valuation. Returns a
    summary with counts and the affected rows. ``apply=False`` reports only.
    """
    rows = conn.execute(
        "SELECT id, title, variant, vin, status FROM listings "
        "WHERE status='active'").fetchall()
    flagged = []
    for r in rows:
        vin_model = None
        if r["vin"]:
            d = conn.execute("SELECT model FROM vin_decodes WHERE vin=?",
                             (r["vin"],)).fetchone()
            vin_model = d["model"] if d else None
        verdict, reason = classify_911(title=r["title"], variant=r["variant"],
                                       vin_model=vin_model)
        if verdict != VERDICT_CONFIRMED:
            flagged.append({"id": r["id"], "title": r["title"],
                            "verdict": verdict, "reason": reason})
            if apply:
                conn.execute(
                    "UPDATE listings SET status='quarantined', quarantine_reason=? "
                    "WHERE id=?", (reason, r["id"]))
    if apply and flagged:
        conn.commit()
    return {
        "checked": len(rows),
        "quarantined": len(flagged),
        "rejected_model": sum(1 for f in flagged if f["verdict"] == VERDICT_REJECTED),
        "uncertain": sum(1 for f in flagged if f["verdict"] == VERDICT_QUARANTINE),
        "flagged": flagged,
    }

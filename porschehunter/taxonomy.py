"""Normalized Porsche 911 model taxonomy for the filter UI.

This is US-market REFERENCE data, not market data: which body/variant options a
given generation actually shipped with. It exists so the filter interface can
offer only combinations that really exist for the selected generations, and so
generation is never confused with trim.

It intentionally uses the SAME variant vocabulary as
``generations.normalize_variant`` so a stored listing's normalized variant maps
straight onto a filter checkbox. Where the real line-up is richer than that
vocabulary (limited editions, T/E/S sub-trims of the earliest cars), the extra
trims are simply not offered as filters rather than invented here.

Nothing in this module reads the database or the network.
"""

from __future__ import annotations

from . import generations as _gens

# Canonical generation order (matches generations.GENERATIONS keys).
ALL_GENERATIONS = [g for g, *_ in _gens.GENERATIONS]

# UI grouping. Each group is (label, [generation keys]). "930" is the Turbo of
# the air-cooled G-series, not a separate generation in this codebase's scheme,
# so it is named in the air-cooled group's label rather than invented as a key.
GENERATION_GROUPS = [
    ("Air-cooled (901 / G-body-930 / 964 / 993)", ["901", "G", "964", "993"]),
    ("996 (996.1 / 996.2)", ["996.1", "996.2"]),
    ("997 (997.1 / 997.2)", ["997.1", "997.2"]),
    ("991 (991.1 / 991.2)", ["991.1", "991.2"]),
    ("992 (992.1 / 992.2)", ["992.1", "992.2"]),
]

GENERATION_LABELS = {
    "901": "901 (1964-73, long hood)",
    "G": "G-series / 930 (1974-89, air-cooled)",
    "964": "964 (1989-94, air-cooled)",
    "993": "993 (1995-98, last air-cooled)",
    "996.1": "996.1 (1999-2001)",
    "996.2": "996.2 (2002-04)",
    "997.1": "997.1 (2005-08)",
    "997.2": "997.2 (2009-12)",
    "991.1": "991.1 (2012-16)",
    "991.2": "991.2 (2017-19)",
    "992.1": "992.1 (2020-24)",
    "992.2": "992.2 (2025+)",
}

# Which normalized variants each generation actually offered (US market).
# Kept deliberately conservative: a trim appears only where that generation
# genuinely sold it. Body styles (coupe/cabriolet/targa/speedster) are tracked
# separately in AVAILABLE_BODIES, not here.
AVAILABLE_VARIANTS = {
    "901":   ["Carrera"],
    "G":     ["Carrera", "Turbo"],
    "964":   ["Carrera", "Carrera 4", "Turbo"],
    "993":   ["Carrera", "Carrera S", "Carrera 4", "Carrera 4S", "Turbo",
              "Turbo S", "GT2"],
    "996.1": ["Carrera", "Carrera 4", "Turbo", "GT3", "GT2"],
    "996.2": ["Carrera", "Carrera 4", "Carrera 4S", "Turbo", "Turbo S",
              "GT3", "GT2"],
    "997.1": ["Carrera", "Carrera S", "Carrera 4", "Carrera 4S", "Targa 4",
              "Targa 4S", "Turbo", "GT3", "GT3 RS", "GT2"],
    "997.2": ["Carrera", "Carrera S", "Carrera 4", "Carrera 4S",
              "Carrera GTS", "Carrera 4 GTS", "Targa 4", "Targa 4S",
              "Turbo", "Turbo S", "GT3", "GT3 RS", "GT2 RS", "Speedster"],
    "991.1": ["Carrera", "Carrera S", "Carrera 4", "Carrera 4S",
              "Carrera GTS", "Carrera 4 GTS", "Targa 4", "Targa 4S",
              "Turbo", "Turbo S", "GT3", "GT3 RS"],
    "991.2": ["Carrera", "Carrera T", "Carrera S", "Carrera 4", "Carrera 4S",
              "Carrera GTS", "Carrera 4 GTS", "Targa 4", "Targa 4S",
              "Turbo", "Turbo S", "GT3", "GT3 Touring", "GT3 RS", "GT2 RS",
              "Speedster"],
    "992.1": ["Carrera", "Carrera T", "Carrera S", "Carrera 4", "Carrera 4S",
              "Carrera GTS", "Carrera 4 GTS", "Targa 4", "Targa 4S",
              "Turbo", "Turbo S", "GT3", "GT3 Touring", "GT3 RS"],
    "992.2": ["Carrera", "Carrera T", "Carrera S", "Carrera 4", "Carrera 4S",
              "Carrera GTS", "Carrera 4 GTS", "Targa 4", "Targa 4S",
              "Turbo", "Turbo S", "GT3", "GT3 Touring", "GT3 RS"],
}

# Body styles each generation offered.
AVAILABLE_BODIES = {
    "901":   ["coupe", "targa"],
    "G":     ["coupe", "cabriolet", "targa", "speedster"],
    "964":   ["coupe", "cabriolet", "targa", "speedster"],
    "993":   ["coupe", "cabriolet", "targa"],
    "996.1": ["coupe", "cabriolet"],
    "996.2": ["coupe", "cabriolet", "targa"],
    "997.1": ["coupe", "cabriolet", "targa"],
    "997.2": ["coupe", "cabriolet", "targa", "speedster"],
    "991.1": ["coupe", "cabriolet", "targa"],
    "991.2": ["coupe", "cabriolet", "targa", "speedster"],
    "992.1": ["coupe", "cabriolet", "targa"],
    "992.2": ["coupe", "cabriolet", "targa"],
}

# Canonical variant order for a stable UI (superset used when All is selected).
_VARIANT_ORDER = [
    "Carrera", "Carrera T", "Carrera S", "Carrera 4", "Carrera 4S",
    "Carrera GTS", "Carrera 4 GTS", "Targa", "Targa 4", "Targa 4S",
    "Turbo", "Turbo S", "GT3", "GT3 Touring", "GT3 RS", "GT2", "GT2 RS",
    "Speedster",
]

ALL_VARIANTS = [v for v in _VARIANT_ORDER
                if any(v in vs for vs in AVAILABLE_VARIANTS.values())]

BODY_STYLES = ["coupe", "cabriolet", "targa", "speedster"]
TRANSMISSIONS = ["manual", "pdk", "tiptronic", "automatic"]
DRIVETRAINS = ["rwd", "awd"]

# Options the UI can filter on. These are only ever matched from confirmed,
# structured data (never guessed from a free-text description), so a car with no
# structured option data stays "unknown" for every one of these.
FILTERABLE_OPTIONS = [
    ("sport_chrono", "Sport Chrono"),
    ("sport_exhaust", "Sport Exhaust (PSE)"),
    ("ccb", "Carbon-ceramic brakes (PCCB)"),
    ("sport_seats", "Sport seats"),
]

# Operator-managed acquisition pipeline stages (Task 2G). These are set by the
# operator, never inferred. 'unvalued'/'preliminary'/'underwritten' are derived
# from the valuation engine and handled separately.
DEAL_STAGES = [
    "needs_inspection", "contacted_seller", "offer_submitted", "acquired",
    "reconditioning", "listed_for_resale", "sold",
]


def normalize_generation(gen: str | None) -> list[str]:
    """Expand a family ('997') to its members; pass a specific gen through."""
    return _gens.expand(gen)


def variants_for(generations: list[str] | None) -> list[str]:
    """Union of variants available for the given generations, in canonical order.

    ``None`` or empty means "all generations" -> the full variant list.
    """
    if not generations:
        return list(ALL_VARIANTS)
    wanted = set()
    for g in generations:
        for member in normalize_generation(g) or [g]:
            wanted.update(AVAILABLE_VARIANTS.get(member, []))
    return [v for v in _VARIANT_ORDER if v in wanted]


def bodies_for(generations: list[str] | None) -> list[str]:
    if not generations:
        return list(BODY_STYLES)
    wanted = set()
    for g in generations:
        for member in normalize_generation(g) or [g]:
            wanted.update(AVAILABLE_BODIES.get(member, []))
    return [b for b in BODY_STYLES if b in wanted]


def variant_exists(generation: str | None, variant: str | None) -> bool:
    """Does this variant genuinely exist for this generation? Unknown -> True.

    Missing information is never treated as a contradiction: if either side is
    unknown we do not claim the combination is impossible.
    """
    if not generation or not variant:
        return True
    members = normalize_generation(generation) or [generation]
    return any(variant in AVAILABLE_VARIANTS.get(m, []) for m in members)


def generation_options() -> list[dict]:
    """UI-ready generation options grouped for the filter panel."""
    out = []
    for label, gens in GENERATION_GROUPS:
        out.append({
            "group": label,
            "generations": [
                {"key": g, "label": GENERATION_LABELS.get(g, g)} for g in gens
            ],
        })
    return out

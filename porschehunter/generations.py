"""911 generation reference and variant normalisation.

Model-year ranges are US-market and are reference data, not market data.
Launch years overlap in the real world (the 991 and the 997.2 were both sold
as 2012 cars, the 964 and G-series both as 1989 cars), so `from_year` returns
the dominant generation for a year and flags the ambiguous ones. A VIN decode
always wins over a year-only guess.
"""

from __future__ import annotations

# (generation, first_year, last_year, note)
GENERATIONS = [
    ("901",   1964, 1973, "Original long-hood cars."),
    ("G",     1974, 1989, "Impact-bumper G-series, including 3.2 Carrera."),
    ("964",   1989, 1994, "1989 overlaps the G-series."),
    ("993",   1995, 1998, "Last air-cooled."),
    ("996.1", 1999, 2001, "US 996 Carrera from MY1999."),
    ("996.2", 2002, 2004, "Facelift; 3.6 engine."),
    ("997.1", 2005, 2008, "IMS through MY2008 non-DFI cars."),
    ("997.2", 2009, 2012, "DFI + PDK. 2012 overlaps the 991."),
    ("991.1", 2012, 2016, "2012 overlaps the 997.2."),
    ("991.2", 2017, 2019, "Turbocharged Carrera."),
    ("992.1", 2020, 2024, ""),
    ("992.2", 2025, 2030, "Range end is a forward placeholder, not a fact."),
]

AMBIGUOUS_YEARS = {1989: ("G", "964"), 2012: ("997.2", "991.1")}

# Coarse families, used when a listing only says "997" or "991".
FAMILIES = {
    "996": ("996.1", "996.2"),
    "997": ("997.1", "997.2"),
    "991": ("991.1", "991.2"),
    "992": ("992.1", "992.2"),
}

VALID = {g for g, *_ in GENERATIONS} | set(FAMILIES)


def from_year(year: int | None) -> tuple[str | None, bool]:
    """Return (generation, ambiguous). Ambiguous years pick the earlier gen."""
    if year is None:
        return None, False
    if year in AMBIGUOUS_YEARS:
        return AMBIGUOUS_YEARS[year][0], True
    for gen, lo, hi, _ in GENERATIONS:
        if lo <= year <= hi:
            return gen, False
    return None, False


def family(generation: str | None) -> str | None:
    """'997.1' -> '997'. Passes through a family unchanged."""
    if not generation:
        return None
    g = generation.strip()
    if g in FAMILIES:
        return g
    return g.split(".")[0] if "." in g else g


def expand(generation: str | None) -> list[str]:
    """'997' -> ['997.1','997.2']; '997.1' -> ['997.1']."""
    if not generation:
        return []
    g = generation.strip()
    return list(FAMILIES.get(g, (g,)))


def note(generation: str) -> str:
    for gen, _lo, _hi, n in GENERATIONS:
        if gen == generation:
            return n
    return ""


# --- Variant normalisation --------------------------------------------------
# Order matters: the most specific string must be tested first.
_VARIANT_PATTERNS = [
    ("GT2 RS", ["gt2 rs", "gt2rs"]),
    ("GT3 RS", ["gt3 rs", "gt3rs"]),
    ("GT2", ["gt2"]),
    ("GT3 Touring", ["gt3 touring"]),
    ("GT3", ["gt3"]),
    ("Turbo S", ["turbo s"]),
    ("Turbo", ["turbo"]),
    # Targa variants MUST be tested before the Carrera "4s"/"4" bare needles, or
    # a "Targa 4S" matches "Carrera 4S" via the bare "4s".
    ("Targa 4S", ["targa 4s"]),
    ("Targa 4", ["targa 4"]),
    ("Targa", ["targa"]),
    ("Carrera 4S", ["carrera 4s", "c4s", "4s"]),
    ("Carrera 4 GTS", ["carrera 4 gts", "c4 gts"]),
    ("Carrera GTS", ["carrera gts", "gts"]),
    ("Carrera 4", ["carrera 4", "c4"]),
    ("Carrera S", ["carrera s", "c2s"]),
    ("Speedster", ["speedster"]),
    ("Carrera T", ["carrera t"]),
    ("Carrera", ["carrera", "c2"]),
]

_BODY_PATTERNS = [
    ("cabriolet", ["cabriolet", "cabrio", "convertible"]),
    ("targa", ["targa"]),
    ("coupe", ["coupe", "coupé"]),
]

_TRANS_PATTERNS = [
    ("pdk", ["pdk", "doppelkupplung"]),
    ("tiptronic", ["tiptronic", "tip "]),
    ("manual", ["manual", "6-speed manual", "7-speed manual", "6mt", "7mt", " mt", "stick"]),
    ("automatic", ["automatic"]),
]


def _match(text: str, patterns) -> str | None:
    t = f" {text.lower()} "
    for label, needles in patterns:
        for n in needles:
            if n in t:
                return label
    return None


import re as _re

_WORD_CACHE: dict[str, "_re.Pattern"] = {}


def _match_word(text: str, patterns) -> str | None:
    """Like _match but matches each needle on word boundaries, so 'Turbo' does
    NOT match 'twin-turbocharged' (a 991.2/992 Carrera is turbocharged but is a
    Carrera, not a Turbo). Multi-word needles keep working."""
    low = text.lower()
    for label, needles in patterns:
        for n in needles:
            rx = _WORD_CACHE.get(n)
            if rx is None:
                rx = _WORD_CACHE[n] = _re.compile(r"(?<!\w)" + _re.escape(n.strip()) + r"(?!\w)")
            if rx.search(low):
                return label
    return None


def normalize_variant(text: str | None) -> str | None:
    return _match_word(text, _VARIANT_PATTERNS) if text else None


def normalize_body_style(text: str | None) -> str | None:
    return _match(text, _BODY_PATTERNS) if text else None


def normalize_transmission(text: str | None) -> str | None:
    return _match(text, _TRANS_PATTERNS) if text else None


def parse_year(text: str | None) -> int | None:
    """Pull a plausible model year out of a listing title."""
    if not text:
        return None
    import re
    for m in re.finditer(r"\b(19[6-9]\d|20[0-3]\d)\b", text):
        y = int(m.group(1))
        if 1963 <= y <= 2031:
            return y
    return None

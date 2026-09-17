"""Rough US distance estimates for transport costing.

These are approximate geographic centroids of each state, used only to turn
"the car is in Oregon, my friend is in Ohio" into a mileage figure good enough
for a first screen. They are geography, not market data, but they are COARSE:
a centroid can be several hundred miles from the actual seller.

Always replace the estimate with a real broker quote before committing money.
Pass an explicit --transport-quote on a deal to do exactly that.
"""

from __future__ import annotations

import math

# state -> (lat, lon) approximate centroid
STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.0, -152.0), "AZ": (34.3, -111.7),
    "AR": (34.9, -92.4), "CA": (37.2, -119.5), "CO": (39.0, -105.5),
    "CT": (41.6, -72.7), "DE": (39.0, -75.5), "DC": (38.9, -77.0),
    "FL": (28.6, -82.4), "GA": (32.6, -83.4), "HI": (20.3, -156.4),
    "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3),
    "IA": (42.1, -93.5), "KS": (38.5, -98.4), "KY": (37.5, -85.3),
    "LA": (31.1, -92.0), "ME": (45.4, -69.2), "MD": (39.0, -76.8),
    "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3),
    "MS": (32.7, -89.7), "MO": (38.4, -92.5), "MT": (47.0, -109.6),
    "NE": (41.5, -99.8), "NV": (39.3, -116.6), "NH": (43.7, -71.6),
    "NJ": (40.2, -74.7), "NM": (34.4, -106.1), "NY": (42.9, -75.5),
    "NC": (35.5, -79.4), "ND": (47.4, -100.5), "OH": (40.3, -82.8),
    "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8),
    "RI": (41.7, -71.6), "SC": (33.9, -80.9), "SD": (44.4, -100.2),
    "TN": (35.8, -86.4), "TX": (31.5, -99.3), "UT": (39.3, -111.7),
    "VT": (44.1, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.4),
    "WV": (38.6, -80.6), "WI": (44.6, -89.7), "WY": (43.0, -107.6),
}

# Straight-line miles under-state real driving distance.
ROAD_FACTOR = 1.22


def great_circle_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def estimate_road_miles(from_state: str | None, to_state: str | None) -> tuple[float | None, str]:
    """Returns (miles, basis). miles is None when either end is unknown."""
    a = STATE_CENTROIDS.get((from_state or "").strip().upper())
    b = STATE_CENTROIDS.get((to_state or "").strip().upper())
    if a is None or b is None:
        missing = "seller state" if a is None else "destination state"
        return None, f"unknown {missing}"
    if a == b:
        return 150.0 * ROAD_FACTOR, "same state -- nominal in-state distance"
    miles = great_circle_miles(a, b) * ROAD_FACTOR
    return miles, (f"approx. state-centroid distance {from_state.upper()}->{to_state.upper()} "
                   f"x {ROAD_FACTOR} road factor")

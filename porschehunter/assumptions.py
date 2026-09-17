"""Central register of every tunable number the deal model uses.

Rule for this project: no number that affects a profit figure may be hidden
inside a formula. It lives here, with a stated basis and a `verified` flag.

`verified=False` means: this is a placeholder the operator has not yet
confirmed against their own invoices, quotes or transaction records. The
dashboard and CLI both surface unverified assumptions explicitly.

Nothing here is a market observation. Market values come only from `comps`,
which are documented completed sales.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Assumption:
    key: str
    value: float
    unit: str
    basis: str
    verified: bool = False
    # 'unset' means there is no honest default. The model will not treat the
    # shipped value as a real figure, and a deal cannot be fully underwritten
    # until the operator supplies one.
    status: str = "placeholder"

    @property
    def label(self) -> str:
        return {"verified": "VERIFIED",
                "unset": "NOT SET -- YOU MUST SUPPLY THIS",
                "placeholder": "UNVERIFIED PLACEHOLDER"}[self.status]


# --- Profit target ----------------------------------------------------------
DEFAULTS = [
    Assumption(
        "target_net_profit", 8000.0, "USD",
        "Operator-stated minimum net profit per vehicle.",
        verified=True,
    ),

    # --- Reconditioning -----------------------------------------------------
    Assumption(
        "repairs_996", 4500.0, "USD",
        "Placeholder reconditioning budget for a 996 bought sight-unseen. "
        "Replace with your friend's actual shop rate and a per-car estimate. "
        "996/997.1 carry known IMS/RMS and bore-scoring exposure that this "
        "flat number does not price.",
    ),
    Assumption(
        "repairs_997", 4500.0, "USD",
        "Placeholder reconditioning budget for a 997. Replace with a real quote.",
    ),
    Assumption(
        "repairs_991", 3500.0, "USD",
        "Placeholder reconditioning budget for a 991. Replace with a real quote.",
    ),
    Assumption(
        "repairs_default", 5000.0, "USD",
        "Placeholder reconditioning budget for any other 911 generation.",
    ),
    Assumption(
        "detail_and_photography", 900.0, "USD",
        "Placeholder for paint correction, full detail and a listing photo set. "
        "Replace with what your friend actually charges.",
    ),

    # --- Transport ----------------------------------------------------------
    Assumption(
        "transport_base", 350.0, "USD",
        "Placeholder fixed component of an enclosed-carrier booking. "
        "Replace with a real broker quote.",
    ),
    Assumption(
        "transport_per_mile", 0.85, "USD/mile",
        "Placeholder enclosed-transport rate per road mile. "
        "Replace with real broker quotes; rates vary by lane and season.",
    ),
    Assumption(
        "transport_fallback_miles", 1500.0, "miles",
        "Distance used when the seller's location is unknown, so an unknown "
        "location never looks cheaper than a known one.",
    ),

    # --- Acquisition-side transaction costs ---------------------------------
    # These apply on the BUY side and are easy to forget. Several are
    # conditional: they only appear when the listing says they should.
    Assumption(
        "ppi_cost", 450.0, "USD",
        "Placeholder pre-purchase inspection at an independent Porsche shop in "
        "the seller's area. Applied to EVERY car -- buying a 911 sight-unseen "
        "without a PPI is the single most expensive mistake available here.",
    ),
    Assumption(
        "purchase_tax_pct", 0.0, "fraction of purchase price",
        "Transaction/use tax on the PURCHASE. THIS HAS NO DEFAULT. A 0% rate "
        "assumes a dealer or resale exemption, which is a claim about YOUR tax "
        "position that this tool cannot make for you -- and it is material: at "
        "6.5% a $40,000 car carries $2,600 of tax. Until you set it, every "
        "profit figure is PRELIMINARY and stated as 'before purchase tax'. "
        "Set it either way: 0 if you have a confirmed exemption, or your "
        "actual combined rate.",
        status="unset",
    ),
    Assumption(
        "dealer_doc_fee", 500.0, "USD",
        "Dealer documentation/processing fee, applied only when the seller is "
        "a dealer. Caps vary widely by state. Placeholder.",
    ),
    Assumption(
        "auction_buyer_premium_pct", 0.05, "fraction of hammer",
        "Buyer premium you pay when the listing is an auction, applied only to "
        "auction listings. Check the venue's published rate and any cap.",
    ),

    # --- Selling costs ------------------------------------------------------
    Assumption(
        "sale_fee_pct", 0.05, "fraction of resale",
        "Placeholder blended seller-side fee (auction commission, marketplace "
        "fee or consignment). Replace per intended sales channel.",
    ),
    Assumption(
        "sale_fee_cap", 5000.0, "USD",
        "Placeholder cap on the percentage seller fee.",
    ),
    Assumption(
        "title_and_admin", 400.0, "USD",
        "Placeholder title, temporary registration, notary and shipping-of-docs cost.",
    ),

    # --- Carrying -----------------------------------------------------------
    Assumption(
        "days_to_sell", 60.0, "days",
        "Placeholder holding period from purchase to funds received. "
        "Replace once you have your own sell-through history.",
    ),
    Assumption(
        "carrying_cost_per_day", 12.0, "USD/day",
        "Placeholder storage + insurance + floorplan interest per day.",
    ),

    # --- Risk ---------------------------------------------------------------
    Assumption(
        "risk_reserve_pct", 0.06, "fraction of purchase price",
        "Placeholder reserve for undisclosed faults found on arrival. "
        "Scales with purchase price because engine-out work on a more "
        "expensive car costs more.",
    ),
    Assumption(
        "resale_haircut_pct", 0.03, "fraction of comp value",
        "Placeholder discount applied to the comp-derived value to reflect "
        "that you are a seller who wants a timely sale, not the top of market.",
    ),

    # --- Valuation model ----------------------------------------------------
    Assumption(
        "mileage_adjust_per_mile", 0.22, "USD/mile",
        "Placeholder value change per odometer mile when adjusting a comp to "
        "the subject car. This is a crude linear stand-in for a curve that is "
        "not linear. Calibrate from your own comp set before trusting it.",
    ),
    Assumption(
        "mileage_adjust_cap", 12000.0, "USD",
        "Cap on the absolute mileage adjustment applied to any single comp, "
        "so a far-off comp cannot dominate the estimate.",
    ),
    Assumption(
        "comp_max_age_days", 365.0, "days",
        "Comps older than this are excluded. Collector-market prices move.",
    ),
    Assumption(
        "comp_mileage_window", 40000.0, "miles",
        "Comps outside this odometer band from the subject car are excluded.",
    ),
    Assumption(
        "comp_year_window", 3.0, "model years",
        "Comps outside this model-year band from the subject car are excluded.",
    ),
    Assumption(
        "market_trend_pct_per_year", 0.0, "fraction/year",
        "Time-adjustment applied to older comps. Default 0 = no adjustment, "
        "because this platform has no verified market index. Set it only if "
        "you have a documented index for the specific generation.",
        verified=True,  # verified in the sense that 0 = deliberately no adjustment
    ),
    Assumption(
        "min_comps_low_confidence", 3.0, "count",
        "Fewest comps that will produce any estimate at all.",
    ),
    Assumption(
        "min_comps_medium_confidence", 5.0, "count",
        "Comps needed before the estimate is labelled medium confidence.",
    ),
    Assumption(
        "min_comps_high_confidence", 8.0, "count",
        "Comps needed before the estimate is labelled high confidence.",
    ),
    Assumption(
        "buyer_premium_pct", 0.05, "fraction of hammer",
        "Buyer premium added to auction comps recorded as hammer-only. "
        "Check the actual venue's published rate before relying on this.",
    ),
]

# Keep `status` consistent with `verified` for everything that is not 'unset'.
DEFAULTS = [
    a if a.status == "unset"
    else Assumption(a.key, a.value, a.unit, a.basis, a.verified,
                    "verified" if a.verified else "placeholder")
    for a in DEFAULTS
]

BY_KEY = {a.key: a for a in DEFAULTS}

UNSET_KEYS = [a.key for a in DEFAULTS if a.status == "unset"]


def default_value(key: str) -> float:
    return BY_KEY[key].value


def unverified_keys() -> list[str]:
    return [a.key for a in DEFAULTS if not a.verified]

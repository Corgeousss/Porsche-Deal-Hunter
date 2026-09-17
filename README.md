# Porsche 911 Deal Hunter

An internal screening tool for buying undervalued 911s, reconditioning them and
reselling them at a minimum projected net profit of **$8,000 per car**.

Python 3.11+, standard library only. No dependencies, no build step.

## What it does

1. Ingests real 911 listings from sources this tool is actually allowed to read,
   plus manual URL entry for the marketplaces that have no authorized access.
2. Stores specs, price, mileage, VIN, photos, seller location, listing URL and
   **price history** in SQLite.
3. Values each car from **documented completed sales you have recorded**, with
   mileage and buyer-premium adjustments.
4. Projects resale, repairs, transport, fees, carrying cost, a risk reserve and
   net profit — then flags anything clearing $8,000 and states your maximum bid.
5. Serves a plain dashboard with the numbers, the comps behind them and the
   source links.

> **Status: no connector has yet completed a live call to a real endpoint.**
> Every network path is implemented and unit-tested against in-memory payloads,
> never run against a live service. See **[AUDIT.md](AUDIT.md)** for the
> component-by-component breakdown, and run `validate` on a networked machine
> to change that.

## The three rules this codebase enforces in code

**1. No invented vehicles, prices or comparables.**
A comp requires a real `http(s)` URL, a real sale date and a positive price, or
`comps add` rejects it. With fewer than 3 matching comps the valuation engine
returns `insufficient_comps` and **no number at all** — it does not estimate
from thin data. Test fixtures are marked `is_synthetic=1` and are excluded from
every production query.

**2. An asking price is never a sale price.**
Every comp declares a `price_basis`. Only `verified_transaction` reaches a
valuation. CLASSIC.COM's "Last Asking" figures, MarketCheck's Past Inventory
removals and every dealer listing are stored, labelled and excluded. Every comp
also declares a `permission_basis`; records with `unknown` are excluded, so the
tool cannot quietly accumulate someone else's data.

**3. No bypassing marketplace access restrictions.**
Cars & Bids is registered as `prohibited` and is never fetched. Every HTTP
request checks `robots.txt` first, uses one honest User-Agent, waits 5 seconds
between requests to a host, and only reads JSON-LD that a site publishes
deliberately. There is no login handling, no CAPTCHA handling and no proxy
rotation anywhere in this repository.

See **[DATA_SOURCES.md](DATA_SOURCES.md)** for the source-by-source assessment,
**[AUDIT.md](AUDIT.md)** for what is genuinely working versus merely written,
and **[RUNBOOK.md](RUNBOOK.md)** for exact commands.

## Quick start

```bash
python3 -m porschehunter init
python3 -m porschehunter sources          # what this tool may and may not read

# 1. Record documented completed sales. This is the part that makes it work.
python3 -m porschehunter comps add \
    --generation 997.1 --variant "Carrera S" --year 2008 \
    --mileage 48000 --transmission manual --body coupe \
    --price 62500 --date 2026-07-14 \
    --venue bring_a_trailer --url https://bringatrailer.com/listing/<the-real-one>/

python3 -m porschehunter comps coverage   # how much you can actually value

# 2. Add listings you are looking at.
python3 -m porschehunter add https://www.facebook.com/marketplace/item/<id> \
    --title "2008 Porsche 911 Carrera S" --mileage 51000 --price 39500 \
    --vin WP0AB29958S7xxxxx --state CA --city Fresno --seller-type private

# 3. Replace placeholder costs with real quotes for a specific car.
python3 -m porschehunter override 1 --repairs 1800 --transport 1100 \
    --note "Mike's quote 2026-09-17; enclosed carrier quote from Reliable"

# 4. Score everything. --destination is your friend's state.
python3 -m porschehunter scan --destination OH

# 5. Dashboard
python3 -m porschehunter serve --destination OH   # http://127.0.0.1:8000
```

To see the dashboard layout before you have real data:
`python3 -m porschehunter serve --destination OH --preview-synthetic`
(renders a red banner; synthetic figures, never for a buying decision).

## Commands

| Command | Purpose |
|---|---|
| `init` | Create the database and the comp-import template. |
| `sources` | Source access status, cost and limitations. |
| `status` | Refresh timestamps, run history, missing-data counts. |
| `enable <src> [--off]` | Turn a connector on. Refuses sources whose terms prohibit it. |
| `add <url> [...]` | Manual listing entry. Routes the URL to the right marketplace. |
| `fetch <src>` | Run an automated connector (`dealer_jsonld`, `craigslist_rss`, `marketcheck`). |
| `comps add \| import \| coverage` | Record and inspect documented completed sales. |
| `value <id>` | Full valuation JSON for one listing, including every comp used. |
| `scan --destination XX` | Score all active listings, flag opportunities. |
| `listings` | What is in the database and what is missing from it. |
| `override <id> [...]` | Record real per-car repair/transport/days quotes. Replaces placeholders. |
| `vin <VIN>` | Decode via NHTSA vPIC (free, no key); offline checks if unreachable. |
| `assumptions [--set K V]` | List or change every cost assumption. |
| `validate [--listing-url U]` | Live real-data validation. The only thing that can mark a source live-verified. |
| `acceptance <id> --destination XX` | Full underwriting report for one car, or INSUFFICIENT DATA with the exact gap. |
| `classic-com init-config \| status` | CLASSIC.COM licensed adapter setup. |
| `sheet --out FILE` | Export the mechanic cost input sheet (.html or .csv). |
| `serve --destination XX` | Dashboard on 127.0.0.1:8000. |

## The money model

```
expected_resale = comp_value × (1 − resale_haircut)

costs(P) = P + repairs + detailing + transport + title/admin
             + sale_fee(expected_resale)
             + carrying(days × per_day)
             + risk_reserve(risk_pct × P)

net(P)   = expected_resale − costs(P)
```

Both the purchase price and the risk reserve scale with `P`, so the maximum you
can pay and still clear the target is:

```
P_max = (expected_resale − fixed_costs − target) / (1 + risk_pct)
```

A unit test asserts that buying at `P_max` yields exactly the $8,000 target.

## Assumptions — read this before trusting a profit figure

Every number that is not a documented sale lives in `porschehunter/assumptions.py`
with a stated basis and a `verified` flag. **24 of 26 ship as UNVERIFIED
placeholders**, including every repair budget, the transport rate, the sale fee,
the carrying cost and the $/mile mileage adjustment. They are there so the model
runs, not because they are right.

```bash
python3 -m porschehunter assumptions
python3 -m porschehunter assumptions --set repairs_997 3200 \
    --basis "Mike's quote, 2026-09: full service + IMS on a 997.1" --verified
```

The dashboard, the CLI and every explanation string label unverified inputs
explicitly. Replace them with your own quotes before acting on a number.

### What the valuation engine does not know

Options, service history, accident history, paint condition, colour
desirability, matching-numbers engines, PPI outcomes. On a 911 these routinely
move price by more than the mileage adjustment being applied. **This is a screen
that tells you which cars are worth a phone call — not an appraisal.**

## Configuration files

| File | Purpose |
|---|---|
| `data/allowed_domains.txt` | Dealer domains you have opted in for JSON-LD reading. Empty by default. |
| `data/feeds.txt` | RSS saved-search feeds. Empty by default. |
| `data/comps_template.csv` | Header-only CSV for bulk comp import. No sample rows, by design. |
| `MARKETCHECK_API_KEY` (env) | Enables the MarketCheck connector. |
| `PORSCHE_DB` (env) | Database path. Default `data/porsche.db`. |

## Tests

```bash
python3 -m unittest discover -s tests -v    # 98 tests
```

Covers the profit identity, the max-bid solve, mileage-adjustment direction and
capping, buyer-premium handling, stale-comp exclusion, the comp validation
guards, the synthetic-data firewall, VIN check-digit math, generation mapping,
JSON-LD parsing, RSS parsing and price-history tracking.

Every vehicle and sale in the test suite is synthetic, marked as such, and
cannot reach a real valuation.

## Known limitations

- **Comps must be entered by hand.** No authorized bulk source for 911
  sold prices exists at this volume. This is the main cost of using the tool.
- **Transport distance is a state-centroid estimate**, not a route. Record the
  real number with `override <id> --transport <usd>` once you have a broker quote.
- **No geocoding, no route API** — both would need a paid key.
- **The MarketCheck connector is untested against a live key.** Verify endpoint
  paths against your own plan's documentation.
- **Generation inference from model year alone is ambiguous** for 1989 and 2012.
  The tool says so rather than guessing silently.

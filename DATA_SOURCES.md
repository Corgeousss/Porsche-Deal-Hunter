# Data source assessment

What each source actually offers, what access requires, and what this platform
is therefore allowed to do with it.

> **Corrections applied 2026-09-17.** An earlier revision of this file wrongly
> stated that CLASSIC.COM had no API, and recommended hand-aggregating Bring a
> Trailer results. Both were wrong and have been corrected below. The
> superseded claims are listed at the end so the error is on the record.

## How these findings were verified

This repository was built inside a sandbox whose network egress is restricted
to package registries. **No host in this document could be opened directly from
the build environment**, including the two documentation URLs cited below.
Claims are tagged:

| Tag | Meaning |
|---|---|
| **[OPERATOR]** | Supplied directly by the platform owner. |
| **[SEARCHED]** | From web search results returned during this session. |
| **[UNVERIFIED]** | Not confirmed against the primary page. **Confirm before relying on it.** |

**No pricing figure appears in this document**, because I have not seen one
from a primary source. Do not let any number in this repository stand in for a
quote.

---

## Summary

| Source | Authorized programmatic access | Status in this platform |
|---|---|---|
| **CLASSIC.COM** | **Yes — official licensed third-party API.** Taxonomy, sales history, comparable sales. Negotiated via datasupport@classic.com | **Adapter written, authentication disabled** until licensed credentials + endpoint config are supplied |
| **MarketCheck** | Yes — commercial API. Dealer, private-party, auction and past inventory | **Client written, off** until a key is supplied. Several endpoint paths unconfirmed |
| **NHTSA vPIC** | Yes — public REST API, no key | **Connector written. Never yet called live** (see AUDIT.md) |
| **Dealer sites w/ schema.org JSON-LD** | Publicly published structured data; per-site terms apply | Connector written, opt-in per domain, robots.txt enforced. **Never yet called live** |
| **Craigslist** | RSS on search pages; terms prohibit scrapers and bulk harvesting | Connector written, **off by default** |
| **Bring a Trailer** | No public API, no data-licensing product | **Manual reference only.** See the warning below |
| **Cars & Bids** | **No** — terms prohibit extraction and aggregation | **Blocked in code.** Never fetched |
| **Facebook Marketplace** | **No** public listing API | Manual URL entry only |

---

## 1. CLASSIC.COM — the correct primary route for comps

**[OPERATOR]** CLASSIC.COM operates an **official API for licensed third
parties**, documented at <https://support.classic.com/classic.com-api>, with
**taxonomy, sales history and comparable-sales functionality**. Access is
**negotiated directly with `datasupport@classic.com`** — it is not self-serve
and there is no public signup.

This makes CLASSIC.COM the correct primary route for the comparable-sales data
this platform is built around. It is the one source that offers verified
completed-sale prices and comps under a licence, as a product.

### The mapping decision that matters

**[SEARCHED]** CLASSIC.COM's historical database preserves both completed sales
and *removed listings*. Where no sold price was provided, it retains the
**"Last Asking"** price — the final price the car was listed at. Their vehicle
History reports draw on auction records, dealer records and public records.

**A last-asking price is not a transaction.** So the adapter
(`porschehunter/sources/classic_com.py`) requires the licensed response schema
to state *which field proves a completed sale*, and **fails closed**: any
record it cannot prove is a completed sale is stored as `price_basis='last_asking'`
and is excluded from every valuation. There is a unit test for exactly this.

### What to ask for, and what to confirm

**Smallest licence bundle that supports this tool.** The tool needs, minimally:

1. **Verified completed-sale prices, per vehicle**, for 996/997/991 911s —
   individual transactions with sale price, sale date, mileage and a source
   URL. Aggregates and indices are not enough: the valuation engine adjusts
   each comp individually for mileage, so it needs the rows, not the averages.
2. **A comparable-sales lookup** keyed on generation/variant/year/mileage, if
   offered separately from raw sales history.
3. **Taxonomy** sufficient to resolve 911 generation and variant, so their
   model identifiers can be mapped onto this tool's generations.

Everything else — active listing feeds, market charts, embeds, dealer-facing
analytics — is outside what this tool needs. Ask them to quote the narrowest
bundle covering points 1–3.

**Commercial rights to confirm in writing**, because they change whether this
tool is usable at all. The adapter records your answers in the `licence` block
of `data/classic_com_api.json`:

| Question | Why it matters here |
|---|---|
| Is internal business use permitted? | This is an internal investment tool, not a consumer product. |
| May data be **stored locally**, and for how long? | The entire architecture is a local SQLite cache of comps. A no-retention licence would require a redesign. |
| May figures be **displayed to third parties**? | Do you show a comp-backed valuation to a seller, a partner, or your mechanic? |
| Is **attribution** required, and in what form? | Affects the dashboard and any exported report. |
| Are derived values (your valuation output) permitted? | The whole point is producing a number *derived* from their comps. |
| Rate limits and call volume | At your volume this is unlikely to bind, but confirm it. |
| Pricing and minimum term | **Do not assume.** Ask. |

**Status in code:** adapter written, **authentication disabled**. It refuses to
run until (a) `CLASSIC_COM_API_KEY` is set and (b) `data/classic_com_api.json`
is filled in from the licensed docs and marked `"confirmed": true`. It will not
guess an endpoint path. Start with `porschehunter classic-com init-config`.

## 2. MarketCheck

**[SEARCHED]** MarketCheck's Cars API (<https://docs.marketcheck.com/docs/api/cars>)
documents considerably more than dealer inventory:

- **Active dealer listings** — `GET /v2/search/car/active`
- **Private Party Inventory Search** — private-party listings
- **Auction Inventory Search** — auction listings
- **Past Inventory Search** — sold, expired and removed listings.
  **Dealer inventory only, US and Canada. Does not cover private party or auction.**
- **History by VIN** — past listings, price changes, mileage changes, seller info
- **VIN decode** — `GET /v2/decode/car/neovin/{vin}/specs`
- **Price prediction / comparables** — `GET /v2/predict/car/us/marketcheck_price/comparables`

This corrects the earlier claim that MarketCheck was dealer-only. Private-party
and auction inventory are documented products.

### The past-inventory trap

**Past Inventory contains removals, not confirmed transactions.** A listing
leaving a dealer feed may have sold, been withdrawn, been traded, or been
relisted elsewhere — and the last price observed is the last *asking* price.

**[SEARCHED]** MarketCheck's own price prediction is described as trained on
sold-listing data across dealerships; that is a **model output**, not a
transaction record for a specific car.

So: inferred dealer sales are **not** treated as verified transaction prices
anywhere in this platform. The client imports past-inventory records with
`price_basis='inferred_from_removal'`, which the valuation engine excludes.

### What to ask for, and what to confirm

**Smallest bundle for this tool:**
1. **Active listing search** — dealer, *plus private-party and auction* if
   priced sensibly, since private-party cars are where underpricing tends to sit.
2. **A genuine transaction-price product, if one exists.** Ask them directly:
   *"Which endpoint, if any, returns a price a buyer actually paid for a
   specific VIN — as opposed to the last asking price of a removed listing?"*
   The answer determines whether MarketCheck can feed comps at all, or only
   listings.
3. **VIN decode** only if you want it in place of the free NHTSA vPIC decoder.

**Rights to confirm:** internal use, local storage and retention, whether
derived valuations may be shown to third parties, attribution, rate limits,
call allowance and overage, and pricing. **Do not assume any of it.**

**Status in code:** client written, off until `MARKETCHECK_API_KEY` is set.
The `active` and VIN-decode paths are documented literally; `private_party`,
`auction`, `past` and `vin_history` are documented **by name** but their exact
paths are **unconfirmed**, so the client raises `EndpointNotConfirmed` rather
than calling a guessed URL. Fill them in from your plan's docs.

## 3. NHTSA vPIC

**[SEARCHED]** `https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{VIN}?format=json`
— free, no API key, no registration, batch decoding supported.

Gives make, model, model year, body class, engine displacement and cylinders,
plant. Does **not** give options, condition, mileage, price, or reliable
Porsche trim resolution.

**Status:** implemented, with results cached and timestamped, and an offline
fallback that runs structural checks only. **It has never been called live from
this environment** — see AUDIT.md.

## 4. Dealer websites publishing schema.org Vehicle data

Dealer platforms commonly embed a `schema.org/Vehicle` JSON-LD block for search
engines, carrying VIN, mileage, price, colour, transmission and dealer address.

Guardrails in code: per-domain operator allowlist (`data/allowed_domains.txt`,
empty by default), robots.txt checked on every request, one honest User-Agent,
5-second per-host floor, no cookies, no login, no CAPTCHA handling, no proxy
rotation, and **no HTML-scraping fallback** — if there is no JSON-LD, it stops.

**A dealer's asking price is an asking price.** Nothing ingested here is a
transaction, and none of it becomes a comp.

## 5. Bring a Trailer — corrected guidance

**[SEARCHED]** No public API and no data-licensing product. The third-party
"BaT scrapers" on Apify and RapidAPI are unaffiliated services and are not
integrated here.

> **Corrected.** An earlier revision of this file recommended hand-entering ~30
> BaT results per generation to seed the comp database. **That recommendation
> is withdrawn.** Systematically aggregating another site's auction results into
> your own database is a different act from reading a page as a human, and it is
> not something to do on the assumption that manual entry makes it acceptable.
> It needs permission.

**Current guidance:** use BaT as a human reference. If you want its results in
this database as comps, either obtain permission from BaT, or license the same
underlying transactions through CLASSIC.COM, which aggregates auction results
as a product. If you do obtain permission, record it — the comps table requires
`--permission-basis operator_asserts_permission --permission-note "<how>"`, and
comps without a permission basis are excluded from valuations.

## 6. Cars & Bids — off limits

**[SEARCHED]** Their Terms of Use prohibit manual or automated processes to
extract, aggregate or reproduce site content for commercial purposes,
competitive analysis, AI/ML training, or any unauthorized purpose.

**This platform never fetches carsandbids.com.** The source is registered
`prohibited`; `enable cars_and_bids` refuses; pasting a URL into `add` stores an
operator reference and prints a notice.

## 7. Facebook Marketplace

**[SEARCHED]** No public Marketplace listings API. Meta's commerce APIs are
partner-gated and aimed at businesses posting inventory. Manual URL entry only.

## 8. Craigslist

**[SEARCHED]** Terms prohibit collecting content via robots, spiders, scripts,
scrapers or crawlers, and prohibit non-browser software interoperating with the
site. Craigslist does publish RSS on search pages and endorses RSS readers, but
bulk harvesting via those feeds is not permitted.

**Off by default.** Read the terms and decide for yourself. A feed yields title,
URL, post date and sometimes a price — no VIN, no mileage, no photos.

---

## What this means for the strategy

1. **Comps are the bottleneck, and CLASSIC.COM is the route.** Their licensed
   API is purpose-built for exactly what this tool needs. Opening that
   conversation is the highest-value action available.
2. **Listings and comps are different problems.** MarketCheck is strong on
   *listings* — including private-party and auction, which is where
   underpricing tends to live. Whether it can supply *verified transaction
   prices* is a question to put to them directly.
3. **Asking prices are not sales, anywhere.** CLASSIC.COM's "Last Asking",
   MarketCheck's Past Inventory, and every dealer listing are all asking
   prices. The platform stores them, labels them, and refuses to value from
   them.
4. **Credentials needed:** `CLASSIC_COM_API_KEY` (licensed) and
   `MARKETCHECK_API_KEY` (paid). Nothing else in the platform needs a login.

## Superseded claims

| Earlier claim | Correction |
|---|---|
| "CLASSIC.COM: no documented self-serve public API... data licensing appears to be a direct commercial conversation" | Wrong. An official licensed third-party API exists with taxonomy, sales history and comparable sales, documented at support.classic.com/classic.com-api. |
| "MarketCheck's strength is dealer inventory; private-party cars are largely out of scope" | Wrong. Private Party and Auction Inventory Search are documented products. |
| "BaT: record ~30 by hand per generation — the highest-value thing you can do" | Withdrawn. Aggregating another site's results needs permission; use a licensed source. |
| MarketCheck base host `mc-api.marketcheck.com` | Corrected to `api.marketcheck.com/v2` per the published docs. |
| (absent) | Added: inferred dealer sales and last-asking prices are never treated as verified transaction prices. |

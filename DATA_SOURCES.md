# Data source assessment

Assignment item 1. What each source actually offers, what it costs, and what
this platform is therefore allowed to do with it.

## How these findings were verified

This repository was built inside a sandbox whose network egress is restricted
to package registries. I could not open `marketcheck.com`, `classic.com`,
`bringatrailer.com` or `carsandbids.com` directly from here. Each claim below
is therefore tagged:

| Tag | Meaning |
|---|---|
| **[SEARCHED]** | Established from web search results returned during this session. |
| **[UNVERIFIED]** | Consistent with what I know, but I could not open the primary page to confirm it. **Confirm before spending money or relying on it.** |

No pricing figure below should go into a budget until you have a written quote.

---

## Summary table

| Source | Authorized programmatic access | Cost | Status in this platform |
|---|---|---|---|
| **NHTSA vPIC** | Yes — public REST API, no key, no registration | Free | **Live connector.** VIN decode. |
| **Dealer sites w/ schema.org JSON-LD** | Publicly published structured data, per-site terms apply | Free | **Live connector, opt-in per domain**, robots.txt enforced. |
| **MarketCheck** | Yes — commercial API with a paid key | Paid, quote required | **Client written, off until you supply a key.** |
| **Craigslist** | RSS on search pages; terms prohibit scrapers/bulk harvesting | Free | **Connector written, off by default.** Read the terms and decide. |
| **CLASSIC.COM** | No documented self-serve public API; free market alerts, paid membership for full sold history | Free tier + paid membership | **Manual comp entry.** Best comp source. |
| **Bring a Trailer** | No public API, no data-licensing product | Free to read | **Manual comp entry.** |
| **Cars & Bids** | **No** — terms prohibit extraction/aggregation | Free to read | **Blocked in code.** Never fetched. |
| **Facebook Marketplace** | **No** public listing API | Free to read | **Manual URL entry only.** |

---

## 1. NHTSA vPIC — the one unambiguous win

**[SEARCHED]** `https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{VIN}?format=json`
is free, requires no API key and no registration, supports batch decoding up
to 50 VINs per call, and returns 100+ fields per VIN.

**What it gives you:** make, model, model year, body class, engine
displacement and cylinders, manufacturing plant.

**What it does not give you:** options, condition, mileage, price, service
history, or fine-grained Porsche trim. A vPIC decode will not tell you a 997.1
is a Carrera S rather than a base Carrera with any reliability.

**Implemented in:** `porschehunter/vin.py`. Results are cached in
`vin_decodes` with a timestamp. When the network is unavailable the code falls
back to offline structural checks only (check digit, WMI, model-year
character) and records that it did so — it never fabricates a decode.

## 2. Dealer websites publishing schema.org Vehicle data

Most dealer platforms embed a `<script type="application/ld+json">` block
describing each car, specifically so search engines can read it. That block
routinely carries VIN, mileage, price, colour, transmission and dealer
address — the exact fields this platform needs.

**Access limitations, and the guardrails in code:**
- Each domain must be added to `data/allowed_domains.txt` by you, after you
  have read that site's terms. Nothing is fetched from a domain that is not
  listed.
- `robots.txt` is fetched and obeyed for every host before any request
  (`porschehunter/http_util.py`). If robots.txt cannot be read, the host is
  treated as off limits.
- One honest `User-Agent`, a 5-second minimum delay per host, no cookies, no
  login, no CAPTCHA handling, no proxy rotation.
- If a page has no JSON-LD, the connector **stops**. It does not fall back to
  scraping raw HTML.

**Cost:** free. **Coverage:** whatever dealers you choose to follow. This is
the practical substitute for a paid feed at low volume.

## 3. MarketCheck — the real paid option

**[SEARCHED]** MarketCheck sells an automotive data API covering dealer
inventory across the US and Canada, with listings search, VIN decode, market
analytics and sales-history products. Documentation lives at
`docs.marketcheck.com`; a dashboard at `developers.marketcheck.com`.

**[UNVERIFIED] Cost:** search results referenced self-serve pricing "starting
at $8" alongside custom enterprise quotes, but I could not open the pricing
page to confirm tiers, call limits or which endpoints each tier includes.
**Treat the price as unknown and ask them for a written quote** that names:
1. the **Cars search** endpoint (active listings) — this is what feeds listings,
2. the **sales history / market** endpoints — this is what could feed comps,
3. monthly call allowance and overage pricing.

**What you need to supply:** export `MARKETCHECK_API_KEY`. That is the only
credential this platform asks for.

**Key limitation:** MarketCheck's strength is franchise and independent
**dealer** inventory. Private-party Craigslist and Facebook cars — often where
the genuine underpricing lives — are largely out of scope.

**Implemented in:** `porschehunter/sources/marketcheck.py`. The endpoint path
and parameter names follow MarketCheck's published v2 shape, but they were
written **without access to a live key**, so verify them against your own plan's
docs before trusting the connector.

## 4. CLASSIC.COM — your best comp source, but not a feed

**[SEARCHED]** CLASSIC.COM offers free **market alerts** on any of its 10,000+
markets (new listings and sold prices by email), shows every sold price to all
users for 30 days, and unlocks the full historical sold record, sales comps and
market charts behind a paid membership. It aggregates listings from dealers,
auctions and data providers, and runs a Verified Seller partner program.

**[UNVERIFIED]** I found no documented self-serve public API. Data licensing
appears to be a direct commercial conversation with them. **If you want
automated comps, email them and ask** — describe your volume, and ask
specifically for sold-price history by generation.

**How this platform uses it today:** as a **comp source via manual entry**.
Set up free market alerts for 996/997/991, and when a sold price lands, record
it with `comps add --venue classic_com --url <sold listing URL>`.

## 5. Bring a Trailer

**[SEARCHED]** No official public API and no data-licensing product. The
third-party "BaT scrapers" that show up in search results are unaffiliated
services; using one puts the terms-of-service question on you, and this
platform does not integrate any of them.

**How this platform uses it:** manual comp entry. BaT completed auctions are
the single richest public record of what 911s actually sell for, with photos,
mileage and commentary. Recording ~30 by hand per generation is a few evenings
of work and is the highest-value thing you can do to make this tool useful.

**Note the fee treatment:** record whether the price you enter is hammer-only
or includes the buyer premium (`--includes-fees`). The valuation engine adds a
premium to hammer-only auction comps so every comp sits on the same
"what a buyer actually paid" basis.

## 6. Cars & Bids — explicitly off limits

**[SEARCHED]** The Cars & Bids Terms of Use prohibit using any manual or
automated process to extract, aggregate or reproduce site content for
commercial purposes, competitive analysis, AI/ML training, or any other
purpose not expressly authorized.

**This platform therefore never fetches carsandbids.com.** The source is
registered as `prohibited`; `porschehunter enable cars_and_bids` refuses, and
pasting a Cars & Bids URL into `add` stores it as an operator reference with a
printed notice. Reading the site yourself as a human is between you and their
terms — the software does not do it for you.

## 7. Facebook Marketplace

**[SEARCHED]** Meta has never released a public Marketplace listings API.
Marketplace is a closed consumer product; Meta's commerce APIs are
partner-gated, in limited programs, and aimed at businesses **posting**
inventory rather than reading it.

**How this platform uses it:** manual URL entry. In practice: browse
Marketplace yourself, and when something looks interesting paste the URL with
whatever the seller disclosed.

## 8. Craigslist

**[SEARCHED]** Craigslist's terms prohibit collecting content via robots,
spiders, scripts, scrapers or crawlers, and prohibit software other than
general-purpose browsers that interoperates with the site. Craigslist **does**
publish RSS on search-result pages and endorses RSS readers, but search results
also note that bulk harvesting via those feeds is likely to get you blocked.

**How this platform handles that tension:** the RSS connector exists, is
**disabled by default**, polls slowly with a 5-second per-host floor, and is
documented as suitable only for a small number of personal saved searches. You
enable it deliberately or not at all. I am not in a position to give you a
legal opinion on it — read the terms and make your own call.

**What a feed actually gives you:** title, URL, post date, and a price if the
title contains one. No VIN, no mileage, no photos. Every Craigslist item
therefore lands with those fields recorded as missing, for you to fill in.

---

## What this means for your strategy

At low annual volume, the honest picture is:

1. **Comps are the bottleneck, not listings.** The platform refuses to produce
   a valuation without documented completed sales, and the best sold-price
   records (BaT, Cars & Bids, CLASSIC.COM) have no authorized bulk access.
   Your first real investment is an evening or two of hand-entering sold
   auctions per generation — not a subscription.
2. **Listing discovery stays partly manual, and that is fine at your volume.**
   Dealer JSON-LD plus your own Marketplace and BaT browsing covers a lot. A
   MarketCheck subscription buys you nationwide dealer inventory automatically;
   it does not buy you the private-party cars where the margin usually is.
3. **The credentials you would need**, and why:
   - `MARKETCHECK_API_KEY` — nationwide dealer listing search, automated. Paid.
   - A CLASSIC.COM membership — full historical sold prices for comps. Paid,
     and manual to transcribe unless you negotiate a data agreement.
   - Nothing else. No source in this platform needs a login, and none is
     accessed in a way that requires one.

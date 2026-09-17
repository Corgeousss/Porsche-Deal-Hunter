# Repository audit — implemented vs. documented vs. unit-tested

Audit date: 2026-09-17. Re-run the evidence commands below to reproduce.

**The distinction this document enforces:** code that parses a saved payload in
a unit test is *not* a working connector. A connector is working when a real
call to a real endpoint has succeeded and been recorded.

## Headline finding

> **No connector in this repository has ever completed a live call to a real
> endpoint.** Every network path is unit-tested against in-memory payloads
> only. The build environment's egress proxy denies all five data hosts
> (`403 Forbidden` on CONNECT), so no live call could be attempted, let alone
> demonstrated.

Verify:

```bash
python3 -m porschehunter --db data/real.db validate --destination OH
```

The report ends with the live-verification status, and the `sources` table
carries `live_verified_at`, which **only** a successful live call in
`porschehunter/validate.py` can set. Nothing else writes it.

## Status by component

Legend: **LIVE** = proven against a real endpoint · **CODE+TESTS** = implemented
and unit-tested, never run live · **CODE ONLY** = implemented, no meaningful
test · **DOC ONLY** = described, not built.

### Ingestion connectors

| Component | Status | Evidence | What is untested |
|---|---|---|---|
| Manual URL entry | **CODE+TESTS** | `TestManualEntry` (6 tests) | Nothing network-dependent — this path is genuinely complete |
| NHTSA vPIC decode | **CODE+TESTS** | `TestVin` (4 tests) cover check-digit, WMI, year, format | `decode_via_vpic()` has **never executed**. Response field names (`Make`, `Model`, `ModelYear`, `ErrorCode`) are unconfirmed against a live payload |
| Dealer JSON-LD (single URL) | **CODE+TESTS** | `TestJsonLd` (3 tests) parse a hand-written schema.org document | `ingest_url()` has **never fetched a page**. robots.txt handling, redirects, charset, real dealer HTML: all unexercised |
| Dealer sitemap discovery | **CODE+TESTS** | `TestDealerSitemapDiscovery` (7 tests) cover sitemap/index parsing, URL filtering and the allowlist refusal | Never fetched a real sitemap. URL-shape heuristics are unproven against real dealer platforms |
| Craigslist RSS | **CODE+TESTS** | `TestRss` (2 tests) parse a hand-written feed | Never fetched a real feed. Disabled by default |
| MarketCheck | **CODE ONLY** | `TestMarketCheckAdapter` (12 tests) cover mapping, pagination guards and the schema gate, not I/O | **Never called.** The three endpoint PATHS are operator-confirmed against MarketCheck's docs. The RESPONSE SCHEMA is not: it lives in `data/marketcheck_fields.json` as candidates with `confirmed: false`, and ingestion is blocked until `marketcheck probe` confirms it against a real reply |
| CLASSIC.COM | **CODE ONLY, AUTH DISABLED** | `TestClassicComAdapter` (10 tests) cover the config gate and price-basis classification | **Never called.** Base URL, auth style, every endpoint path and every response field name are operator-supplied config — the adapter refuses to run until they are filled in and marked confirmed |

### Core logic — no network, genuinely working

| Component | Status | Evidence |
|---|---|---|
| SQLite schema + migrations | **CODE+TESTS** | Migration verified against a pre-existing database |
| Listing upsert, price history, missing-field tracking | **CODE+TESTS** | `test_price_history_records_changes`, `test_update_never_erases_known_data` |
| Comp validation guards | **CODE+TESTS** | `TestCompGuards` (5 tests) |
| Provenance gates (price basis / permission basis) | **CODE+TESTS** | `TestProvenanceGates` (9 tests) |
| Valuation engine | **CODE+TESTS** | `TestValuation` (8 tests) — median, mileage direction, capping, buyer premium, staleness |
| Deal model + max-bid solve | **CODE+TESTS** | `TestDealMath` (8) + `TestTransactionCosts` (13) — identity holds under tax and auction premium; unset tax is never read as an exemption; preliminary vs underwritten status |
| Per-car overrides | **CODE+TESTS** | `TestOverrides` (4 tests) |
| Generation / variant / VIN normalization | **CODE+TESTS** | `TestGenerations` (4), `TestVin` (4) |
| Validation workflow | **CODE+TESTS** | `TestValidationWorkflow` (7 tests) |
| Acceptance report | **CODE+TESTS** | `TestAcceptanceReport` (6 tests) |
| Mechanic cost sheet | **CODE+TESTS** | `TestMechanicSheet` (4 tests) |
| Dashboard render | **CODE+TESTS** | Rendered 13,295 bytes in validation run #1 |

### Documented but deliberately not built

| Item | Why |
|---|---|
| Cars & Bids connector | Their terms prohibit extraction. Registered `prohibited`; `enable` refuses |
| Facebook Marketplace connector | No public API exists |
| Bring a Trailer connector | No API, and aggregating results needs permission |
| Geocoding / route distances | Needs a paid key. State-centroid estimate used instead, labelled |
| Email-alert ingestion (CLASSIC.COM alerts → comps) | Superseded by the licensed API as the correct route |

## Known correctness risks in never-executed code

Honest list of what will most likely break on first live contact:

1. **vPIC response keys** — `decode_and_store()` reads `Make`, `Model`,
   `ModelYear`, `Trim`, `BodyClass`, `DisplacementL`, `EngineCylinders`,
   `EngineConfiguration`, `PlantCity`, `ErrorCode`. Unconfirmed against a live
   response.
2. **MarketCheck response shape** — `listings[]`, `build{}`, `dealer{}`,
   `media.photo_links[]`, `vdp_url`. Unconfirmed.
3. **MarketCheck pagination** — the `start`/`rows` loop assumes an empty
   `listings` array terminates. Unconfirmed.
4. **JSON-LD real-world variance** — dealer platforms nest `offers`,
   `mileageFromOdometer` and `seller.address` inconsistently. The parser handles
   the documented shapes and a couple of variants; real sites will surface more.
5. **robots.txt edge cases** — `urllib.robotparser` fails closed here (an
   unreadable robots.txt blocks the host). Correct, but it means a site with a
   slow or 500-ing robots.txt is simply unusable.
6. **CLASSIC.COM everything** — the entire wire format is operator-supplied
   config. The `classify_price_basis` logic is tested, but against a synthetic
   response shape.

## Test suite composition

116 tests, all passing:

```bash
python3 -m unittest discover -s tests -v
```

- 0 tests make a network call.
- 0 tests mock a network call. Network code is simply not exercised.
- All fixtures are flagged `is_synthetic=1` and excluded from production paths;
  `TestProvenanceGates.test_synthetic_comps_are_excluded_from_real_valuations`
  asserts this directly.

## What would change these statuses

Run the validation workflow on a machine with normal outbound HTTPS:

```bash
python3 -m porschehunter --db data/real.db validate \
    --destination OH \
    --listing-url "https://<allowlisted-dealer>/inventory/<real-911>"
```

A successful run sets `live_verified_at` for `nhtsa_vpic` and `dealer_jsonld`
and records the endpoint, HTTP status and retrieval timestamp in
`validation_steps`. Until then, this document's headline finding stands.

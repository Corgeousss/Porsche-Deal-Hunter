# Runbook — running this locally and validating against real data

Python 3.11+, standard library only. No dependencies, no build step, no server
to provision.

```bash
git clone <this repo> && cd Porsche-Deal-Hunter
python3 --version          # need 3.11 or newer
python3 -m unittest discover -s tests   # 98 tests, expect OK
```

---

## 1. Create a clean, real-data-only database

Keep real data in its own file. Nothing synthetic ever goes here.

```bash
export PORSCHE_DB=data/real.db
python3 -m porschehunter init
python3 -m porschehunter sources        # what may be read, and what is live-verified
```

## 2. Baseline validation, before connecting anything

```bash
python3 -m porschehunter validate --destination OH
```

`--destination` is the two-letter state where your friend's shop is; it drives
the transport estimate. Expect network steps to `PASS` on a normal machine and
`FAIL`/`SKIP` behind a restrictive proxy. **Nothing is marked live-verified
unless a real call actually succeeded.**

## 3. Connect one authorized listing source

The only listing source you can turn on today without a commercial agreement is
a dealer site that publishes `schema.org/Vehicle` JSON-LD.

**Step 3a — read that dealer's terms of use.** Then opt the domain in:

```bash
echo "exampleporschedealer.com" >> data/allowed_domains.txt
python3 -m porschehunter enable dealer_jsonld
```

The tool checks `robots.txt` on every request and will refuse if it is
disallowed. There is no override for that.

**Step 3b — validate end to end against one real listing:**

```bash
python3 -m porschehunter validate \
    --destination OH \
    --listing-url "https://exampleporschedealer.com/inventory/2008-porsche-911-carrera-s-XXXXX"
```

This runs, in order: synthetic-data check → provider readiness → network egress
→ live vPIC call → domain allowlist → robots.txt → ingest → duplicate handling
→ VIN normalization → valuation → underwriting → dashboard render. Every network
step records the exact URL, HTTP status and retrieval timestamp into
`validation_steps`.

**Step 3c — confirm what became live-verified:**

```bash
python3 -m porschehunter status
sqlite3 data/real.db \
  "SELECT step, status, endpoint, http_status, retrieved_at FROM validation_steps
   WHERE run_id=(SELECT MAX(id) FROM validation_runs);"
```

**Step 3d — ingest normally from then on:**

```bash
python3 -m porschehunter fetch dealer_jsonld --url "https://.../inventory/<car>"
python3 -m porschehunter listings
```

For anything without an authorized connector — Facebook Marketplace, a private
seller, an auction you are watching — enter it by hand:

```bash
python3 -m porschehunter add "https://www.facebook.com/marketplace/item/<id>" \
    --title "2008 Porsche 911 Carrera S" --mileage 51000 --price 39500 \
    --vin WP0AB29958S7XXXXX --state CA --city Fresno --seller-type private
```

## 4. Record comps — required before anything can be valued

Every comp must declare **what the number is** and **why you may use it**.
Both default to unusable.

```bash
# Your own completed sale — the strongest comp you will ever have.
python3 -m porschehunter comps add \
    --generation 997.1 --variant "Carrera S" --year 2008 --mileage 48000 \
    --transmission manual --body coupe \
    --price 62500 --date 2026-07-14 --venue own_sale \
    --url "https://<your record or invoice location>" \
    --price-basis verified_transaction \
    --permission-basis own_transaction

python3 -m porschehunter comps coverage   # usable vs merely stored
```

`--price-basis` must be `verified_transaction` for the comp to count.
`last_asking` and `inferred_from_removal` are stored, labelled, and excluded.
`--permission-basis operator_asserts_permission` additionally requires
`--permission-note` describing how you are permitted to use the record.

## 5. Cost assumptions and the mechanic's sheet

```bash
python3 -m porschehunter assumptions                      # all 30, with status
python3 -m porschehunter sheet --out data/cost_sheet.html # printable one-pager
python3 -m porschehunter sheet --out data/cost_sheet.csv  # fillable version
```

Send the sheet to your mechanic. Feed answers back:

```bash
# Global — applies to every car of that generation
python3 -m porschehunter assumptions --set repairs_997 3200 \
    --basis "Mike's quote 2026-09-17: full service + IMS on a 997.1" --verified

# Per car — a real quote for one specific listing
python3 -m porschehunter override 1 --repairs 1800 --transport 1100 \
    --note "Mike 2026-09-17; enclosed carrier quote from Reliable"
```

**Set the tax question explicitly, either way:**

```bash
# If you hold a resale/dealer exemption:
python3 -m porschehunter assumptions --set purchase_tax_pct 0 \
    --basis "Resale exemption confirmed with accountant 2026-09-17" --verified
# If you do not:
python3 -m porschehunter assumptions --set purchase_tax_pct 0.0675 \
    --basis "OH state + county rate, confirmed 2026-09-17" --verified
```

## 6. Underwrite

```bash
python3 -m porschehunter scan --destination OH             # screen everything
python3 -m porschehunter acceptance 1 --destination OH \
    --out data/underwriting_1.txt                          # full report on one car
```

`acceptance` exits `0` when it produces a complete underwriting and `2` when it
returns INSUFFICIENT DATA. It will not lower its bar: it needs at least three
usable verified comps and a clean database.

## 7. Dashboard

```bash
python3 -m porschehunter serve --destination OH   # http://127.0.0.1:8000
```

---

## Licensed providers

Both are disabled until you supply credentials. Neither will guess an endpoint.

**CLASSIC.COM** — the correct primary route for comps.

```bash
python3 -m porschehunter classic-com init-config    # writes data/classic_com_api.json
# Fill it in from https://support.classic.com/classic.com-api after agreeing
# terms with datasupport@classic.com, then set "confirmed": true
export CLASSIC_COM_API_KEY=<licensed key>
python3 -m porschehunter classic-com status
```

**MarketCheck** — listings.

```bash
export MARKETCHECK_API_KEY=<paid key>
python3 -m porschehunter fetch marketcheck --year-min 1999 --year-max 2019
```

`private_party`, `auction`, `past` and `vin_history` raise
`EndpointNotConfirmed` until you set their paths in
`porschehunter/sources/marketcheck.py` from your plan's documentation.

## Environment variables

| Variable | Purpose |
|---|---|
| `PORSCHE_DB` | Database path. Default `data/porsche.db` |
| `CLASSIC_COM_API_KEY` | Enables the CLASSIC.COM adapter |
| `MARKETCHECK_API_KEY` | Enables the MarketCheck client |
| `MARKETCHECK_BASE_URL` | Override the API host if your plan differs |

## Troubleshooting

| Symptom | Cause |
|---|---|
| `validate` reports `network_egress FAIL` | Outbound HTTPS blocked. No live check can pass; fix networking first |
| `robots.txt ... does not permit` | Hard stop by design. Use manual entry |
| `not in data/allowed_domains.txt` | Add the domain after reading that site's terms |
| `INSUFFICIENT DATA` from `acceptance` | Fewer than 3 usable comps. The report names exactly what is needed |
| Comps added but `coverage` shows 0 usable | Missing `--price-basis verified_transaction` or a permission basis |
| `EndpointNotConfirmed` | Deliberate. Confirm the path against your plan's docs |

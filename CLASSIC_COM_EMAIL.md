# CLASSIC.COM API licensing request — ready to send

**To:** datasupport@classic.com
**Subject:** API access request — verified 911 sales data for an internal acquisition tool

---

Hello CLASSIC.COM team,

I run a small used-Porsche acquisition operation and I'm building an **internal**
tool that screens 911 listings and estimates resale value from **verified
completed sales**. I'd like to license your API. I've reviewed
https://support.classic.com/classic.com-api and would appreciate a quote and a
sample dataset.

**What I need the data for:** to value a specific 911 against real, completed
transactions — not asking prices. My tool already refuses to treat a "last
asking" figure or a removed listing as a sale, so I specifically need records
you can confirm are completed transactions.

**Smallest bundle that would work for me — please quote this:**

1. **Verified completed-sale records, per vehicle**, for 996 / 997 / 991
   (and ideally 964 / 993 / 992) 911s, each row with:
   - sale price and **whether it includes buyer's premium**
   - sale date
   - mileage
   - generation / model / trim (variant)
   - transmission and body style if available
   - a source URL for the transaction
   - VIN where available
   Individual rows, not just aggregates or an index — my engine adjusts each
   comp for mileage and rejects mismatched ones, so I need the underlying sales.

2. **A comparable-sales lookup** keyed on generation / variant / year / mileage,
   if you offer it separately from raw sales history.

3. **Taxonomy** sufficient to resolve 911 generation and variant, so I can map
   your model identifiers onto mine.

I do **not** need active-listing feeds, market charts, embeds or dealer-facing
analytics — please quote the narrowest bundle covering 1–3.

**Please also confirm, in writing, the commercial terms** — these determine
whether the tool is usable at all:

- Is **internal business use** permitted? (This is an internal investment tool,
  not a consumer-facing product.)
- May data be **stored locally**, and for how long? (My architecture is a local
  cache of comps; a no-retention licence would need a redesign.)
- May comp-backed **valuations be shown to third parties** — e.g. a partner, a
  mechanic, or a seller?
- Are **derived values** (a valuation computed from your comps) permitted?
- Is **attribution** required, and in what form?
- **Rate limits**, call allowance, and any overage charges.
- **Pricing and minimum term.**

**Requests to get me started:**

- A **price quote** for the bundle above.
- A **sample dataset** (even 20–50 rows for one generation) so I can confirm the
  response shape maps cleanly onto my importer before committing.
- The **API documentation** for the endpoints included, including which field
  proves a record is a completed sale versus a last-asking price.

Happy to sign an NDA or data agreement. Thanks very much — I'm ready to move
quickly once I understand the terms.

Best regards,
[Your name]
[Business name]
[Phone]
cdean95@gmail.com

---

### After they reply (what to do with the answers)

1. `python -m porschehunter classic-com init-config` (already run — creates
   `data/classic_com_api.json`).
2. Fill in `base_url`, `auth`, `endpoints` and the response **field map** from
   their docs, and set `"confirmed": true`. Record the commercial-rights answers
   in the `licence` block of that file.
3. `set CLASSIC_COM_API_KEY=<licensed key>` (PowerShell: `$env:CLASSIC_COM_API_KEY="..."`).
4. `python -m porschehunter classic-com status` — should report **ready: True**.
5. Import comps, then `comps coverage` to see how many cars become valuable.

The adapter is already written and unit-tested; it stays disabled until steps
2–3 are done, and it will not guess an endpoint path.

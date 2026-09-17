# Facebook Marketplace — compliant integration

## Investigation: what access is actually available

- **No public Marketplace listings API.** Meta does not offer an API that
  returns Marketplace listings for search/discovery. The Commerce/Catalog APIs
  are partner-gated and are for *businesses posting their own inventory*, not for
  pulling other people's private listings. So **fully-automatic nationwide
  discovery of privately-listed 911s is not available through an authorized
  channel.**
- **Terms prohibit scraping.** Facebook's Terms forbid automated collection of
  data, and listings sit behind login. This tool will not bypass login, CAPTCHAs,
  rate limits, or Meta's terms — there is no scraper here and none will be added.
- **Permitted alerts:** none via an official Marketplace feed. (You can create a
  Marketplace saved search inside Facebook and let *Facebook* email you; that is
  Facebook notifying you, which is fine — but it is not something this app can do
  for you automatically.)

**Conclusion:** the compliant path is a **browser-assisted, operator-approved
import** of a listing *you are already viewing* in your own logged-in browser.
The app captures the details you can see, preserves the original URL, asks for
anything missing, and files it as an **assisted import** — clearly distinct from
automatic dealer discovery. It runs through the same 911-only guard, so a Cayman
pasted from Facebook is rejected too.

Imported Facebook listings are then **searchable alongside dealer inventory**,
appear in **saved searches and alerts**, and go through the **valuation
workflow** (they stay UNVALUED until verified comps exist, like every other car).
On the dashboard they carry an **"assisted import"** badge and `source =
facebook_marketplace`.

## Three ways to import (all operator-approved, all compliant)

### 1. One-click bookmarklet (fastest)

Create a browser bookmark whose **URL** is the code below (Chrome: bookmark any
page, then edit it and paste this as the URL). Name it **"➜ Deal Hunter"**.

```
javascript:(function(){function m(p){var e=document.querySelector('meta[property="'+p+'"]')||document.querySelector('meta[name="'+p+'"]');return e?e.content:'';}var d={url:location.href.split('?')[0],title:m('og:title'),price:(m('product:price:amount')||m('og:price:amount')),image:m('og:image'),notes:m('og:description')};var y=(String(d.title).match(/\b(19|20)\d\d\b/)||[])[0];if(y)d.year=y;window.open('http://127.0.0.1:8000/#import='+encodeURIComponent(JSON.stringify(d)),'_blank');})();
```

**Use it:** open a Porsche 911 listing on Facebook Marketplace (logged in, as
normal), click **➜ Deal Hunter**. A new tab opens the dashboard with an import
form pre-filled from the listing's published page tags. Review it, add anything
missing (mileage, VIN, state), and click **Import**. Nothing is sent anywhere
except your own local app, and only when you click Import.

> The bookmarklet only reads the page's standard `og:`/`product:` meta tags in
> your own browser for the single listing you opened — it does not crawl,
> paginate, or automate Facebook.

### 2. Dashboard "Import listing" button

On the dashboard, click **Import listing** (top bar), paste the Facebook URL and
type in the details you can see. Same result, no bookmark needed.

### 3. Claude-assisted (interactive)

While you have a listing open, ask Claude to import it. With your approval, Claude
reads the details from the tab you're viewing and files the import for you — one
listing at a time, each confirmed by you.

## What gets captured

VIN (if shown), year/generation/variant, mileage, price, location, seller type,
listing URL (preserved verbatim), photo, and your notes. Missing fields are
recorded as missing, never guessed. The listing is decoded/verified as a 911; if
a VIN is provided and decodes to a non-911, the import is quarantined.

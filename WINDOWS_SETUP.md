# Windows setup — one command at a time

Written for someone who does not write code. Do these **in order**. After each
one, check that what you see matches "You should see". If it doesn't, stop and
send me what you got.

**Where to type:** click Start, type `powershell`, press Enter. A blue or black
window opens. That is PowerShell. Every command goes there — type it (or paste
with right-click) and press Enter.

---

## Step 1 — Do you have Python?

```powershell
python --version
```

**You should see:** `Python 3.11.x` or higher (3.12, 3.13 are fine).

**If you see an error, or the Microsoft Store opens:** install Python from
<https://www.python.org/downloads/> — download, run it, and **tick "Add python.exe
to PATH"** on the first screen before clicking Install. Then close PowerShell,
open it again, and repeat Step 1.

## Step 2 — Go to the project folder

If you already have the project, replace the path below with yours:

```powershell
cd $HOME\Porsche-Deal-Hunter
```

**You should see:** the prompt changes to end with `Porsche-Deal-Hunter>`.

**If you don't have the project yet**, get it first:

```powershell
cd $HOME
git clone https://github.com/Corgeousss/Porsche-Deal-Hunter.git
cd Porsche-Deal-Hunter
```

## Step 3 — Get the latest code

```powershell
git fetch origin
```

**You should see:** a few lines about downloading, or nothing at all. Both fine.

```powershell
git checkout claude/porsche-acquisition-platform-0jsjrq
```

**You should see:** `Switched to branch 'claude/porsche-acquisition-platform-0jsjrq'`

```powershell
git pull
```

**You should see:** `Already up to date.` or a list of updated files.

## Step 4 — Check it runs

There is **nothing to install**. No pip, no packages. It uses only what Python
ships with.

```powershell
python -m unittest discover -s tests
```

**You should see:** a row of dots, then `Ran 116 tests` and `OK` on the last line.

**If you see `FAILED`:** stop and send me the output.

## Step 5 — Create your real database

```powershell
$env:PORSCHE_DB = "data\real.db"
```

**You should see:** nothing. That is correct.

> This only lasts while the PowerShell window is open. If you close it, run this
> line again before anything else.

```powershell
python -m porschehunter init
```

**You should see:** `Initialised data\real.db`

## Step 6 — Set your tax position

The tool refuses to guess this. Pick **one** of the two lines below.

If your accountant has confirmed you buy under a **resale/dealer exemption**:

```powershell
python -m porschehunter assumptions --set purchase_tax_pct 0 --basis "resale exemption confirmed with accountant" --verified
```

If you will **pay tax** on purchases (replace `0.0675` with your real combined rate — 6.75% is written as `0.0675`):

```powershell
python -m porschehunter assumptions --set purchase_tax_pct 0.0675 --basis "my state combined rate, confirmed" --verified
```

**You should see:** `purchase_tax_pct = 0` (or your rate) `[marked VERIFIED]`

## Step 7 — Check your internet connection works for this tool

```powershell
python -m porschehunter validate --destination OH
```

Replace `OH` with the two-letter state where your friend's shop is.

**You should see:** a list of checks. The important line is:

```
[PASS] network_egress
[PASS] vpic_live_call
```

**If those say PASS**, your computer can reach the outside world and the VIN
decoder is now genuinely verified — the last line will say which sources became
live-verified.

**If they say FAIL**, your network or antivirus is blocking it. Send me the
output.

---

# Getting your first real Porsche listings

You have two routes. **Route A costs nothing.** Do that one first.

## Route A — a dealer website (free)

This only works on dealer sites that publish machine-readable listing data, and
only where the site's own rules permit it. The tool checks those rules itself
and stops if they say no.

**Step A1 — pick one dealer** whose inventory you actually care about. Read
their Terms of Use page first. Then add their domain (no `https://`, no `www.`):

```powershell
Add-Content data\allowed_domains.txt "exampleporschedealer.com"
```

**You should see:** nothing. Correct.

**Step A2 — turn the source on:**

```powershell
python -m porschehunter enable dealer_jsonld
```

**You should see:** `dealer_jsonld enabled`

**Step A3 — pull their listings:**

```powershell
python -m porschehunter fetch dealer_jsonld --domain exampleporschedealer.com
```

This is **slow on purpose** — one page every 5 seconds, so we are a polite
visitor. Forty pages takes about four minutes. Let it finish.

**You should see:** lines about sitemaps, then
`dealer_jsonld: seen=N new=N updated=0 status=ok`

**If you see `status=skipped` and "No candidate vehicle URLs found":** that
dealer doesn't publish a usable sitemap. Try a different dealer, or add cars one
at a time (see Route C).

## Route B — MarketCheck (paid, nationwide)

Only after you have a key from marketcheck.com.

**Step B1 — set your key** (replace with your real key):

```powershell
$env:MARKETCHECK_API_KEY = "your-key-here"
```

**Step B2 — confirm what their data actually looks like.** I could not see
their response format, so the tool must learn it from a real reply before it
will import anything:

```powershell
python -m porschehunter marketcheck probe --kind active
```

**You should see:** the endpoint it called, then a list of field names, then
either "Mapped fields that RESOLVED" (good) or a list that "did NOT resolve".

**Send me that output.** I will correct the mapping file for you. That is the
one step where you need me.

**Step B3 — after I confirm the mapping**, pull listings:

```powershell
python -m porschehunter fetch marketcheck --kind fsbo --year-min 1999 --year-max 2019
```

`--kind fsbo` is private sellers, `--kind active` is dealers, `--kind auction`
is auctions.

## Route C — type in one car by hand (always works)

For a Facebook Marketplace car, a private seller, anything:

```powershell
python -m porschehunter add "https://www.facebook.com/marketplace/item/123456" --title "2008 Porsche 911 Carrera S" --price 39500 --mileage 51000 --state CA --city Fresno --seller-type private
```

**You should see:** `Added listing #1`

---

# See your cars

```powershell
python -m porschehunter listings
```

**You should see:** each car with its price, mileage, location, source URL and a
status line.

Every car will say **`UNVALUED - no usable comparable sales`**. That is correct
and expected. The tool will not put a value on a car until it has real records of
what similar cars actually sold for, and you don't have those yet.

---

# What makes valuations possible

You need at least **3 verified completed sales** per car type. Two ways to get
them:

**Free, today —** your own past sales:

```powershell
python -m porschehunter comps add --generation 997.1 --variant "Carrera S" --year 2008 --mileage 48000 --price 62500 --date 2026-07-14 --venue own_sale --url "https://your-records-link" --price-basis verified_transaction --permission-basis own_transaction
```

**You should see:** `-> USABLE in valuations.`

**Paid —** email `datasupport@classic.com` and license their sales-history API.
That is the proper long-term source.

Check where you stand at any time:

```powershell
python -m porschehunter comps coverage
```

**You should see:** a table ending with `N of M stored comps are usable`.

Once a car has 3+ usable comps:

```powershell
python -m porschehunter acceptance 1 --destination OH
```

**You should see:** either a full underwriting with a maximum offer, or
`INSUFFICIENT DATA` telling you exactly what is still missing.

---

# Quick reference

| I want to... | Command |
|---|---|
| See my cars | `python -m porschehunter listings` |
| Find opportunities | `python -m porschehunter scan --destination OH` |
| Check one car in detail | `python -m porschehunter acceptance 1 --destination OH` |
| See my cost assumptions | `python -m porschehunter assumptions` |
| Print the mechanic's sheet | `python -m porschehunter sheet --out data\cost_sheet.html` |
| Check everything still works | `python -m porschehunter validate --destination OH` |

**Remember:** every new PowerShell window needs these two lines first:

```powershell
cd $HOME\Porsche-Deal-Hunter
$env:PORSCHE_DB = "data\real.db"
```

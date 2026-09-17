"""Filterable inventory dashboard. Standard-library http.server, no frameworks,
no external assets -- inline CSS/JS so it works offline and on a phone.

The server exposes a small JSON API the single-page UI calls:

    GET  /                       the app shell (also what validate renders)
    GET  /api/options            taxonomy + facets + source coverage
    GET  /api/inventory?f=&sort= filtered, sorted, enriched listings + summary
    GET  /api/searches           saved searches
    POST /api/searches           save {name, filters, sort, notify}
    POST /api/searches/delete    {id}
    GET  /api/notifications       {unread, items}
    POST /api/notifications/read  {id?}
    POST /api/alerts/run          recompute alerts (manual; never background)
    GET  /api/recon?listing_id=   recon lines + summary for one car
    POST /api/recon               {listing_id, category, amount, basis, note}
    POST /api/stage               {listing_id, deal_stage}
    POST /api/condition           {listing_id, field, value}

Every profit/return figure shown is comp-backed or it is not shown at all;
unvalued cars are labelled UNVALUED, never given an invented value.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import urllib.parse
from pathlib import Path

from . import db as _db
from . import filters as _filters
from . import recon as _recon
from . import searches as _searches
from . import taxonomy as _tax
from .sources import manual as _manual

PRICE_PRESETS = [30000, 50000, 75000, 100000, 150000, 200000]

# Fields the operator may set from the UI, with their allowed values (None = free text/number).
_CONDITION_FIELDS = {
    "title_status": ["clean", "salvage", "rebuilt"],
    "accident_history": ["none", "reported"],
    "service_records": ["yes", "no"],
    "recent_major_service": ["yes", "no"],
    "ppi_done": ["yes", "no"],
    "seller_docs": ["yes", "no"],
    "original_status": ["original", "modified"],
    "owners_count": None,
    "known_issues": None,
    "cosmetic_condition": None,
}


# ---------------------------------------------------------------------------
# API payload builders (also unit-testable without a server)
# ---------------------------------------------------------------------------
def options_payload(conn, destination_state: str) -> dict:
    active = conn.execute(
        "SELECT * FROM listings WHERE status='active'").fetchall()
    states = sorted({r["seller_state"] for r in active if r["seller_state"]})
    variants_present = sorted({r["variant"] for r in active if r["variant"]})
    seller_types = sorted({(r["seller_type"] or "unknown") for r in active})

    coverage = []
    for s in conn.execute("SELECT * FROM sources ORDER BY key"):
        rows = conn.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT vin) v FROM listings "
            "WHERE source_key=? AND status='active'", (s["key"],)).fetchone()
        if rows["c"] == 0 and not s["enabled"] and s["access_method"] == "manual":
            pass
        coverage.append({
            "key": s["key"], "name": s["name"],
            "enabled": bool(s["enabled"]),
            "authorized": s["authorized"],
            "live_verified": bool(s["live_verified_at"]),
            "last_success": s["last_success_at"],
            "active_listings": rows["c"], "unique_vins": rows["v"],
        })

    # Per-dealer-domain contribution (all dealer sites share source_key
    # 'dealer_jsonld', so unique active vehicles are counted by listing host).
    dealer_coverage: dict[str, dict] = {}
    for r in active:
        host = urllib.parse.urlparse(r["url"]).netloc.replace("www.", "")
        d = dealer_coverage.setdefault(host, {"host": host, "active_listings": 0,
                                              "vins": set()})
        d["active_listings"] += 1
        if r["vin"]:
            d["vins"].add(r["vin"].upper())
    dealer_coverage = sorted(
        ({"host": d["host"], "active_listings": d["active_listings"],
          "unique_vins": len(d["vins"])} for d in dealer_coverage.values()),
        key=lambda x: -x["active_listings"])

    return {
        "generation_groups": _tax.generation_options(),
        "dealer_coverage": dealer_coverage,
        "available_variants": _tax.AVAILABLE_VARIANTS,
        "all_variants": _tax.ALL_VARIANTS,
        "body_styles": _tax.BODY_STYLES,
        "transmissions": _tax.TRANSMISSIONS,
        "drivetrains": _tax.DRIVETRAINS,
        "options": [{"key": k, "label": lbl} for k, lbl in _tax.FILTERABLE_OPTIONS],
        "deal_stages": _tax.DEAL_STAGES,
        "recon_categories": [{"key": k, "label": lbl} for k, lbl in _recon.CATEGORIES],
        "condition_fields": _CONDITION_FIELDS,
        "price_presets": PRICE_PRESETS,
        "facets": {"states": states, "variants_present": variants_present,
                   "seller_types": seller_types,
                   "sources": [c["key"] for c in coverage if c["active_listings"]]},
        "target_net_profit": _db.get_assumption(conn, "target_net_profit"),
        "destination_state": destination_state,
        "source_coverage": coverage,
        "counts": {
            "total_active": len(active),
            "unique_vins": len({(r["vin"] or "").upper() for r in active if r["vin"]}),
        },
    }


def inventory_payload(conn, filters: dict, sort: str, destination_state: str,
                      include_synthetic: bool = False) -> dict:
    rows = _filters.query(conn, filters, sort, destination_state, include_synthetic)
    def under(n):
        return sum(1 for r in rows if r.get("price") is not None and r["price"] < n)
    by_gen: dict[str, int] = {}
    by_variant: dict[str, int] = {}
    for r in rows:
        by_gen[r.get("generation") or "unknown"] = by_gen.get(r.get("generation") or "unknown", 0) + 1
        by_variant[r.get("variant") or "unknown"] = by_variant.get(r.get("variant") or "unknown", 0) + 1
    return {
        "count": len(rows),
        "results": rows,
        "summary": {
            "under_50k": under(50000), "under_75k": under(75000),
            "under_100k": under(100000),
            "by_generation": dict(sorted(by_gen.items())),
            "by_variant": dict(sorted(by_variant.items())),
            "unvalued": sum(1 for r in rows if r.get("valuation_status") != "ok"),
            "duplicates": sum(1 for r in rows if r.get("is_duplicate_vin")),
        },
    }


# ---------------------------------------------------------------------------
# The page (shell). Data is loaded by the inline app from the API above.
# ---------------------------------------------------------------------------
def render(conn, destination_state: str, include_synthetic: bool = False) -> str:
    boot = {
        "destination_state": destination_state,
        "include_synthetic": include_synthetic,
        "default_price_max": 100000,
        "target_net_profit": _db.get_assumption(conn, "target_net_profit"),
    }
    return (_PAGE
            .replace("/*__CSS__*/", _CSS)
            .replace("/*__JS__*/", _JS)
            .replace("__BOOT__", json.dumps(boot)))


_CSS = r"""
*{box-sizing:border-box}
body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#16181d}
header{background:#16181d;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:20}
header h1{margin:0;font-size:16px;font-weight:700;white-space:nowrap}
header .sub{color:#9aa0a6;font-size:11px}
header .spacer{flex:1}
button{font:inherit;cursor:pointer;border:1px solid #cfd2d6;background:#fff;border-radius:6px;padding:6px 10px}
button.primary{background:#1a5fb4;color:#fff;border-color:#1a5fb4}
button.ghost{background:transparent;color:#fff;border-color:#3a3d44}
select,input[type=text],input[type=number]{font:inherit;padding:5px 7px;border:1px solid #cfd2d6;border-radius:6px;background:#fff;width:100%}
.wrap{display:flex;align-items:flex-start}
aside{width:300px;min-width:300px;background:#fff;border-right:1px solid #e2e4e8;padding:12px;height:calc(100vh - 52px);overflow:auto;position:sticky;top:52px}
main{flex:1;padding:14px 16px;min-width:0}
.fgroup{border-bottom:1px solid #eceef0;padding:9px 0}
.fgroup h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#5f6368;cursor:pointer;display:flex;justify-content:space-between}
.fgroup .body{display:block}
.fgroup.collapsed .body{display:none}
.row2{display:flex;gap:6px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.chip{border:1px solid #cfd2d6;border-radius:14px;padding:3px 9px;font-size:12px;background:#fff;cursor:pointer;user-select:none}
.chip.on{background:#1a5fb4;color:#fff;border-color:#1a5fb4}
label.ck{display:flex;align-items:center;gap:6px;font-size:13px;margin:2px 0}
label.ck input{width:auto}
.grp-label{font-size:11px;color:#5f6368;margin:6px 0 2px;font-weight:600}
.banner{background:#fff4e5;border:1px solid #f0c48a;padding:9px 11px;border-radius:6px;margin-bottom:12px;font-size:13px}
.banner.bad{background:#fdecea;border-color:#e6a49c}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.stat{background:#fff;border:1px solid #e2e4e8;border-radius:8px;padding:8px 12px;min-width:96px}
.stat b{display:block;font-size:18px}
.stat span{color:#5f6368;font-size:11px}
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #e2e4e8;border-radius:8px;padding:12px;margin-bottom:10px;display:flex;gap:12px}
.card.hit{border-left:4px solid #1a7f37}
.card img{width:150px;height:100px;object-fit:cover;border-radius:6px;background:#eceef0;flex-shrink:0}
.card .info{flex:1;min-width:0}
.card h3{margin:0 0 3px;font-size:15px}
.meta{color:#5f6368;font-size:12px;margin:2px 0}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600}
.pill.go{background:#e6f4ea;color:#137333}.pill.no{background:#f1f3f4;color:#5f6368}
.pill.warn{background:#fef7e0;color:#976800}.pill.dup{background:#fce8e6;color:#c5221f}
.price{font-size:17px;font-weight:700}
a{color:#1a5fb4}
.notif-panel,.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:40;display:none}
.notif-panel.open,.modal-bg.open{display:block}
.notif-inner,.modal{position:absolute;background:#fff;border-radius:10px;padding:16px;max-height:80vh;overflow:auto}
.notif-inner{right:12px;top:56px;width:min(420px,92vw)}
.modal{left:50%;top:8vh;transform:translateX(-50%);width:min(560px,94vw)}
.notif{border-bottom:1px solid #eceef0;padding:7px 0;font-size:13px}
.notif.unread{background:#f0f6ff}
.bell{position:relative}
.badge{position:absolute;top:-6px;right:-6px;background:#c5221f;color:#fff;border-radius:9px;font-size:10px;padding:0 5px}
.reconrow{display:flex;gap:6px;align-items:center;margin:4px 0}
.reconrow label{flex:1;font-size:13px}
.reconrow input{width:100px}.reconrow select{width:110px}
@media(max-width:820px){aside{position:fixed;left:0;top:52px;z-index:30;transform:translateX(-100%);transition:.2s;box-shadow:2px 0 12px rgba(0,0,0,.2)}
 aside.open{transform:none}.card img{width:96px;height:72px}.wrap{display:block}}
"""


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Porsche 911 Deal Hunter</title>
<style>/*__CSS__*/</style></head><body>
<header>
  <button class=ghost id=btnFilters style="display:none">Filters</button>
  <h1>911 Deal Hunter</h1>
  <span class=sub id=hsub></span>
  <span class=spacer></span>
  <select id=sort style="width:auto"></select>
  <select id=savedSel style="width:auto"><option value="">Saved searches…</option></select>
  <button id=btnImport title="Import a Facebook/private listing you are viewing">Import listing</button>
  <button id=btnSave>Save search</button>
  <button id=btnRefresh title="Recompute matches and alerts">Refresh</button>
  <button class="bell" id=btnBell>🔔<span class=badge id=badge style="display:none">0</span></button>
</header>
<div class=wrap>
  <aside id=aside></aside>
  <main>
    <div id=banners></div>
    <div class=summary id=summary></div>
    <div class=toolbar>
      <b id=count>…</b><span class=sub id=coverage></span>
      <span class=spacer></span>
      <button id=btnReset>Reset all filters</button>
    </div>
    <div id=results></div>
  </main>
</div>
<div class=notif-panel id=notifPanel><div class=notif-inner>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b>Notifications</b><button id=markRead>Mark all read</button></div>
  <div style="font-size:11px;color:#5f6368;margin:4px 0 8px">
    Alerts update when you press Refresh. This app does not monitor in the background.</div>
  <div id=notifList></div></div></div>
<div class=modal-bg id=modalBg><div class=modal id=modal></div></div>
<script>__BOOT_TAG__</script>
<script>/*__JS__*/</script>
</body></html>""".replace("__BOOT_TAG__", 'window.BOOT=__BOOT__;')


_JS = r"""
const $=(s,e=document)=>e.querySelector(s), $$=(s,e=document)=>[...e.querySelectorAll(s)];
const money=v=>v==null?'—':'$'+Math.round(v).toLocaleString();
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let OPT={}, STATE={filters:{price_max:BOOT.default_price_max}, sort:'newest'};

async function api(path,opts){const r=await fetch(path,opts);if(!r.ok)throw new Error(r.status);return r.json();}

const SORTS=[['newest','Newest listing'],['price_asc','Lowest asking price'],
 ['price_desc','Highest asking price'],['mileage_asc','Lowest mileage'],
 ['largest_reduction','Largest price reduction'],['highest_profit','Highest verified profit'],
 ['highest_roi','Highest verified ROI'],['auction_soon','Auction ending soon']];

function getGens(){return STATE.filters.generations||[];}
function selectedVariants(){ // variants available for currently selected generations
  const gens=getGens(); if(!gens.length) return OPT.all_variants;
  const set=new Set(); gens.forEach(g=>{(OPT.available_variants[g]||[]).forEach(v=>set.add(v));});
  return OPT.all_variants.filter(v=>set.has(v));
}

function chip(label,active,on){const c=document.createElement('span');c.className='chip'+(active?' on':'');
  c.textContent=label;c.onclick=on;return c;}

function multiChip(container,items,selKey,labelFn){
  container.innerHTML='';
  const sel=STATE.filters[selKey]||[];
  items.forEach(it=>{const key=(typeof it==='object')?it.key:it; const lab=labelFn?labelFn(it):(typeof it==='object'?it.label:it);
    container.appendChild(chip(lab, sel.includes(key), ()=>{
      let s=STATE.filters[selKey]||[]; s=s.includes(key)?s.filter(x=>x!==key):[...s,key];
      if(s.length)STATE.filters[selKey]=s; else delete STATE.filters[selKey];
      if(selKey==='generations')buildFilters(); apply();}));});
}

function numRange(minKey,maxKey,ph){
  const d=document.createElement('div');d.className='row2';
  const a=document.createElement('input');a.type='number';a.placeholder='min '+(ph||'');a.value=STATE.filters[minKey]??'';
  const b=document.createElement('input');b.type='number';b.placeholder='max '+(ph||'');b.value=STATE.filters[maxKey]??'';
  a.oninput=()=>{a.value===''?delete STATE.filters[minKey]:STATE.filters[minKey]=+a.value;apply();};
  b.oninput=()=>{b.value===''?delete STATE.filters[maxKey]:STATE.filters[maxKey]=+b.value;apply();};
  d.append(a,b);return d;
}

function group(title,collapsed){const g=document.createElement('div');g.className='fgroup'+(collapsed?' collapsed':'');
  const h=document.createElement('h4');h.innerHTML=esc(title)+'<span>▾</span>';const b=document.createElement('div');b.className='body';
  h.onclick=()=>g.classList.toggle('collapsed');g.append(h,b);return {g,b};}

function buildFilters(){
  const a=$('#aside');a.innerHTML='';
  // PRICE
  let {g,b}=group('Price & financial');
  const presets=document.createElement('div');presets.className='chips';
  OPT.price_presets.forEach(p=>presets.appendChild(chip('under $'+(p/1000)+'k',STATE.filters.price_max===p,()=>{STATE.filters.price_max=p;delete STATE.filters.price_min;buildFilters();apply();})));
  presets.appendChild(chip('all prices',!STATE.filters.price_max&&!STATE.filters.price_min,()=>{delete STATE.filters.price_max;delete STATE.filters.price_min;buildFilters();apply();}));
  b.append(presets, numRange('price_min','price_max','$'));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Min projected net profit (verified only)</div>');
  const np=document.createElement('input');np.type='number';np.placeholder='e.g. '+BOOT.target_net_profit;np.value=STATE.filters.net_profit_min??'';
  np.oninput=()=>{np.value===''?delete STATE.filters.net_profit_min:STATE.filters.net_profit_min=+np.value;apply();};b.append(np);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Min ROI on capital (verified only, e.g. 0.15)</div>');
  const roi=document.createElement('input');roi.type='number';roi.step='0.01';roi.value=STATE.filters.roi_min??'';
  roi.oninput=()=>{roi.value===''?delete STATE.filters.roi_min:STATE.filters.roi_min=+roi.value;apply();};b.append(roi);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Resale value range (verified only)</div>');
  b.append(numRange('resale_min','resale_max','$'));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Max total capital / recon / shipping (estimate)</div>');
  b.append(numRange(null,'capital_max','cap'),numRange(null,'recon_max','recon'),numRange(null,'shipping_max','ship'));
  a.append(g);
  // GENERATION
  ({g,b}=group('Model & generation'));
  OPT.generation_groups.forEach(grp=>{
    b.insertAdjacentHTML('beforeend','<div class=grp-label>'+esc(grp.group)+'</div>');
    const c=document.createElement('div');c.className='chips';
    grp.generations.forEach(gg=>c.appendChild(chip(gg.key,getGens().includes(gg.key),()=>{
      let s=getGens();s=s.includes(gg.key)?s.filter(x=>x!==gg.key):[...s,gg.key];
      if(s.length)STATE.filters.generations=s;else delete STATE.filters.generations;buildFilters();apply();})));
    b.append(c);});
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Variant (only those valid for selected generations)</div>');
  const vc=document.createElement('div');vc.className='chips';multiChip(vc,selectedVariants(),'variants');b.append(vc);
  a.append(g);
  // SPECS
  ({g,b}=group('Specifications'));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Model year</div>');b.append(numRange('year_min','year_max'));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Mileage</div>');b.append(numRange('mileage_min','mileage_max','mi'));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Transmission</div>');
  const tc=document.createElement('div');tc.className='chips';multiChip(tc,[...OPT.transmissions,'unknown'],'transmissions');b.append(tc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Body style</div>');
  const bc=document.createElement('div');bc.className='chips';multiChip(bc,[...OPT.body_styles,'unknown'],'body_styles');b.append(bc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Drivetrain</div>');
  const dc=document.createElement('div');dc.className='chips';multiChip(dc,[...OPT.drivetrains,'unknown'],'drivetrains');b.append(dc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Exterior / interior colour contains</div>');
  const ex=document.createElement('input');ex.type='text';ex.placeholder='exterior colour';ex.value=STATE.filters.exterior_color||'';
  ex.oninput=()=>{ex.value?STATE.filters.exterior_color=ex.value:delete STATE.filters.exterior_color;apply();};b.append(ex);
  const inc=document.createElement('input');inc.type='text';inc.placeholder='interior colour';inc.value=STATE.filters.interior_color||'';
  inc.oninput=()=>{inc.value?STATE.filters.interior_color=inc.value:delete STATE.filters.interior_color;apply();};b.append(inc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Confirmed factory options (never guessed)</div>');
  const oc=document.createElement('div');oc.className='chips';multiChip(oc,OPT.options,'options');b.append(oc);
  a.append(g);
  // CONDITION
  ({g,b}=group('Condition & history',true));
  const condDefs=[['title_status','Title',['clean','salvage','rebuilt','unknown']],
   ['accident_history','Accidents',['none','reported','unknown']],
   ['service_records','Service records',['yes','no','unknown']],
   ['ppi_done','PPI done',['yes','no','unknown']],
   ['seller_docs','Seller docs',['yes','no','unknown']],
   ['original_status','Original/modified',['original','modified','unknown']]];
  condDefs.forEach(([k,lab,vals])=>{b.insertAdjacentHTML('beforeend','<div class=grp-label>'+lab+'</div>');
    const c=document.createElement('div');c.className='chips';multiChip(c,vals,k);b.append(c);});
  a.append(g);
  // LOCATION
  ({g,b}=group('Location & seller',true));
  b.insertAdjacentHTML('beforeend','<div class=grp-label>State</div>');
  const sc=document.createElement('div');sc.className='chips';multiChip(sc,OPT.facets.states,'states');b.append(sc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>City contains</div>');
  const city=document.createElement('input');city.type='text';city.value=STATE.filters.city||'';
  city.oninput=()=>{city.value?STATE.filters.city=city.value:delete STATE.filters.city;apply();};b.append(city);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Seller type</div>');
  const stc=document.createElement('div');stc.className='chips';multiChip(stc,['dealer','private','auction_house','unknown'],'seller_types');b.append(stc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Listing type</div>');
  const ltc=document.createElement('div');ltc.className='chips';multiChip(ltc,['fixed','auction','unknown'],'listing_types');b.append(ltc);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Source</div>');
  const src=document.createElement('div');src.className='chips';multiChip(src,OPT.facets.sources,'sources');b.append(src);
  a.append(g);
  // ACTIVITY
  ({g,b}=group('Listing activity',true));
  const la=document.createElement('div');la.className='chips';
  [['1','24h'],['3','3 days'],['7','7 days'],['30','30 days']].forEach(([d,lab])=>
    la.appendChild(chip('listed '+lab,STATE.filters.listed_within_days==+d,()=>{
      STATE.filters.listed_within_days==+d?delete STATE.filters.listed_within_days:STATE.filters.listed_within_days=+d;buildFilters();apply();})));
  b.append(la);
  const red=document.createElement('label');red.className='ck';red.innerHTML='<input type=checkbox '+(STATE.filters.price_reduced?'checked':'')+'> price reduced';
  red.querySelector('input').onchange=e=>{e.target.checked?STATE.filters.price_reduced=true:delete STATE.filters.price_reduced;apply();};b.append(red);
  const dup=document.createElement('label');dup.className='ck';dup.innerHTML='<input type=checkbox '+(STATE.filters.duplicates_only?'checked':'')+'> duplicate VINs only';
  dup.querySelector('input').onchange=e=>{e.target.checked?STATE.filters.duplicates_only=true:delete STATE.filters.duplicates_only;apply();};b.append(dup);
  b.insertAdjacentHTML('beforeend','<div class=grp-label>Auction ending within (hours)</div>');
  const au=document.createElement('input');au.type='number';au.value=STATE.filters.auction_ending_within_hours??'';
  au.oninput=()=>{au.value===''?delete STATE.filters.auction_ending_within_hours:STATE.filters.auction_ending_within_hours=+au.value;apply();};b.append(au);
  a.append(g);
  // DEAL STATUS
  ({g,b}=group('Deal status',true));
  const stages=['unvalued','preliminary','underwritten',...OPT.deal_stages];
  const dsc=document.createElement('div');dsc.className='chips';multiChip(dsc,stages,'deal_statuses');b.append(dsc);
  a.append(g);
}

function renderSummary(s){
  $('#summary').innerHTML=
    stat(s.under_50k,'under $50k')+stat(s.under_75k,'under $75k')+stat(s.under_100k,'under $100k')+
    stat(s.unvalued,'unvalued')+stat(s.duplicates,'dup VIN');
}
const stat=(n,l)=>'<div class=stat><b>'+n+'</b><span>'+l+'</span></div>';

function card(v){
  const hit=v.meets_threshold&&v.underwriting_status==='underwritten';
  const title=esc(v.title||((v.year||'')+' 911 '+(v.variant||'')));
  let val= v.valuation_status==='ok'
    ? '<span class="pill go">net '+money(v.net_profit)+(v.roi!=null?' · ROI '+(v.roi*100).toFixed(0)+'%':'')+'</span>'
    : '<span class="pill no">UNVALUED — no verified comps</span>';
  const dup=v.is_duplicate_vin?' <span class="pill dup">dup VIN</span>':'';
  const AUTO=['dealer_jsonld','marketcheck'];
  const assisted=!AUTO.includes(v.source_key)?' <span class="pill warn">assisted import</span>':'';
  const img=v.photo_url?'<img loading=lazy src="'+esc(v.photo_url)+'">':'<img alt="">';
  return '<div class="card'+(hit?' hit':'')+'">'+img+'<div class=info>'+
    '<h3>'+title+' '+val+dup+assisted+'</h3>'+
    '<div class=meta><span class=price>'+money(v.price)+'</span> · '+esc(v.generation||'gen?')+' · '+
      (v.mileage?v.mileage.toLocaleString()+' mi':'mileage?')+' · '+esc(v.transmission||'trans?')+' · '+
      esc(v.body_style||'body?')+'</div>'+
    '<div class=meta>'+esc([v.seller_city,v.seller_state].filter(Boolean).join(', ')||'location?')+
      ' · '+esc(v.seller_type||'seller?')+' · VIN '+esc(v.vin||'unknown')+'</div>'+
    '<div class=meta>source <b>'+esc(v.source_key)+'</b> · '+
      (v.days_on_market!=null?Math.round(v.days_on_market)+'d on market':'')+
      (v.price_reduced?' · <span class="pill warn">−'+money(v.price_reduction_total)+'</span>':'')+
      ' · recon~'+money(v.recon_estimate)+' · ship~'+money(v.shipping_estimate)+'</div>'+
    '<div class=meta><a href="'+esc(v.url)+'" target=_blank rel=noopener>Open seller listing ↗</a>'+
      ' · <a href="#" onclick="openRecon('+v.id+');return false">Recon costs</a>'+
      ' · <a href="#" onclick="openStage('+v.id+');return false">Set stage'+(v.deal_stage?' ('+esc(v.deal_stage)+')':'')+'</a></div>'+
    '</div></div>';
}

async function apply(){
  const q=new URLSearchParams({f:JSON.stringify(STATE.filters),sort:STATE.sort});
  const d=await api('/api/inventory?'+q.toString());
  $('#count').textContent=d.count+' vehicle'+(d.count===1?'':'s');
  renderSummary(d.summary);
  $('#results').innerHTML=d.results.map(card).join('')||'<div class=card>No vehicles match these filters.</div>';
}

function reset(){STATE={filters:{price_max:BOOT.default_price_max},sort:'newest'};$('#sort').value='newest';buildFilters();apply();}

async function loadNotifs(){const d=await api('/api/notifications');
  $('#badge').style.display=d.unread?'':'none';$('#badge').textContent=d.unread;
  $('#notifList').innerHTML=d.items.map(n=>'<div class="notif'+(n.read?'':' unread')+'">'+
    '<b>'+esc(n.title)+'</b><div class=meta>'+esc(n.body||'')+' · '+esc(n.kind)+' · '+esc(n.search_name||'')+'</div></div>').join('')
    ||'<div class=meta>No notifications yet. Save a search with alerts, then Refresh.</div>';}

async function openRecon(id){
  const d=await api('/api/recon?listing_id='+id);
  let rows=OPT.recon_categories.map(c=>{const line=(d.lines||[]).find(l=>l.category===c.key)||{};
    return '<div class=reconrow><label>'+esc(c.label)+'</label>'+
      '<input type=number id="rc_'+c.key+'" placeholder=0 value="'+(line.amount??'')+'">'+
      '<select id="rb_'+c.key+'"><option value=estimate'+(line.basis==='estimate'?' selected':'')+'>estimate</option>'+
      '<option value=quote'+(line.basis==='quote'?' selected':'')+'>quote</option>'+
      '<option value=invoice'+(line.basis==='invoice'?' selected':'')+'>invoice</option></select></div>';}).join('');
  $('#modal').innerHTML='<h3>Reconditioning costs — listing #'+id+'</h3>'+
    '<div class=meta>Enter your friend’s real numbers. Tag each as estimate, written quote, or paid invoice. '+
    'A recorded total replaces the generic repair assumption for this car.</div>'+rows+
    '<div style="margin-top:10px"><button class=primary onclick="saveRecon('+id+')">Save</button> '+
    '<button onclick="closeModal()">Close</button> <span class=meta>Total: '+money(d.summary.total)+
    ' ('+esc(d.summary.weakest_basis||'none')+')</span></div>';
  $('#modalBg').classList.add('open');
}
async function saveRecon(id){
  for(const c of OPT.recon_categories){const amt=$('#rc_'+c.key).value;
    if(amt!==''){await api('/api/recon',{method:'POST',body:JSON.stringify(
      {listing_id:id,category:c.key,amount:+amt,basis:$('#rb_'+c.key).value})});}}
  closeModal();apply();
}
const IMPORT_FIELDS=[['url','Listing URL (required)'],['title','Title (e.g. 2008 Porsche 911 Carrera S)'],
 ['price','Asking price'],['year','Year'],['variant','Variant'],['mileage','Mileage'],
 ['vin','VIN (optional)'],['state','State (2-letter)'],['city','City'],
 ['exterior_color','Exterior colour'],['image','Photo URL'],['notes','Notes']];
function openImport(pre){
  pre=pre||{};
  const rows=IMPORT_FIELDS.map(([k,lab])=>
    '<div style="margin:5px 0"><div class=grp-label>'+lab+'</div>'+
    '<input id="im_'+k+'" type="text" value="'+esc(pre[k]||'')+'"></div>').join('');
  $('#modal').innerHTML='<h3>Import a listing (assisted)</h3>'+
    '<div class=meta>For Facebook Marketplace and other private listings with no '+
    'authorized feed. Enter what you can see (or use the one-click bookmarklet — '+
    'see FACEBOOK_IMPORT.md). The original URL is preserved; missing fields stay '+
    'unknown. Non-911s are rejected. This is an ASSISTED import, kept distinct '+
    'from automatic dealer discovery.</div>'+rows+
    '<div style="margin-top:10px"><button class=primary onclick="doImport()">Import</button> '+
    '<button onclick="closeModal()">Cancel</button></div><div id=imMsg class=meta></div>';
  $('#modalBg').classList.add('open');
}
async function doImport(){
  const b={}; IMPORT_FIELDS.forEach(([k])=>{const v=$('#im_'+k).value.trim(); if(v)b[k]=v;});
  if(!b.url){$('#imMsg').textContent='A listing URL is required.';return;}
  const r=await api('/api/import',{method:'POST',body:JSON.stringify(b)});
  if(r.error){$('#imMsg').textContent='Rejected: '+r.error;return;}
  $('#imMsg').textContent=(r.created?'Imported':'Updated')+' listing #'+r.id+' (source: '+r.source+'). '+
    (r.missing&&r.missing.length?'Missing: '+r.missing.join(', '):'');
  await apply();
}
function openStage(id){
  const opts=['','needs_inspection','contacted_seller','offer_submitted','acquired','reconditioning','listed_for_resale','sold']
    .map(s=>'<option value="'+s+'">'+(s||'(none)')+'</option>').join('');
  $('#modal').innerHTML='<h3>Acquisition stage — listing #'+id+'</h3><select id=stageSel>'+opts+'</select>'+
    '<div style="margin-top:10px"><button class=primary onclick="saveStage('+id+')">Save</button> <button onclick="closeModal()">Close</button></div>';
  $('#modalBg').classList.add('open');
}
async function saveStage(id){await api('/api/stage',{method:'POST',body:JSON.stringify({listing_id:id,deal_stage:$('#stageSel').value||null})});closeModal();apply();}
function closeModal(){$('#modalBg').classList.remove('open');}

async function saveSearch(){
  const name=prompt('Name this search (e.g. "Manual 997 under $60k"):');if(!name)return;
  const notify={};
  ['new_match','below_threshold','price_drop','auction_soon','meets_profit'].forEach(k=>{
    notify[k]=confirm('Alert on '+k.replace('_',' ')+'?');});
  await api('/api/searches',{method:'POST',body:JSON.stringify({name,filters:STATE.filters,sort:STATE.sort,notify})});
  await loadSearches();alert('Saved. Alerts recompute when you press Refresh.');
}
async function loadSearches(){const d=await api('/api/searches');
  $('#savedSel').innerHTML='<option value="">Saved searches…</option>'+
    d.map(s=>'<option value="'+s.id+'">'+esc(s.name)+'</option>').join('');
  window._searches=d;}

async function init(){
  OPT=await api('/api/options');
  $('#hsub').textContent=OPT.counts.total_active+' active · '+OPT.counts.unique_vins+' unique VINs · dest '+OPT.destination_state;
  $('#coverage').textContent=' · dealers: '+(OPT.dealer_coverage||[]).map(c=>c.host+'('+c.active_listings+')').join(', ');
  $('#sort').innerHTML=SORTS.map(([v,l])=>'<option value="'+v+'">'+l+'</option>').join('');
  $('#sort').onchange=e=>{STATE.sort=e.target.value;apply();};
  $('#btnReset').onclick=reset;$('#btnSave').onclick=saveSearch;
  $('#btnImport').onclick=()=>openImport();
  // One-click bookmarklet hands data via the URL fragment (#import=<json>) so it
  // works cross-origin from facebook.com without any CORS or automated access.
  if(location.hash.startsWith('#import=')){
    try{const data=JSON.parse(decodeURIComponent(location.hash.slice(8)));
        history.replaceState(null,'',location.pathname); openImport(data);}catch(e){}
  }
  $('#btnRefresh').onclick=async()=>{const r=await api('/api/alerts/run',{method:'POST'});await loadNotifs();await apply();};
  $('#btnBell').onclick=()=>{$('#notifPanel').classList.toggle('open');loadNotifs();};
  $('#notifPanel').onclick=e=>{if(e.target.id==='notifPanel')e.target.classList.remove('open');};
  $('#markRead').onclick=async()=>{await api('/api/notifications/read',{method:'POST',body:'{}'});loadNotifs();};
  $('#modalBg').onclick=e=>{if(e.target.id==='modalBg')closeModal();};
  $('#btnFilters').onclick=()=>$('#aside').classList.toggle('open');
  if(window.innerWidth<=820)$('#btnFilters').style.display='';
  $('#savedSel').onchange=e=>{const s=(window._searches||[]).find(x=>x.id==e.target.value);
    if(s){STATE.filters=JSON.parse(JSON.stringify(s.filters));STATE.sort=s.sort||'newest';$('#sort').value=STATE.sort;buildFilters();apply();}};
  buildFilters();await apply();await loadNotifs();
  if(BOOT.include_synthetic)$('#banners').innerHTML='<div class="banner bad"><b>PREVIEW — SYNTHETIC DATA.</b> Not for buying decisions.</div>';
}
init();
"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def serve(db_path, host="127.0.0.1", port=8000, destination_state="OH",
          include_synthetic: bool = False):
    def handler_factory():
        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, body, ctype="application/json", code=200):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, default=str).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self):
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                conn = _db.connect(db_path)
                try:
                    if u.path in ("/", "/index.html"):
                        self._send(render(conn, destination_state, include_synthetic),
                                   "text/html; charset=utf-8")
                    elif u.path == "/api/options":
                        self._send(options_payload(conn, destination_state))
                    elif u.path == "/api/inventory":
                        f = json.loads(qs.get("f", ["{}"])[0] or "{}")
                        sort = qs.get("sort", ["newest"])[0]
                        self._send(inventory_payload(conn, f, sort, destination_state,
                                                     include_synthetic))
                    elif u.path == "/api/searches":
                        self._send(_searches.list_searches(conn))
                    elif u.path == "/api/notifications":
                        self._send({"unread": _searches.unread_count(conn),
                                    "items": _searches.list_notifications(conn)})
                    elif u.path == "/api/recon":
                        lid = int(qs.get("listing_id", ["0"])[0])
                        self._send({"lines": _recon.get_costs(conn, lid),
                                    "summary": _recon.summary(conn, lid)})
                    else:
                        self._send({"error": "not found"}, code=404)
                except Exception as exc:  # keep the server alive, report as JSON
                    self._send({"error": f"{type(exc).__name__}: {exc}"}, code=500)
                finally:
                    conn.close()

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                conn = _db.connect(db_path)
                try:
                    body = self._read_json()
                    if u.path == "/api/searches":
                        sid = _searches.save_search(conn, body["name"],
                                                    body.get("filters", {}),
                                                    body.get("sort"),
                                                    body.get("notify", {}))
                        self._send({"id": sid})
                    elif u.path == "/api/searches/delete":
                        _searches.delete_search(conn, int(body["id"]))
                        self._send({"ok": True})
                    elif u.path == "/api/alerts/run":
                        self._send(_searches.run_alerts(conn, destination_state))
                    elif u.path == "/api/notifications/read":
                        _searches.mark_read(conn, body.get("id"))
                        self._send({"ok": True})
                    elif u.path == "/api/recon":
                        _recon.set_cost(conn, int(body["listing_id"]), body["category"],
                                        float(body["amount"]),
                                        body.get("basis", "estimate"), body.get("note"))
                        self._send({"ok": True, "summary": _recon.summary(conn, int(body["listing_id"]))})
                    elif u.path == "/api/import":
                        self._send(_assisted_import(conn, body))
                    elif u.path == "/api/stage":
                        _set_stage(conn, int(body["listing_id"]), body.get("deal_stage"))
                        self._send({"ok": True})
                    elif u.path == "/api/condition":
                        _set_condition(conn, int(body["listing_id"]), body["field"], body.get("value"))
                        self._send({"ok": True})
                    else:
                        self._send({"error": "not found"}, code=404)
                except Exception as exc:
                    self._send({"error": f"{type(exc).__name__}: {exc}"}, code=400)
                finally:
                    conn.close()

            def log_message(self, *a):
                pass
        return Handler

    with socketserver.ThreadingTCPServer((host, port), handler_factory()) as httpd:
        httpd.daemon_threads = True
        print(f"Dashboard on http://{host}:{port}  (destination state: {destination_state})")
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def _assisted_import(conn, body: dict) -> dict:
    """Operator-approved import of a single listing the operator is viewing
    (e.g. a Facebook Marketplace car). Distinguished from automatic discovery by
    its source (facebook_marketplace / other manual) and data_source_note. Runs
    through the same 911 guard, so a non-911 is rejected here too.
    """
    url = (body.get("url") or "").strip()
    if not url:
        return {"error": "A listing URL is required."}
    def _num(v):
        try:
            return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "") else None
        except ValueError:
            return None
    try:
        lid, created, report = _manual.add_listing(
            conn, url,
            title=body.get("title") or None,
            year=int(body["year"]) if str(body.get("year") or "").isdigit() else None,
            variant=body.get("variant") or None,
            body_style=body.get("body_style") or None,
            transmission=body.get("transmission") or None,
            mileage=int(_num(body.get("mileage"))) if _num(body.get("mileage")) else None,
            vin=body.get("vin") or None,
            price=_num(body.get("price")),
            seller_type=body.get("seller_type") or "private",
            seller_city=body.get("city") or None,
            seller_state=body.get("state") or None,
            exterior_color=body.get("exterior_color") or None,
            notes=body.get("notes") or None,
            photos=[body["image"]] if body.get("image") else None,
            decode_vin=bool(body.get("vin")))
    except _manual.NotA911 as exc:
        return {"error": str(exc), "rejected": True}
    return {"ok": True, "id": lid, "created": created,
            "source": report.get("source_key"),
            "notices": report.get("notices", []),
            "missing": report.get("missing_fields", [])}


def _set_stage(conn, listing_id: int, stage: str | None) -> None:
    if stage and stage not in _tax.DEAL_STAGES:
        raise ValueError(f"unknown stage '{stage}'")
    conn.execute("UPDATE listings SET deal_stage=?, stage_updated_at=? WHERE id=?",
                 (stage, _db.utcnow(), listing_id))
    conn.commit()


def _set_condition(conn, listing_id: int, field: str, value) -> None:
    if field not in _CONDITION_FIELDS:
        raise ValueError(f"field '{field}' is not operator-settable")
    allowed = _CONDITION_FIELDS[field]
    if allowed is not None and value not in (None, "") and value not in allowed:
        raise ValueError(f"{field} must be one of {allowed}")
    conn.execute(f"UPDATE listings SET {field}=? WHERE id=?", (value or None, listing_id))
    conn.commit()

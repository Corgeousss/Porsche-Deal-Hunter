-- Porsche 911 acquisition platform -- SQLite schema.
-- Every row that describes a real-world vehicle or sale carries provenance:
-- where it came from, when it was observed, and whether it was verified.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Source registry + access tracking (assignment item 8)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    key                  TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    access_method        TEXT NOT NULL,   -- api | rss | jsonld | manual | email_alert
    authorized           TEXT NOT NULL,   -- yes | yes_with_credentials | manual_only | prohibited
    requires_credentials INTEGER NOT NULL DEFAULT 0,
    cost_notes           TEXT,
    terms_url            TEXT,
    limitations          TEXT,
    enabled              INTEGER NOT NULL DEFAULT 0,
    last_success_at      TEXT,
    last_attempt_at      TEXT,
    last_error           TEXT,

    -- Proof that a real call to a real endpoint succeeded. Set ONLY by a
    -- successful live validation run -- never by a unit test, never by hand.
    live_verified_at     TEXT,
    live_verified_note   TEXT
);

-- One row per ingestion attempt, so "when did we last see this source" is always answerable.
CREATE TABLE IF NOT EXISTS source_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key    TEXT NOT NULL REFERENCES sources(key),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running | ok | error | skipped
    items_seen    INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_key ON source_runs(source_key, started_at DESC);

-- ---------------------------------------------------------------------------
-- Listings (assignment item 3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key        TEXT NOT NULL REFERENCES sources(key),
    source_listing_id TEXT,
    url               TEXT NOT NULL UNIQUE,
    title             TEXT,

    -- specifications
    year              INTEGER,
    model             TEXT DEFAULT '911',
    generation        TEXT,              -- 996, 997.1, 991.2, ...
    variant           TEXT,              -- Carrera, Carrera S, Turbo, GT3, ...
    body_style        TEXT,              -- coupe, cabriolet, targa
    transmission      TEXT,              -- manual, tiptronic, pdk, unknown
    drivetrain        TEXT,              -- rwd, awd
    engine            TEXT,
    exterior_color    TEXT,
    interior_color    TEXT,

    mileage           INTEGER,
    mileage_unit      TEXT DEFAULT 'mi',
    vin               TEXT,

    price             REAL,
    currency          TEXT DEFAULT 'USD',
    listing_type      TEXT,              -- fixed | auction | unknown
    auction_ends_at   TEXT,

    seller_type       TEXT,              -- private | dealer | auction_house | unknown
    seller_name       TEXT,
    seller_city       TEXT,
    seller_state      TEXT,
    seller_zip        TEXT,

    status            TEXT NOT NULL DEFAULT 'active',  -- active | sold | removed | stale
    listing_date      TEXT,              -- when the source first listed it, if known
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,     -- refresh timestamp of the most recent read
    data_source_note  TEXT,              -- e.g. "operator-entered from listing page"
    raw               TEXT,              -- raw payload as JSON, for auditing
    notes             TEXT,

    -- Operator-managed acquisition pipeline + condition/history (Tasks 2D, 2G).
    -- All default NULL = UNKNOWN. Missing is never read as a verified negative.
    deal_stage        TEXT,              -- needs_inspection | contacted_seller | ... | sold
    stage_updated_at  TEXT,
    title_status      TEXT,              -- clean | salvage | rebuilt  (NULL = unknown)
    accident_history  TEXT,              -- none | reported          (NULL = unknown)
    owners_count      INTEGER,
    service_records   TEXT,              -- yes | no                 (NULL = unknown)
    recent_major_service TEXT,           -- yes | no                 (NULL = unknown)
    known_issues      TEXT,              -- free text, operator-entered
    cosmetic_condition TEXT,             -- free text, operator-entered
    ppi_done          TEXT,              -- yes | no                 (NULL = unknown)
    seller_docs       TEXT,              -- yes | no                 (NULL = unknown)
    original_status   TEXT,              -- original | modified     (NULL = unknown)
    quarantine_reason TEXT               -- why a row is status='quarantined' (audit trail)
);
CREATE INDEX IF NOT EXISTS idx_listings_gen ON listings(generation, status);
CREATE INDEX IF NOT EXISTS idx_listings_vin ON listings(vin);

CREATE TABLE IF NOT EXISTS listing_photos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT NOT NULL,
    UNIQUE(listing_id, url)
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    price       REAL,
    currency    TEXT DEFAULT 'USD',
    source_key  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id, observed_at);

-- Fields we know we are missing, per listing (assignment item 8).
CREATE TABLE IF NOT EXISTS missing_fields (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    noted_at   TEXT NOT NULL,
    PRIMARY KEY (listing_id, field)
);

-- ---------------------------------------------------------------------------
-- VIN decodes (NHTSA vPIC -- free, authorized, no key)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vin_decodes (
    vin        TEXT PRIMARY KEY,
    decoded_at TEXT NOT NULL,
    decoder    TEXT NOT NULL,           -- nhtsa_vpic | local_wmi
    make       TEXT,
    model      TEXT,
    model_year INTEGER,
    trim       TEXT,
    body_class TEXT,
    engine     TEXT,
    plant      TEXT,
    error_text TEXT,
    raw        TEXT
);

-- ---------------------------------------------------------------------------
-- Comparables: DOCUMENTED COMPLETED SALES ONLY (assignment item 4)
-- A row may only exist if it has a real source URL and a real sale date.
-- is_synthetic=1 is reserved for unit-test fixtures and is excluded from every
-- production valuation path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comps (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation     TEXT NOT NULL,
    variant        TEXT,
    year           INTEGER,
    body_style     TEXT,
    transmission   TEXT,
    mileage        INTEGER,
    sale_price     REAL NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    sale_date      TEXT NOT NULL,        -- ISO date the sale completed
    venue          TEXT,                 -- bring_a_trailer | cars_and_bids | classic_com | dealer | private
    source_key     TEXT NOT NULL,
    source_url     TEXT NOT NULL,        -- must be a real http(s) URL to the completed sale
    vin            TEXT,
    condition_note TEXT,
    includes_fees  INTEGER NOT NULL DEFAULT 0,  -- 1 if sale_price already includes buyer premium

    -- Is this figure a price a buyer actually paid, or an inference?
    -- verified_transaction  -- a documented completed sale at a stated price
    -- last_asking           -- final asking price of a removed listing. NOT a sale.
    -- inferred_from_removal -- listing vanished; a sale is INFERRED. NOT a sale price.
    -- unknown               -- provenance not established
    -- Only 'verified_transaction' is usable by the valuation engine.
    price_basis    TEXT NOT NULL DEFAULT 'unknown',

    -- On what basis are we permitted to hold and use this record?
    -- licensed_api | own_transaction | seller_disclosed | public_record
    -- | operator_asserts_permission | unknown
    -- 'unknown' is excluded from the valuation engine.
    permission_basis TEXT NOT NULL DEFAULT 'unknown',
    permission_note  TEXT,

    is_synthetic   INTEGER NOT NULL DEFAULT 0,
    recorded_at    TEXT NOT NULL,
    recorded_by    TEXT,
    UNIQUE(source_url, vin, sale_date)
);
CREATE INDEX IF NOT EXISTS idx_comps_gen ON comps(generation, variant, sale_date);

-- ---------------------------------------------------------------------------
-- Valuation + deal output
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS valuations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    computed_at  TEXT NOT NULL,
    method       TEXT NOT NULL,
    point_value  REAL,
    low_value    REAL,
    high_value   REAL,
    n_comps      INTEGER NOT NULL DEFAULT 0,
    confidence   TEXT NOT NULL,          -- none | low | medium | high
    status       TEXT NOT NULL,          -- ok | insufficient_comps | missing_inputs
    detail       TEXT                    -- JSON: every comp used + every adjustment
);
CREATE INDEX IF NOT EXISTS idx_valuations_listing ON valuations(listing_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS deals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    valuation_id        INTEGER REFERENCES valuations(id) ON DELETE SET NULL,
    computed_at         TEXT NOT NULL,
    asking_price        REAL,
    expected_resale     REAL,
    total_cost          REAL,
    net_profit          REAL,
    max_purchase_price  REAL,
    meets_threshold     INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,   -- ok | insufficient_comps | missing_inputs
    detail              TEXT             -- JSON: full cost breakdown + explanation
);
CREATE INDEX IF NOT EXISTS idx_deals_listing ON deals(listing_id, computed_at DESC);

-- ---------------------------------------------------------------------------
-- Assumption register (assignment item 8: label all unverified assumptions)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assumptions (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    unit       TEXT NOT NULL,
    basis      TEXT NOT NULL,
    verified   INTEGER NOT NULL DEFAULT 0,
    -- unset       -- has no defensible default; the operator MUST supply it
    --                before a deal can be called fully underwritten
    -- placeholder -- a made-up default so the model runs. Not a quote.
    -- verified    -- the operator supplied a real, confirmed number
    status     TEXT NOT NULL DEFAULT 'placeholder',
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Per-listing overrides: real quotes for a specific car beat the generic
-- placeholder assumptions. Anything set here is treated as VERIFIED.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_overrides (
    listing_id   INTEGER PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    repairs      REAL,
    transport    REAL,
    days_to_sell REAL,
    note         TEXT,
    updated_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Live validation runs: the evidence trail for "does this actually work".
-- One row per check, recording the endpoint called and what came back.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    profile     TEXT NOT NULL,     -- which workflow was run
    passed      INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS validation_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES validation_runs(id) ON DELETE CASCADE,
    step          TEXT NOT NULL,
    status        TEXT NOT NULL,   -- pass | fail | skip
    endpoint      TEXT,            -- the exact URL called, if any
    retrieved_at  TEXT,            -- when the response came back
    http_status   INTEGER,
    detail        TEXT,
    started_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_steps_run ON validation_steps(run_id);

-- ---------------------------------------------------------------------------
-- Confirmed factory options / features, per listing. Populated ONLY from
-- structured, confirmed data (e.g. JSON-LD additionalProperty) -- never guessed
-- from a free-text description. A car with no row for a feature is UNKNOWN for
-- that feature, which is different from "does not have it".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_features (
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    feature_key TEXT NOT NULL,     -- sport_chrono | sport_exhaust | ccb | sport_seats | ...
    present     INTEGER NOT NULL,  -- 1 confirmed present, 0 confirmed absent
    provenance  TEXT NOT NULL,     -- where the confirmation came from
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (listing_id, feature_key)
);

-- ---------------------------------------------------------------------------
-- Saved searches (Task 3). filters is the JSON filter object; notify is the
-- JSON set of alert criteria the operator chose for this search.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS saved_searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    filters     TEXT NOT NULL DEFAULT '{}',
    sort        TEXT,
    notify      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_run_at TEXT
);

-- In-app notification feed (Task 3). dedup_key makes a repeated alert for the
-- same unchanged fact a no-op, so the operator is not spammed.
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    search_id  INTEGER REFERENCES saved_searches(id) ON DELETE SET NULL,
    search_name TEXT,
    listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,     -- new_match | price_drop | below_threshold | auction_soon | meets_profit
    title      TEXT NOT NULL,
    body       TEXT,
    dedup_key  TEXT NOT NULL UNIQUE,
    read       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

-- ---------------------------------------------------------------------------
-- Automatic-refresh bookkeeping. One row per dealer domain, so we always know
-- when each source last refreshed SUCCESSFULLY and can resume oldest-first
-- after an interruption. The scheduler itself lives in Windows Task Scheduler;
-- this table only records what actually ran.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refresh_state (
    domain        TEXT PRIMARY KEY,
    last_attempt  TEXT,
    last_success  TEXT,
    last_status   TEXT,            -- ok | error | skipped
    last_error    TEXT,
    new_count     INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    active_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT,             -- manual | scheduled
    domains_done INTEGER NOT NULL DEFAULT 0,
    new_total    INTEGER NOT NULL DEFAULT 0,
    alerts_created INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'running',
    message      TEXT
);

-- ---------------------------------------------------------------------------
-- Reconditioning cost workflow (Task 6). One row per (listing, category).
-- Every number carries whether it is an estimate, a written quote, or a paid
-- invoice, so the underwriting can tell a guess from a committed cost.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recon_costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,   -- ppi | engine_mechanical | transmission | tires_brakes | ...
    amount      REAL NOT NULL,
    basis       TEXT NOT NULL DEFAULT 'estimate',  -- estimate | quote | invoice
    note        TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE(listing_id, category)
);
CREATE INDEX IF NOT EXISTS idx_recon_listing ON recon_costs(listing_id);

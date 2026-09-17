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
    last_error           TEXT
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
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,     -- refresh timestamp of the most recent read
    data_source_note  TEXT,              -- e.g. "operator-entered from listing page"
    raw               TEXT,              -- raw payload as JSON, for auditing
    notes             TEXT
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

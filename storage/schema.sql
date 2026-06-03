-- Core events table — one row per emitted detection event
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    visitor_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,       -- ISO-8601 UTC
    zone_id         TEXT,
    dwell_ms        INTEGER DEFAULT 0,
    is_staff        INTEGER DEFAULT 0,   -- 0=false 1=true
    confidence      REAL NOT NULL,
    queue_depth     INTEGER,
    sku_zone        TEXT,
    session_seq     INTEGER DEFAULT 0,
    ingested_at     TEXT NOT NULL        -- wall-clock time of ingest
);

CREATE INDEX IF NOT EXISTS idx_events_store_ts     ON events (store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_visitor      ON events (visitor_id);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_store_type   ON events (store_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_zone         ON events (zone_id);

-- POS transactions — one row per unique order (grouped from product-level CSV rows)
CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id  TEXT PRIMARY KEY,    -- order_id from CSV
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,       -- ISO-8601 UTC (converted from order_date+order_time IST)
    basket_value    REAL NOT NULL        -- sum of total_amount for this order_id
);

CREATE INDEX IF NOT EXISTS idx_pos_store_ts ON pos_transactions (store_id, timestamp);
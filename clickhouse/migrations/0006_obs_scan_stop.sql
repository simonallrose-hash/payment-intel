-- object: obs_scan_stop
-- Stop journal: one row per walk that did not reach the payment step (5.2, FR-CW-13).
-- `steps` keeps the walked steps with durations as parallel arrays. 24-month TTL.
CREATE TABLE IF NOT EXISTS obs_scan_stop
(
    scan_date        Date,
    scan_ts          DateTime64(3, 'UTC'),
    scan_run_id      UUID,
    host_id          UInt64,
    etld1            String,
    platform_id      LowCardinality(String),
    adapter          LowCardinality(String),
    stop_step        LowCardinality(String),
    stop_reason      LowCardinality(String),
    stop_detail      String,
    page_url         String,
    element_selector String,
    element_text     String,
    http_status      UInt16,
    steps            Nested(name LowCardinality(String), duration_ms UInt32),
    scanner_version  LowCardinality(String),
    ruleset_version  LowCardinality(String),
    artifact_key     String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (stop_reason, platform_id, scan_ts)
TTL scan_date + INTERVAL 24 MONTH
SETTINGS index_granularity = 8192;

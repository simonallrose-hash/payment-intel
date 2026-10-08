-- object: obs_scan
-- One row per scan run for the quality dashboard (5.2, FR-QA-03). 12-month TTL.
CREATE TABLE IF NOT EXISTS obs_scan
(
    scan_date    Date,
    scan_ts      DateTime64(3, 'UTC'),
    scan_run_id  UUID,
    host_id      UInt64,
    etld1        String,
    scan_type    LowCardinality(String),
    status       LowCardinality(String),
    coverage     LowCardinality(String),
    duration_ms  UInt32,
    blocked_by   LowCardinality(String),
    platform_id  LowCardinality(String),
    adapter      LowCardinality(String),
    scanner_version LowCardinality(String),
    ruleset_version LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (scan_type, status, scan_ts)
TTL scan_date + INTERVAL 12 MONTH
SETTINGS index_granularity = 8192;

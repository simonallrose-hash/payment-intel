-- object: obs_provider
-- Provider observations (5.2, FR-HI-01, FR-DT-07). Immutable rows, 36-month TTL.
-- Denormalised `country`, `platform_id`, `vertical_id` feed mv_market_share_monthly (ADR-0009).
CREATE TABLE IF NOT EXISTS obs_provider
(
    scan_date      Date,
    scan_ts        DateTime64(3, 'UTC'),
    host_id        UInt64,
    etld1          String,
    provider_id    LowCardinality(String),
    role           LowCardinality(String),
    signal_type    LowCardinality(String),
    signal_value   String,
    page_type      LowCardinality(String),
    page_url       String,
    rule_id        LowCardinality(String),
    rule_version   UInt32,
    confidence     LowCardinality(String),
    confidence_score Float32,
    active_on_checkout UInt8,
    evidence_key   String,
    scan_run_id    UUID,
    country        LowCardinality(String),
    platform_id    LowCardinality(String),
    vertical_id    LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (provider_id, host_id, scan_ts)
TTL scan_date + INTERVAL 36 MONTH
SETTINGS index_granularity = 8192;

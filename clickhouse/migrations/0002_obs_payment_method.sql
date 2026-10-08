-- object: obs_payment_method
-- Payment-method observations (5.2). Same shape as obs_provider with method_id.
CREATE TABLE IF NOT EXISTS obs_payment_method
(
    scan_date      Date,
    scan_ts        DateTime64(3, 'UTC'),
    host_id        UInt64,
    etld1          String,
    method_id      LowCardinality(String),
    provider_id    LowCardinality(String),
    signal_type    LowCardinality(String),
    signal_value   String,
    page_type      LowCardinality(String),
    page_url       String,
    rule_id        LowCardinality(String),
    rule_version   UInt32,
    confidence     LowCardinality(String),
    confidence_score Float32,
    evidence_key   String,
    scan_run_id    UUID,
    country        LowCardinality(String),
    platform_id    LowCardinality(String),
    vertical_id    LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (method_id, host_id, scan_ts)
TTL scan_date + INTERVAL 36 MONTH
SETTINGS index_granularity = 8192;

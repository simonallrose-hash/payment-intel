-- object: obs_tech
-- Technology observations incl. versions (5.2). `version` is internal only (AS-23).
CREATE TABLE IF NOT EXISTS obs_tech
(
    scan_date      Date,
    scan_ts        DateTime64(3, 'UTC'),
    host_id        UInt64,
    tech_id        LowCardinality(String),
    version        String,
    confidence     LowCardinality(String),
    confidence_score Float32,
    rule_id        LowCardinality(String),
    rule_version   UInt32,
    page_type      LowCardinality(String),
    scan_run_id    UUID
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (tech_id, host_id, scan_ts)
TTL scan_date + INTERVAL 36 MONTH
SETTINGS index_granularity = 8192;

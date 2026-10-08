-- object: obs_checkout_host
-- Third-party hosts seen on the checkout page (5.2, FR-DT-11). Internal + C2 only
-- except category = 'psp' (AS-23).
CREATE TABLE IF NOT EXISTS obs_checkout_host
(
    scan_date          Date,
    scan_ts            DateTime64(3, 'UTC'),
    host_id            UInt64,
    third_party_host   String,
    third_party_etld1  String,
    category           LowCardinality(String),
    resource_type      LowCardinality(String),
    initiator          String,
    request_count      UInt32,
    first_seen         UInt8,
    scan_run_id        UUID
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(scan_date)
ORDER BY (third_party_etld1, host_id, scan_ts)
TTL scan_date + INTERVAL 36 MONTH
SETTINGS index_granularity = 8192;

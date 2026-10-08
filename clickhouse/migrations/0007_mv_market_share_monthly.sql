-- object: mv_market_share_monthly
-- object: mv_market_share_monthly_mv
-- Monthly market share: unique stores per (month, country, platform, vertical, provider)
-- (5.2). Target table with uniqState(host_id) + materialised view fed by obs_provider.
CREATE TABLE IF NOT EXISTS mv_market_share_monthly
(
    month        Date,
    country      LowCardinality(String),
    platform_id  LowCardinality(String),
    vertical_id  LowCardinality(String),
    provider_id  LowCardinality(String),
    stores       AggregateFunction(uniq, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(month)
ORDER BY (month, country, platform_id, vertical_id, provider_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_market_share_monthly_mv
TO mv_market_share_monthly
AS
SELECT
    toStartOfMonth(scan_date) AS month,
    country,
    platform_id,
    vertical_id,
    provider_id,
    uniqState(host_id) AS stores
FROM obs_provider
WHERE active_on_checkout = 1
GROUP BY month, country, platform_id, vertical_id, provider_id;

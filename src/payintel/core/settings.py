"""Single settings module (NFR-M-02).

Every limit, period, weight, threshold and retention period from the ТЗ lives
here as a field whose default is the value from the ТЗ; the requirement code is
in the field description. Secrets have no defaults and come from the
environment (NFR-S-06): see `.env.example`.

Environment variables use the `PAYINTEL_` prefix and `__` as the nested
delimiter, e.g. `PAYINTEL_SCAN__LIGHT_INTERVAL_DAYS=7`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseModel):
    dsn: str = Field(
        default="postgresql+psycopg://payintel:payintel@127.0.0.1:5432/payintel",
        description="SQLAlchemy DSN; the default targets docker-compose.dev.yml (AS-16).",
    )
    pool_size: int = Field(default=10, ge=1)
    statement_timeout_ms: int = Field(default=30_000, ge=1_000)


class ClickHouseSettings(BaseModel):
    url: str = Field(default="http://127.0.0.1:8123", description="HTTP interface (AS-16).")
    user: str = "payintel"
    password: SecretStr = SecretStr("")
    database: str = "payintel"
    insert_batch_rows: int = Field(
        default=10_000, description="Flush observation batches at N rows (6.5 item 5)."
    )
    insert_batch_seconds: float = Field(
        default=5.0, description="Flush observation batches every N seconds (6.5 item 5)."
    )


class RedisSettings(BaseModel):
    url: str = "redis://127.0.0.1:6379/0"


class S3Settings(BaseModel):
    endpoint: str = "http://127.0.0.1:9002"
    access_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    region: str = "eu-central-1"
    bucket_artifacts: str = "payintel-artifacts"
    bucket_exports: str = "payintel-exports"


class SecretsSettings(BaseModel):
    encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description="base64, 32 bytes; AES-GCM key for TOTP secrets and store-account passwords "
        "(NFR-S-03, FR-CW-12).",
    )
    api_key_pepper: SecretStr = Field(
        default=SecretStr(""), description="Pepper for SHA-256 of API keys (NFR-S-02)."
    )
    session_secret: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")


class IdentitySettings(BaseModel):
    company_domain: str = Field(
        default="example.invalid",
        description="Domain used for synthetic e-mails `checkout-probe+...@<domain>` (FR-CW-05).",
    )
    bot_page_url: str = Field(
        default="https://example.invalid/bot", description="Public bot page (FR-OO-01, LR-02)."
    )
    user_agent_template: str = Field(
        default="PayIntelBot/1.0 (+{bot_page_url})",
        description="Identifying User-Agent (LR-02).",
    )
    company_phone: str = Field(
        default="+44 20 7946 0000",
        description="Phone typed into checkout forms where no fictional range exists for the "
        "country (FR-CW-05); the default is in the Ofcom drama range, never a real number.",
    )
    contact_email: str = Field(
        default="bot@example.invalid", description="Contact on the bot page (FR-OO-01)."
    )
    crawler_ip_ranges: tuple[str, ...] = Field(
        default=(), description="Published egress ranges for the bot page (FR-OO-01)."
    )
    canary_zone: str = Field(
        default="canary.example.invalid",
        description="DNS zone under company control for canary domains (FR-EX-05, FR-AB-04).",
    )

    @property
    def user_agent(self) -> str:
        return self.user_agent_template.format(bot_page_url=self.bot_page_url)


class ScanSettings(BaseModel):
    """Scheduling, retry and politeness defaults (4.2)."""

    light_interval_days_ecommerce: int = Field(default=7, description="FR-SC-03")
    light_interval_days_candidate: int = Field(default=30, description="FR-SC-03")
    checkout_interval_days: int = Field(default=30, description="FR-SC-03")
    checkout_interval_days_watchlist: int = Field(default=7, description="FR-SC-03, FR-AL-02")
    no_dns_recheck_days: int = Field(default=30, description="FR-DS-06")
    blocked_cooldown_days: int = Field(default=30, description="FR-CW-06")
    retry_backoff_hours: tuple[int, ...] = Field(
        default=(1, 6, 24, 72), description="FR-SC-05 exponential retry delays"
    )
    max_failures_before_unreachable: int = Field(default=4, description="FR-SC-05")
    lease_seconds: int = Field(default=600, description="FR-SC-04 task lease timeout")
    max_requests_per_second_per_host: float = Field(default=1.0, description="FR-SC-06")
    max_browser_sessions_per_etld1: int = Field(default=1, description="FR-SC-06")
    max_requests_per_second_per_ip: float = Field(default=5.0, description="FR-SC-06")
    priority_weight_ecommerce: float = Field(default=1.0, description="FR-SC-02 weight")
    priority_weight_traffic_rank: float = Field(default=1.0, description="FR-SC-02 weight")
    priority_weight_has_checkout: float = Field(default=1.0, description="FR-SC-02 weight")
    priority_weight_watchlist: float = Field(default=1.0, description="FR-SC-02 weight")
    priority_weight_staleness: float = Field(default=1.0, description="FR-SC-02 weight")
    excluded_heavy_scan_tlds: tuple[str, ...] = Field(
        default=("ru", "рф", "xn--p1ai", "cn"), description="LR-22 (configurable)"
    )


class DiscoverySettings(BaseModel):
    """Discovery and DNS (4.1)."""

    dns_nameservers: tuple[str, ...] = Field(
        default=("127.0.0.1",), description="FR-DS-06 own recursive resolver (Unbound)"
    )
    dns_port: int = Field(default=5335, ge=1, le=65535, description="Unbound port in compose")
    dns_timeout_seconds: float = Field(default=5.0, gt=0)
    dns_concurrency: int = Field(default=50, ge=1)
    ecommerce_threshold: float = Field(
        default=0.5, ge=0, le=1, description="FR-DS-08 score at/above → ecommerce"
    )
    not_ecommerce_threshold: float = Field(
        default=0.15, ge=0, le=1, description="FR-DS-08 score below → not_ecommerce"
    )
    parking_max_page_bytes: int = Field(
        default=1_500, ge=0, description="FR-DS-07 tiny homepage without links counts as stub"
    )


class LightScanSettings(BaseModel):
    connect_timeout_seconds: float = Field(default=10.0, description="FR-LS-07")
    read_timeout_seconds: float = Field(default=20.0, description="FR-LS-07")
    max_page_bytes: int = Field(default=5 * 1024 * 1024, description="FR-LS-07 (5 MB)")
    max_product_pages: int = Field(default=2, description="FR-LS-01")
    concurrency_per_worker: int = Field(default=200, description="6.1 worker-light")


class CheckoutSettings(BaseModel):
    walk_timeout_seconds: int = Field(default=90, description="FR-CW-10")
    network_idle_timeout_seconds: int = Field(default=15, description="FR-CW-02")
    context_memory_limit_mb: int = Field(default=1024, description="FR-CW-10")
    browser_restart_every_walks: int = Field(default=50, description="FR-CW-10")
    headless: bool = Field(default=True, description="FR-CW-01 browser mode (no stealth)")
    screenshot_max_bytes: int = Field(default=300 * 1024, description="FR-CW-08 (300 KB JPEG)")
    stop_detail_max_chars: int = Field(default=500, description="FR-CW-13")
    action_timeout_seconds: float = Field(
        default=10.0, description="Per click/fill/navigation timeout inside the 90 s walk"
    )
    max_clicks_per_walk: int = Field(
        default=40, description="Hard cap on guarded clicks per walk (loop protection)"
    )
    max_steps_per_checkout: int = Field(
        default=6, description="Max step-button presses between checkout and payment (FR-CW-02)"
    )
    concurrency_per_worker: int = Field(
        default=8, description="Parallel browser contexts per checkout worker (6.1 worker-checkout)"
    )
    viewport_width: int = Field(default=1366, description="Desktop viewport (FR-CW-01)")
    viewport_height: int = Field(default=900, description="Desktop viewport (FR-CW-01)")


class RetentionSettings(BaseModel):
    raw_artifacts_days: int = Field(default=90, description="FR-LS-04, FR-CW-08, LR-08")
    observations_months: int = Field(default=36, description="5.2 TTL, FR-HI-06")
    obs_scan_months: int = Field(default=12, description="5.2 TTL")
    obs_scan_stop_months: int = Field(default=24, description="5.2 TTL")
    audit_log_months: int = Field(default=24, description="FR-AB-01")
    usage_log_months: int = Field(default=24, description="LR-17")
    export_link_hours: int = Field(default=72, description="FR-EX-04")
    optout_apply_hours: int = Field(default=72, description="FR-OO-02")
    dsar_deadline_days: int = Field(default=30, description="FR-OO-03")


class ApiSettings(BaseModel):
    default_rps: int = Field(default=10, description="FR-API-07")
    default_daily_records: int = Field(default=50_000, description="FR-API-07")
    max_page_size: int = Field(default=1_000, description="FR-API-05")
    session_idle_hours: int = Field(default=12, description="FR-UI-04")
    login_max_attempts: int = Field(default=10, description="NFR-S-05")
    login_attempt_window_minutes: int = Field(default=15, description="NFR-S-05")
    default_watchlist_limit: int = Field(default=5_000, description="FR-AL-01")
    webhook_max_attempts: int = Field(default=5, description="FR-AL-04")
    webhook_retry_window_hours: int = Field(default=24, description="FR-AL-04")
    methodology_url: str = Field(
        default="https://example.invalid/methodology", description="FR-API-10"
    )
    default_monthly_records: int = Field(default=1_000_000, description="FR-API-06 default")
    rate_limit_window_seconds: float = Field(
        default=1.0, description="Sliding window for `api_rps` (FR-API-07)"
    )
    cookie_secure: bool = Field(
        default=True, description="`Secure` on the portal session cookie; off only in dev"
    )
    totp_issuer: str = Field(default="PayIntel", description="TOTP issuer label (NFR-S-03)")
    history_page_size: int = Field(default=100, description="Default rows for history/changes")
    api_key_prefix: str = Field(default="pik_", description="Visible prefix of API keys")


class AlertSettings(BaseModel):
    telegram_api_base: str = Field(default="https://api.telegram.org", description="FR-AL-04")
    webhook_timeout_seconds: float = Field(default=10.0, description="FR-AL-04")
    retry_delays_hours: tuple[float, ...] = Field(
        default=(1.0, 3.0, 7.0, 13.0),
        description="Delays before attempts 2..5; sum ≤ 24 h (FR-AL-04)",
    )
    signature_header: str = Field(default="X-PayIntel-Signature", description="HMAC-SHA256")
    digest_hour_utc: int = Field(default=7, ge=0, le=23, description="Daily digest time")


class ExportSettings(BaseModel):
    canary_min: int = Field(default=3, description="FR-EX-05")
    canary_max: int = Field(default=10, description="FR-EX-05")
    default_max_rows: int = Field(default=200_000, description="FR-EX-06 default per export")
    default_schedule: str = Field(default="monthly", description="FR-EX-03")
    rows_per_chunk: int = Field(default=50_000, description="Row batches while building")


class QualitySettings(BaseModel):
    min_psp_precision: float = Field(default=0.95, description="FR-QA-02 / NFR-Q-01 gate")
    target_psp_recall: float = Field(default=0.85, description="NFR-Q-02")
    target_method_precision: float = Field(default=0.90, description="NFR-Q-03")
    target_platform_accuracy: float = Field(default=0.97, description="NFR-Q-04")
    target_country_accuracy: float = Field(default=0.90, description="NFR-Q-05")
    gold_set_target_size: int = Field(default=500, description="FR-QA-01")
    report_min_cell_size: int = Field(default=30, description="FR-RP-03, LR-19")
    stop_reason_other_max_share: float = Field(default=0.05, description="FR-QA-06")
    stop_reason_alert_delta_pp: float = Field(default=5.0, description="FR-QA-06")
    anomaly_removed_multiplier: float = Field(default=3.0, description="FR-QA-04")


class FlagDefaults(BaseModel):
    """Defaults for feature flags (FR-ADM-05). Runtime values live in `feature_flag`."""

    feature_c2_enabled: bool = Field(default=False, description="FR-KYC-07, LR-16")
    allow_shipping_step_fill: bool = Field(default=True, description="AS-21, FR-CW-05")
    allow_account_registration: bool = Field(default=True, description="AS-25, FR-CW-12")
    allow_payment_field_fill: bool = Field(default=True, description="AS-21, FR-CW-14")
    czds_import_enabled: bool = Field(default=False, description="FR-DS-02, AS-22")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAYINTEL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = Field(default="dev", description="dev | test | prod")
    log_level: str = "INFO"
    scanner_version: str = Field(
        default="0.1.0", description="Recorded in obs_scan_stop (FR-CW-13)"
    )

    postgres: PostgresSettings = PostgresSettings()
    clickhouse: ClickHouseSettings = ClickHouseSettings()
    redis: RedisSettings = RedisSettings()
    s3: S3Settings = S3Settings()
    secrets: SecretsSettings = SecretsSettings()
    identity: IdentitySettings = IdentitySettings()
    scan: ScanSettings = ScanSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    light: LightScanSettings = LightScanSettings()
    checkout: CheckoutSettings = CheckoutSettings()
    retention: RetentionSettings = RetentionSettings()
    api: ApiSettings = ApiSettings()
    alerts: AlertSettings = AlertSettings()
    export: ExportSettings = ExportSettings()
    quality: QualitySettings = QualitySettings()
    flags: FlagDefaults = FlagDefaults()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call `get_settings.cache_clear()`."""
    return Settings()

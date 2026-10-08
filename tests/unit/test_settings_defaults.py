"""Every default must equal the value written in the ТЗ (NFR-M-02; self-check item 8)."""

from __future__ import annotations

import pytest

from payintel.core.settings import Settings, get_settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # make sure a developer's .env does not leak into the assertions
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    for key in list(__import__("os").environ):
        if key.startswith("PAYINTEL_"):
            monkeypatch.delenv(key)
    get_settings.cache_clear()
    return get_settings()


def test_scheduling_defaults(settings: Settings) -> None:
    s = settings.scan
    assert s.light_interval_days_ecommerce == 7  # FR-SC-03
    assert s.light_interval_days_candidate == 30  # FR-SC-03
    assert s.checkout_interval_days == 30  # FR-SC-03
    assert s.checkout_interval_days_watchlist == 7  # FR-SC-03
    assert s.no_dns_recheck_days == 30  # FR-DS-06
    assert s.blocked_cooldown_days == 30  # FR-CW-06
    assert s.retry_backoff_hours == (1, 6, 24, 72)  # FR-SC-05
    assert s.max_failures_before_unreachable == 4  # FR-SC-05
    assert s.max_requests_per_second_per_host == 1.0  # FR-SC-06
    assert s.max_browser_sessions_per_etld1 == 1  # FR-SC-06
    assert s.max_requests_per_second_per_ip == 5.0  # FR-SC-06
    assert set(s.excluded_heavy_scan_tlds) >= {"ru", "xn--p1ai", "cn"}  # LR-22


def test_light_scan_defaults(settings: Settings) -> None:
    assert settings.light.connect_timeout_seconds == 10.0  # FR-LS-07
    assert settings.light.read_timeout_seconds == 20.0  # FR-LS-07
    assert settings.light.max_page_bytes == 5 * 1024 * 1024  # FR-LS-07
    assert settings.light.max_product_pages == 2  # FR-LS-01
    assert settings.light.concurrency_per_worker == 200  # 6.1


def test_checkout_defaults(settings: Settings) -> None:
    c = settings.checkout
    assert c.walk_timeout_seconds == 90  # FR-CW-10
    assert c.network_idle_timeout_seconds == 15  # FR-CW-02
    assert c.context_memory_limit_mb == 1024  # FR-CW-10
    assert c.browser_restart_every_walks == 50  # FR-CW-10
    assert c.screenshot_max_bytes == 300 * 1024  # FR-CW-08
    assert c.stop_detail_max_chars == 500  # FR-CW-13


def test_retention_defaults(settings: Settings) -> None:
    r = settings.retention
    assert r.raw_artifacts_days == 90  # LR-08
    assert r.observations_months == 36  # FR-HI-06
    assert r.obs_scan_months == 12
    assert r.obs_scan_stop_months == 24
    assert r.audit_log_months == 24  # FR-AB-01
    assert r.usage_log_months == 24  # LR-17
    assert r.export_link_hours == 72  # FR-EX-04
    assert r.optout_apply_hours == 72  # FR-OO-02
    assert r.dsar_deadline_days == 30  # FR-OO-03


def test_api_and_export_defaults(settings: Settings) -> None:
    a = settings.api
    assert a.default_rps == 10  # FR-API-07
    assert a.default_daily_records == 50_000  # FR-API-07
    assert a.max_page_size == 1_000  # FR-API-05
    assert a.session_idle_hours == 12  # FR-UI-04
    assert (a.login_max_attempts, a.login_attempt_window_minutes) == (10, 15)  # NFR-S-05
    assert a.default_watchlist_limit == 5_000  # FR-AL-01
    assert (a.webhook_max_attempts, a.webhook_retry_window_hours) == (5, 24)  # FR-AL-04
    assert (settings.export.canary_min, settings.export.canary_max) == (3, 10)  # FR-EX-05


def test_quality_defaults(settings: Settings) -> None:
    q = settings.quality
    assert q.min_psp_precision == 0.95  # FR-QA-02
    assert q.target_psp_recall == 0.85  # NFR-Q-02
    assert q.target_method_precision == 0.90  # NFR-Q-03
    assert q.target_platform_accuracy == 0.97  # NFR-Q-04
    assert q.target_country_accuracy == 0.90  # NFR-Q-05
    assert q.gold_set_target_size == 500  # FR-QA-01
    assert q.report_min_cell_size == 30  # FR-RP-03
    assert q.stop_reason_other_max_share == 0.05  # FR-QA-06
    assert q.stop_reason_alert_delta_pp == 5.0  # FR-QA-06
    assert q.anomaly_removed_multiplier == 3.0  # FR-QA-04


def test_flag_defaults(settings: Settings) -> None:
    f = settings.flags
    assert f.feature_c2_enabled is False  # FR-KYC-07, LR-16
    assert f.allow_shipping_step_fill is True  # AS-21
    assert f.allow_account_registration is True  # AS-25
    assert f.allow_payment_field_fill is True  # AS-21 / FR-CW-14
    assert f.czds_import_enabled is False  # AS-22


def test_clickhouse_batching_and_identity(settings: Settings) -> None:
    assert settings.clickhouse.insert_batch_rows == 10_000  # 6.5
    assert settings.clickhouse.insert_batch_seconds == 5.0  # 6.5
    assert settings.identity.user_agent.startswith("PayIntelBot/1.0 (+")  # LR-02


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYINTEL_SCAN__CHECKOUT_INTERVAL_DAYS", "14")
    get_settings.cache_clear()
    assert get_settings().scan.checkout_interval_days == 14

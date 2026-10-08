"""Stop records stay inside the taxonomy (FR-CW-13) and map failures to codes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from payintel.core.reference_loader import load_reference
from payintel.crawl.checkout.stops import (
    StopContext,
    artifact_keys,
    normalise_stop,
    stop_from_exception,
    stop_row,
)
from payintel.crawl.checkout.types import StepTiming, Stop, WalkStep
from payintel.history.writer import OBS_COLUMNS

TAX = load_reference().stop_reasons


def test_normalise_keeps_valid_and_coerces_unknown_codes() -> None:
    s = normalise_stop(
        Stop(WalkStep.PROTECTION, "captcha", "  h  captcha "), TAX, detail_max_chars=500
    )
    assert s.reason == "captcha" and s.detail == "h captcha"
    s = normalise_stop(Stop(WalkStep.CART, "captcha", "x"), TAX, detail_max_chars=500)
    assert s.reason == "other" and s.detail == "captcha: x"  # wrong step
    s = normalise_stop(Stop(WalkStep.CART, "made_up", ""), TAX, detail_max_chars=500)
    assert s.reason == "other" and s.detail == "made_up"
    s = normalise_stop(Stop(WalkStep.CART, "other", ""), TAX, detail_max_chars=500)
    assert s.detail == "unspecified"
    s = normalise_stop(Stop(WalkStep.CART, "other", "y" * 900), TAX, detail_max_chars=500)
    assert len(s.detail) == 500
    assert TAX.is_valid(s.step.value, s.reason, s.detail)


def test_stop_row_matches_clickhouse_columns() -> None:
    ctx = StopContext(
        scan_run_id=uuid.uuid4(),
        host_id=7,
        etld1="shop.test",
        platform_id="woocommerce",
        adapter="woocommerce",
        scanner_version="0.2.0",
        ruleset_version="2026.10",
        artifact_prefix="checkout/shop.test/x/",
    )
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    stop = Stop(
        WalkStep.SHIPPING, "no_shipping_option", "none", page_url="http://s/", http_status=200
    )
    shot, dom = artifact_keys(ctx.artifact_prefix)
    row = stop_row(
        stop,
        ctx,
        steps=[StepTiming("navigation", 120), StepTiming("product", 80)],
        now=now,
        artifact_key=shot,
    )
    assert row["steps.name"] == ["navigation", "product"]
    assert row["steps.duration_ms"] == [120, 80]
    assert row["artifact_key"] == "checkout/shop.test/x/stop.jpg" and dom.endswith("stop.html.gz")
    assert set(row) == set(OBS_COLUMNS["obs_scan_stop"])


def test_stop_from_exception_classifies() -> None:
    assert stop_from_exception(TimeoutError("Timeout 10000ms exceeded"), "u").reason == "timeout"
    assert stop_from_exception(RuntimeError("Target closed"), "u").reason == "browser_crash"
    assert (
        stop_from_exception(RuntimeError("net::ERR_NAME_NOT_RESOLVED"), "u").reason
        == "navigation_error"
    )
    s = stop_from_exception(ValueError("boom"), "u")
    assert s.reason == "other" and s.step == WalkStep.NAVIGATION and "boom" in s.detail

"""Stop records (FR-CW-13): every walk that did not reach the payment step leaves one.

A stop is validated against `reference/stop_reasons.yaml`: an unknown code
or a code on the wrong step becomes `other` with the original code in the
detail, so the journal always stays within the taxonomy (FR-QA-06 relies on
it). The record carries the evidence keys of the screenshot and the DOM
written next to it (FR-CW-08).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from payintel.core.reference_loader import StopReasonTaxonomy
from payintel.crawl.checkout.types import StepTiming, Stop, WalkStep

SCREENSHOT_NAME = "stop.jpg"
DOM_NAME = "stop.html.gz"


def normalise_stop(stop: Stop, taxonomy: StopReasonTaxonomy, *, detail_max_chars: int) -> Stop:
    """Coerce a stop into the taxonomy; `other` always carries a non-empty detail."""
    detail = " ".join((stop.detail or "").split())
    reason = stop.reason
    if not taxonomy.is_valid(stop.step.value, reason, detail or None):
        original = reason
        reason = "other"
        if original != "other":
            detail = f"{original}: {detail}" if detail else original
    if reason == "other" and not detail:
        detail = "unspecified"
    return Stop(
        step=stop.step,
        reason=reason,
        detail=detail[:detail_max_chars],
        page_url=stop.page_url[:2048],
        element_selector=stop.element_selector[:255],
        element_text=" ".join(stop.element_text.split())[:255],
        http_status=max(0, min(int(stop.http_status or 0), 65535)),
    )


@dataclass(frozen=True)
class StopContext:
    scan_run_id: uuid.UUID
    host_id: int
    etld1: str
    platform_id: str
    adapter: str
    scanner_version: str
    ruleset_version: str
    artifact_prefix: str  # e.g. checkout/<etld1>/<run_id>/


def artifact_keys(prefix: str) -> tuple[str, str]:
    """(screenshot key, DOM key) of a stop's evidence."""
    return f"{prefix}{SCREENSHOT_NAME}", f"{prefix}{DOM_NAME}"


def stop_row(
    stop: Stop,
    ctx: StopContext,
    *,
    steps: list[StepTiming],
    now: datetime,
    artifact_key: str,
) -> dict[str, Any]:
    """One `obs_scan_stop` row (columns of clickhouse/migrations/0006)."""
    return {
        "scan_date": now.date(),
        "scan_ts": now,
        "scan_run_id": ctx.scan_run_id,
        "host_id": ctx.host_id,
        "etld1": ctx.etld1,
        "platform_id": ctx.platform_id,
        "adapter": ctx.adapter,
        "stop_step": stop.step.value,
        "stop_reason": stop.reason,
        "stop_detail": stop.detail,
        "page_url": stop.page_url,
        "element_selector": stop.element_selector,
        "element_text": stop.element_text,
        "http_status": stop.http_status,
        "steps.name": [s.name for s in steps],
        "steps.duration_ms": [max(0, int(s.duration_ms)) for s in steps],
        "scanner_version": ctx.scanner_version,
        "ruleset_version": ctx.ruleset_version,
        "artifact_key": artifact_key,
    }


def stop_from_exception(exc: BaseException, page_url: str) -> Stop:
    """Map an unexpected failure to a navigation-step stop (FR-CW-13 'no silent failures')."""
    name = exc.__class__.__name__
    text = f"{name}: {exc}"[:400]
    low = text.lower()
    if "timeout" in low:
        return Stop(WalkStep.NAVIGATION, "timeout", text, page_url=page_url)
    if "crash" in low or "target closed" in low or "browser has been closed" in low:
        return Stop(WalkStep.NAVIGATION, "browser_crash", text, page_url=page_url)
    if "net::" in low or "navigation" in low:
        return Stop(WalkStep.NAVIGATION, "navigation_error", text, page_url=page_url)
    return Stop(WalkStep.NAVIGATION, "other", text, page_url=page_url)

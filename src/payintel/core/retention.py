"""Retention of raw scan artefacts in S3 (FR-LS-04, FR-CW-08, LR-08; ADR-0036).

Two layers: a bucket lifecycle rule (`ensure_raw_lifecycle`, applied at every worker
start) and an application-side sweep (`sweep_raw_artifacts`, run by `payintel artifacts
sweep` and daily by the exporter) that deletes what is older than
`retention.raw_artifacts_days` whether or not the store executes lifecycle rules.
"""

from __future__ import annotations

from datetime import timedelta

from payintel.core.clock import Clock
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings

RAW_ARTIFACT_PREFIXES: tuple[str, ...] = ("light/", "checkout/")


def ensure_raw_lifecycle(store: ObjectStore, settings: Settings) -> None:
    """(Re)apply the bucket rule: raw HTML/HAR/screenshots expire after the period."""
    store.ensure_lifecycle(
        settings.s3.bucket_artifacts,
        days=settings.retention.raw_artifacts_days,
        prefixes=RAW_ARTIFACT_PREFIXES,
    )


def sweep_raw_artifacts(
    store: ObjectStore,
    settings: Settings,
    *,
    clock: Clock,
    days: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete raw artefacts older than `days` (default: the configured period).

    Returns prefix → number of objects deleted (or counted, with `dry_run`).
    """
    period = settings.retention.raw_artifacts_days if days is None else days
    before = clock.now() - timedelta(days=period)
    return {
        prefix: store.expire_prefix(
            settings.s3.bucket_artifacts, prefix, before=before, dry_run=dry_run
        )
        for prefix in RAW_ARTIFACT_PREFIXES
    }

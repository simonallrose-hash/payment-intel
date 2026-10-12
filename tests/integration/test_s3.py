"""Object store wrapper against an S3-compatible container (AS-16)."""

from __future__ import annotations

import pytest

from payintel.core.errors import StorageError
from payintel.core.s3 import ObjectStore
from payintel.core.settings import S3Settings

pytestmark = pytest.mark.integration


def test_put_get_presign_delete(object_store: ObjectStore, s3_settings: S3Settings) -> None:
    bucket = s3_settings.bucket_artifacts
    object_store.put_bytes(
        bucket, "scan/1/page.html.zst", b"\x28\xb5\x2f\xfd", content_type="application/zstd"
    )
    assert object_store.get_bytes(bucket, "scan/1/page.html.zst") == b"\x28\xb5\x2f\xfd"
    url = object_store.presigned_get_url(bucket, "scan/1/page.html.zst", expires_seconds=72 * 3600)
    assert "X-Amz-Expires=259200" in url  # FR-EX-04: 72 h links
    object_store.delete(bucket, "scan/1/page.html.zst")
    with pytest.raises(StorageError):
        object_store.get_bytes(bucket, "scan/1/page.html.zst")


def test_lifecycle_rule_expires_raw_artifacts(
    object_store: ObjectStore, s3_settings: S3Settings
) -> None:
    """FR-LS-04 / FR-CW-08 / LR-08: raw artefacts live `retention.raw_artifacts_days`."""
    bucket = s3_settings.bucket_artifacts
    object_store.ensure_lifecycle(bucket, days=90, prefixes=("light/", "checkout/"))
    assert object_store.lifecycle_days(bucket) == {"light/": 90, "checkout/": 90}
    object_store.ensure_lifecycle(bucket, days=45, prefixes=("light/",))  # re-applying replaces
    assert object_store.lifecycle_days(bucket) == {"light/": 45}


def test_expire_prefix_deletes_only_older_objects(
    object_store: ObjectStore, s3_settings: S3Settings
) -> None:
    """LR-08 enforced by the application (ADR-0036): objects older than `before` go,
    newer ones and other prefixes stay; `dry_run` only counts."""
    from datetime import UTC, datetime, timedelta

    bucket = s3_settings.bucket_artifacts
    for key in ("light/a.example/1/manifest.json", "light/a.example/1/pages/home.html.gz"):
        object_store.put_bytes(bucket, key, b"{}", content_type="application/json")
    object_store.put_bytes(bucket, "js/ab/abcd.js.gz", b"x", content_type="application/gzip")
    now = datetime.now(UTC)
    assert object_store.expire_prefix(bucket, "light/", before=now - timedelta(days=1)) == 0
    assert object_store.get_bytes(bucket, "light/a.example/1/manifest.json") == b"{}"
    future = now + timedelta(minutes=5)
    assert object_store.expire_prefix(bucket, "light/", before=future, dry_run=True) == 2
    assert object_store.get_bytes(bucket, "light/a.example/1/manifest.json") == b"{}"
    assert object_store.expire_prefix(bucket, "light/", before=future) == 2
    with pytest.raises(StorageError):
        object_store.get_bytes(bucket, "light/a.example/1/manifest.json")
    assert object_store.get_bytes(bucket, "js/ab/abcd.js.gz") == b"x"
    assert object_store.expire_prefix(bucket, "light/", before=future) == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        object_store.expire_prefix(bucket, "light/", before=datetime(2026, 1, 1))
    object_store.delete(bucket, "js/ab/abcd.js.gz")

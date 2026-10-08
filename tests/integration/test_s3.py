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

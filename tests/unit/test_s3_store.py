"""ObjectStore options that need no server: SSE header (NFR-S-07), store factory."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from payintel.core.s3 import ObjectStore, make_object_store
from payintel.core.settings import S3Settings


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def get_bucket_lifecycle_configuration(self, **kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycle")


def test_put_sends_server_side_encryption_when_configured() -> None:
    client = _Client()
    ObjectStore(client, server_side_encryption="AES256").put_bytes(  # type: ignore[arg-type]
        "b", "k", b"x", content_type="text/plain"
    )
    assert client.calls[0]["ServerSideEncryption"] == "AES256"
    ObjectStore(client).put_bytes("b", "k", b"x", content_type="text/plain")  # type: ignore[arg-type]
    assert "ServerSideEncryption" not in client.calls[1]


def test_missing_lifecycle_reads_as_empty() -> None:
    assert ObjectStore(_Client()).lifecycle_days("b") == {}  # type: ignore[arg-type]


def test_factory_passes_the_sse_mode() -> None:
    store = make_object_store(S3Settings(server_side_encryption="aws:kms"))
    assert store._sse == "aws:kms"

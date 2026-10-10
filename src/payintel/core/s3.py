"""S3-compatible object storage client (MinIO) for artifacts and exports (AS-16)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from payintel.core.errors import StorageError
from payintel.core.settings import S3Settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import LifecycleRuleTypeDef


def make_s3_client(settings: S3Settings) -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key.get_secret_value(),
        aws_secret_access_key=settings.secret_key.get_secret_value(),
        region_name=settings.region,
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def make_object_store(settings: S3Settings) -> ObjectStore:
    """Store configured from settings (SSE mode included)."""
    return ObjectStore(
        make_s3_client(settings), server_side_encryption=settings.server_side_encryption
    )


class ObjectStore:
    """Thin wrapper: put/get/presign with timeouts and uniform errors.

    `server_side_encryption` (NFR-S-07) is sent with every put; `ensure_lifecycle`
    provisions the retention of raw artefacts (FR-LS-04, FR-CW-08, LR-08).
    """

    def __init__(self, client: S3Client, *, server_side_encryption: str = "") -> None:
        self._client = client
        self._sse = server_side_encryption

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self._client.head_bucket(Bucket=bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=bucket)
            except ClientError as exc:
                raise StorageError(f"cannot create bucket {bucket}", bucket=bucket) from exc

    def put_bytes(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        extra: dict[str, Any] = {}
        if self._sse:
            extra["ServerSideEncryption"] = self._sse
        try:
            self._client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType=content_type, **extra
            )
        except ClientError as exc:
            raise StorageError("put_object failed", bucket=bucket, key=key) from exc

    def ensure_lifecycle(self, bucket: str, *, days: int, prefixes: Sequence[str]) -> None:
        """Expire objects under each prefix after `days` (raw HTML/HAR/screenshots: 90 days)."""
        rules: list[LifecycleRuleTypeDef] = [
            {
                "ID": f"expire-{prefix.strip('/') or 'all'}",
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "Expiration": {"Days": days},
            }
            for prefix in prefixes
        ]
        try:
            self._client.put_bucket_lifecycle_configuration(
                Bucket=bucket, LifecycleConfiguration={"Rules": rules}
            )
        except ClientError as exc:
            raise StorageError("put_bucket_lifecycle failed", bucket=bucket) from exc

    def lifecycle_days(self, bucket: str) -> dict[str, int]:
        """Prefix → expiration days of the enabled rules (empty when none is configured)."""
        try:
            resp = self._client.get_bucket_lifecycle_configuration(Bucket=bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
                return {}
            raise StorageError("get_bucket_lifecycle failed", bucket=bucket) from exc
        out: dict[str, int] = {}
        for rule in resp.get("Rules", []):
            if rule.get("Status") != "Enabled":
                continue
            prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
            days = rule.get("Expiration", {}).get("Days")
            if days is not None:
                out[prefix] = int(days)
        return out

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise StorageError("get_object failed", bucket=bucket, key=key) from exc
        return response["Body"].read()

    def presigned_get_url(self, bucket: str, key: str, *, expires_seconds: int) -> str:
        """Signed download link with limited lifetime (FR-EX-04: 72 h)."""
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds
        )

    def delete(self, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise StorageError("delete_object failed", bucket=bucket, key=key) from exc

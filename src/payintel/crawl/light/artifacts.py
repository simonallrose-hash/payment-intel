"""Scan artefacts in S3 (FR-LS-02, FR-LS-03, LR-08): sanitised, gzip-compressed, 90-day lifecycle.

Layout under `artifact_prefix = light/<etld1>/<scan_run_id>/`:
  manifest.json          pages, statuses, headers, TLS, timings, script hashes
  pages/<page_type>.html.gz
Scripts are stored once per content hash under `js/<aa>/<sha256>.js.gz`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.assets import HostJsAsset, JsAsset
from payintel.core.s3 import ObjectStore


@dataclass
class ArtifactWriter:
    store: ObjectStore | None
    bucket: str

    def put_json(self, key: str, payload: dict[str, Any]) -> None:
        if self.store is None:
            return
        self.store.put_bytes(
            self.bucket,
            key,
            json.dumps(payload, sort_keys=True).encode(),
            content_type="application/json",
        )

    def put_html(self, key: str, html: str) -> int:
        data = gzip.compress(html.encode("utf-8"), compresslevel=6)
        if self.store is not None:
            self.store.put_bytes(self.bucket, key, data, content_type="application/gzip")
        return len(data)

    def put_script(self, sha256: str, body: bytes) -> str:
        key = f"js/{sha256[:2]}/{sha256}.js.gz"
        if self.store is not None:
            self.store.put_bytes(
                self.bucket,
                key,
                gzip.compress(body, compresslevel=6),
                content_type="application/gzip",
            )
        return key


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_script(
    session: Session,
    writer: ArtifactWriter,
    *,
    host_id: int,
    src_url: str,
    body: bytes,
    content_type: str | None,
    now: datetime,
) -> tuple[str, bool]:
    """Dedup by SHA-256 (FR-LS-03). Returns (sha256, newly_stored)."""
    digest = sha256_hex(body)
    asset = session.get(JsAsset, digest)
    new = asset is None
    if asset is None:
        key = writer.put_script(digest, body)
        session.add(
            JsAsset(
                sha256=digest,
                size_bytes=len(body),
                content_type=(content_type or "")[:128] or None,
                s3_key=key,
                first_seen=now,
                last_seen=now,
            )
        )
    else:
        asset.last_seen = now
    link = session.execute(
        select(HostJsAsset).where(HostJsAsset.host_id == host_id, HostJsAsset.sha256 == digest)
    ).scalar_one_or_none()
    if link is None:
        session.add(
            HostJsAsset(
                host_id=host_id,
                sha256=digest,
                src_url=src_url[:2048],
                first_seen=now,
                last_seen=now,
            )
        )
    else:
        link.last_seen = now
        link.src_url = src_url[:2048]
    session.flush()
    return digest, new

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

from sqlalchemy.dialects.postgresql import insert
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

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        if self.store is not None:
            self.store.put_bytes(self.bucket, key, data, content_type=content_type)

    def put_html(self, key: str, html: str) -> int:
        data = gzip.compress(html.encode("utf-8"), compresslevel=6)
        if self.store is not None:
            self.store.put_bytes(self.bucket, key, data, content_type="application/gzip")
        return len(data)

    @staticmethod
    def script_key(sha256: str) -> str:
        return f"js/{sha256[:2]}/{sha256}.js.gz"

    def put_script(self, sha256: str, body: bytes) -> str:
        key = self.script_key(sha256)
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


@dataclass(frozen=True)
class ScriptMeta:
    """What the network phase keeps about a downloaded script: never the body."""

    src_url: str
    sha256: str
    size_bytes: int
    content_type: str | None
    s3_key: str


def record_asset(session: Session, *, meta: ScriptMeta, now: datetime) -> bool:
    """The `js_asset` row for a body already in S3; concurrent scans that saw the same
    body race on the primary key, so this is an upsert. Returns True when new."""
    stmt = insert(JsAsset).values(
        sha256=meta.sha256,
        size_bytes=meta.size_bytes,
        content_type=(meta.content_type or "")[:128] or None,
        s3_key=meta.s3_key,
        first_seen=now,
        last_seen=now,
    )
    upsert = stmt.on_conflict_do_update(index_elements=["sha256"], set_={"last_seen": now})
    first_seen = session.execute(upsert.returning(JsAsset.first_seen)).scalar_one()
    return bool(first_seen == now)


def record_script(session: Session, *, host_id: int, meta: ScriptMeta, now: datetime) -> bool:
    """Dedup by SHA-256 (FR-LS-03); the object is already in S3. Returns True when new."""
    new = record_asset(session, meta=meta, now=now)
    link = insert(HostJsAsset).values(
        host_id=host_id,
        sha256=meta.sha256,
        src_url=meta.src_url[:2048],
        first_seen=now,
        last_seen=now,
    )
    session.execute(
        link.on_conflict_do_update(
            constraint="uq_host_js_asset_host_id_sha256",
            set_={"last_seen": now, "src_url": meta.src_url[:2048]},
        )
    )
    session.flush()
    return new

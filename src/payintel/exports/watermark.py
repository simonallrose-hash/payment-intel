"""Watermark (FR-EX-05, LR-17): a per-export row order plus an id in the file metadata.

Rows are sorted by `SHA-256(watermark_id || row key)`. The order is unique to
the export and reproducible from the watermark id, so a leaked file can be
matched to the export (and the organisation) even after the metadata was
stripped, as long as the row order survived.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from typing import Any


def order_key(watermark_id: uuid.UUID, row_key: str) -> str:
    return hashlib.sha256(watermark_id.bytes + row_key.encode()).hexdigest()


def order_rows(
    rows: Sequence[dict[str, Any]], watermark_id: uuid.UUID, key: Callable[[dict[str, Any]], str]
) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: order_key(watermark_id, key(r)))


def metadata(
    *,
    export_id: uuid.UUID,
    watermark_id: uuid.UUID,
    org_id: uuid.UUID,
    export_type: str,
    profile: str,
    generated_at: str,
    methodology_url: str,
) -> dict[str, str]:
    return {
        "payintel.export_id": str(export_id),
        "payintel.watermark": str(watermark_id),
        "payintel.org_id": str(org_id),
        "payintel.type": export_type,
        "payintel.profile": profile,
        "payintel.generated_at": generated_at,
        "payintel.methodology": methodology_url,
    }


def order_matches(
    rows: Sequence[dict[str, Any]], watermark_id: uuid.UUID, key: Callable[[dict[str, Any]], str]
) -> bool:
    """True when `rows` are in the order this watermark produces (forensic check)."""
    keys = [order_key(watermark_id, key(r)) for r in rows]
    return keys == sorted(keys)

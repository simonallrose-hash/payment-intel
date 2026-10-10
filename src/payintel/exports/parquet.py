"""Parquet writer with the field dictionary as schema and the watermark as metadata."""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from payintel.exports.schema import Field, arrow_schema

DEFAULT_ROWS_PER_GROUP = 50_000


def write_parquet(
    rows: Sequence[dict[str, Any]],
    fields: tuple[Field, ...],
    metadata: dict[str, str],
    *,
    rows_per_group: int = DEFAULT_ROWS_PER_GROUP,
) -> bytes:
    """Encode `rows` as zstd Parquet, one row group per `rows_per_group` rows.

    Rows are converted group by group, so the peak memory is the row dicts plus one
    group's Arrow buffers, not a second full copy of the table (NFR-P-06: a 1M-row
    snapshot stays inside the exporter's memory limit)."""
    schema = arrow_schema(fields, metadata)
    buf = io.BytesIO()
    step = max(1, rows_per_group)
    with pq.ParquetWriter(buf, schema, compression="zstd") as writer:
        if not rows:
            writer.write_table(pa.Table.from_pydict({f.name: [] for f in fields}, schema=schema))
        for start in range(0, len(rows), step):
            chunk = rows[start : start + step]
            columns = {f.name: [r.get(f.name) for r in chunk] for f in fields}
            writer.write_table(pa.Table.from_pydict(columns, schema=schema))
    return buf.getvalue()


def read_metadata(data: bytes) -> dict[str, str]:
    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items()}


def read_rows(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(io.BytesIO(data)).to_pylist()
    return rows

"""Parquet writer with the field dictionary as schema and the watermark as metadata."""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from payintel.exports.schema import Field, arrow_schema


def write_parquet(
    rows: list[dict[str, Any]], fields: tuple[Field, ...], metadata: dict[str, str]
) -> bytes:
    schema = arrow_schema(fields, metadata)
    columns = {f.name: [r.get(f.name) for r in rows] for f in fields}
    table = pa.Table.from_pydict(columns, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def read_metadata(data: bytes) -> dict[str, str]:
    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items()}


def read_rows(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(io.BytesIO(data)).to_pylist()
    return rows

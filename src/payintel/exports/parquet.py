"""Parquet writer with the field dictionary as schema and the watermark as metadata."""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from typing import Any, BinaryIO

import pyarrow as pa
import pyarrow.parquet as pq

from payintel.exports.schema import Field, arrow_schema

DEFAULT_ROWS_PER_GROUP = 50_000


def write_parquet_to(
    sink: str | BinaryIO,
    rows: Iterable[dict[str, Any]],
    fields: tuple[Field, ...],
    metadata: dict[str, str],
    *,
    rows_per_group: int = DEFAULT_ROWS_PER_GROUP,
) -> int:
    """Stream `rows` into `sink` (a path or binary file) as zstd Parquet, one row group
    per `rows_per_group` rows; returns the number of rows written.

    Rows are consumed and converted group by group, so the peak memory is one group of
    row dicts plus its Arrow buffers, whatever the export size (NFR-P-06, ADR-0037)."""
    schema = arrow_schema(fields, metadata)
    step = max(1, rows_per_group)
    written = 0
    with pq.ParquetWriter(sink, schema, compression="zstd") as writer:
        chunk: list[dict[str, Any]] = []
        for row in rows:
            chunk.append(row)
            if len(chunk) >= step:
                _write_group(writer, chunk, fields, schema)
                written += len(chunk)
                chunk = []
        if chunk:
            _write_group(writer, chunk, fields, schema)
            written += len(chunk)
        if written == 0:
            writer.write_table(pa.Table.from_pydict({f.name: [] for f in fields}, schema=schema))
    return written


def _write_group(
    writer: pq.ParquetWriter,
    chunk: list[dict[str, Any]],
    fields: tuple[Field, ...],
    schema: pa.Schema,
) -> None:
    columns = {f.name: [r.get(f.name) for r in chunk] for f in fields}
    writer.write_table(pa.Table.from_pydict(columns, schema=schema))


def write_parquet(
    rows: Sequence[dict[str, Any]],
    fields: tuple[Field, ...],
    metadata: dict[str, str],
    *,
    rows_per_group: int = DEFAULT_ROWS_PER_GROUP,
) -> bytes:
    """In-memory variant of `write_parquet_to` for small files and tests."""
    buf = io.BytesIO()
    write_parquet_to(buf, rows, fields, metadata, rows_per_group=rows_per_group)
    return buf.getvalue()


def read_metadata(data: bytes) -> dict[str, str]:
    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items()}


def read_rows(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(io.BytesIO(data)).to_pylist()
    return rows

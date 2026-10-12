"""Bounded-memory export assembly (NFR-P-06, ADR-0037): disk sorter and streaming writers."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pyarrow.parquet as pq

from payintel.core.models.base import FieldProfile
from payintel.exports import csv as csv_mod
from payintel.exports import parquet, watermark
from payintel.exports.schema import fields_for
from payintel.exports.stream import SpillSorter


def _rows(n: int) -> list[dict[str, object]]:
    return [{"domain": f"s{i:04d}.de", "country": "DE", "rank": i} for i in range(n)]


def test_sorter_spills_to_disk_and_merges_in_key_order(tmp_path: Path) -> None:
    wm = uuid.uuid4()
    rows = _rows(23)

    def key(r: dict[str, object]) -> str:
        return watermark.order_key(wm, str(r["domain"]))

    with SpillSorter(key, chunk_rows=5, scratch_dir=str(tmp_path)) as sorter:
        sorter.extend(rows[:17])
        for r in rows[17:]:
            sorter.add(r)
        assert sorter.count == 23 and sorter.spilled_chunks == 4  # 20 rows on disk, 3 in memory
        spill_dirs = list(tmp_path.iterdir())
        assert len(spill_dirs) == 1 and len(list(spill_dirs[0].iterdir())) == 4
        merged = list(sorter)
        assert merged == watermark.order_rows(rows, wm, lambda r: str(r["domain"]))
        assert watermark.order_matches(merged, wm, lambda r: str(r["domain"]))
        assert list(sorter) == merged  # iteration is repeatable
    assert list(tmp_path.iterdir()) == []  # the scratch directory is gone


def test_sorter_without_spill_and_empty(tmp_path: Path) -> None:
    with SpillSorter(lambda r: str(r["k"]), chunk_rows=100, scratch_dir=str(tmp_path)) as s:
        s.extend([{"k": "b"}, {"k": "a"}, {"k": "c"}])
        assert s.spilled_chunks == 0 and [r["k"] for r in s] == ["a", "b", "c"]
        assert list(tmp_path.iterdir()) == []
    with SpillSorter(lambda r: str(r["k"]), chunk_rows=1) as empty:
        assert list(empty) == [] and empty.count == 0


def test_sorter_keeps_equal_keys_without_comparing_rows() -> None:
    """Rows with the same key must not be compared with each other (dicts are unorderable)."""
    with SpillSorter(lambda _r: "same", chunk_rows=2) as s:
        s.extend([{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5}])
        assert sorted(r["a"] for r in s) == [1, 2, 3, 4, 5]


def test_parquet_streams_row_groups_to_a_path_and_a_file_object(tmp_path: Path) -> None:
    fields = fields_for("full_snapshot", FieldProfile.C1_BASIC)
    rows = _rows(7)
    path = tmp_path / "x.parquet"
    assert (
        parquet.write_parquet_to(str(path), iter(rows), fields, {"e": "1"}, rows_per_group=3) == 7
    )
    data = path.read_bytes()
    meta = pq.read_metadata(io.BytesIO(data))
    assert meta.num_row_groups == 3 and meta.num_rows == 7
    assert [r["domain"] for r in parquet.read_rows(data)] == [r["domain"] for r in rows]
    assert parquet.read_metadata(data) == {"e": "1"}
    buf = io.BytesIO()
    assert parquet.write_parquet_to(buf, iter(()), fields, {"e": "2"}) == 0
    assert parquet.read_rows(buf.getvalue()) == []
    assert data == parquet.write_parquet(rows, fields, {"e": "1"}, rows_per_group=3)


def test_csv_streams_rows_and_counts_them() -> None:
    fields = fields_for("full_snapshot", FieldProfile.C1_BASIC)
    rows = _rows(4)
    sink = io.StringIO()
    assert csv_mod.write_csv_to(sink, iter(rows), fields, {"export_id": "x"}) == 4
    assert sink.getvalue().encode() == csv_mod.write_csv(rows, fields, {"export_id": "x"})
    meta, parsed = csv_mod.read_csv(sink.getvalue().encode())
    assert meta["export_id"] == "x" and [r["domain"] for r in parsed] == [r["domain"] for r in rows]

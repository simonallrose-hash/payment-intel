"""Bounded-memory assembly of an export (NFR-P-06, ADR-0037).

The watermark (FR-EX-05) is the row order: every row of an export is sorted by
`SHA-256(watermark_id || row key)`. Sorting a million rows used to mean holding
all of them in memory (3.2 GB for a full snapshot). `SpillSorter` is an external
merge sort instead: rows are collected in chunks of `chunk_rows`, each full chunk
is sorted and written to a temporary file, and iteration merges the chunk files
with `heapq.merge`. Peak memory is one chunk of rows plus one row per chunk file.
"""

from __future__ import annotations

import heapq
import pickle
import tempfile
from collections.abc import Callable, Iterable, Iterator
from operator import itemgetter
from pathlib import Path
from typing import Any, cast

Row = dict[str, Any]
_Item = tuple[str, Row]


class SpillSorter:
    """Collect rows, then iterate them in `key` order with bounded memory.

    Use as a context manager (or call `close()`): the temporary directory holding
    the spilled chunks is removed on exit. `count` is the number of rows added.
    """

    def __init__(
        self,
        key: Callable[[Row], str],
        *,
        chunk_rows: int,
        scratch_dir: str | None = None,
    ) -> None:
        self._key = key
        self._chunk_rows = max(1, chunk_rows)
        self._scratch_dir = scratch_dir or None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._buffer: list[_Item] = []
        self._spills: list[Path] = []
        self.count = 0

    def __enter__(self) -> SpillSorter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def spilled_chunks(self) -> int:
        return len(self._spills)

    def add(self, row: Row) -> None:
        self._buffer.append((self._key(row), row))
        self.count += 1
        if len(self._buffer) >= self._chunk_rows:
            self._spill()

    def extend(self, rows: Iterable[Row]) -> None:
        for row in rows:
            self.add(row)

    def _spill(self) -> None:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(
                prefix="payintel-export-", dir=self._scratch_dir
            )
        self._buffer.sort(key=itemgetter(0))
        path = Path(self._tmp.name) / f"chunk-{len(self._spills):05d}.pkl"
        with path.open("wb") as f:
            for item in self._buffer:
                pickle.dump(item, f, protocol=pickle.HIGHEST_PROTOCOL)
        self._spills.append(path)
        self._buffer = []

    @staticmethod
    def _read(path: Path) -> Iterator[_Item]:
        with path.open("rb") as f:
            while True:
                try:
                    # Our own file, written a moment ago by `_spill`, inside a private
                    # temporary directory: not untrusted input.
                    yield cast(_Item, pickle.load(f))  # noqa: S301
                except EOFError:
                    return

    def __iter__(self) -> Iterator[Row]:
        """Rows in key order; the spilled chunks are merged lazily (one row each in memory)."""
        self._buffer.sort(key=itemgetter(0))
        streams: list[Iterator[_Item]] = [self._read(p) for p in self._spills]
        streams.append(iter(self._buffer))
        for _key, row in heapq.merge(*streams, key=itemgetter(0)):
            yield row

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
        self._spills = []
        self._buffer = []

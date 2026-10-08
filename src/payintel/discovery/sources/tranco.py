"""Tranco daily top list: CSV `rank,domain` (FR-DS-01)."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from payintel.core.models.base import DomainSourceKind
from payintel.discovery.sources.base import SourceRecord, register


@register(DomainSourceKind.TRANCO)
def parse_tranco(path: Path) -> Iterator[SourceRecord]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            rank_s, host = row[0].strip(), row[1].strip()
            if not rank_s.isdigit():
                continue  # header or garbage
            yield SourceRecord(hostname=host, rank=int(rank_s))

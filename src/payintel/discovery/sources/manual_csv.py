"""Manual CSV import: one hostname per line, optional header `domain` (FR-DS-01)."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from payintel.core.models.base import DomainSourceKind
from payintel.discovery.sources.base import SourceRecord, register


@register(DomainSourceKind.MANUAL)
def parse_manual(path: Path) -> Iterator[SourceRecord]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            host = row[0].strip()
            if not host or host.lower() in {"domain", "hostname", "host"} or host.startswith("#"):
                continue
            yield SourceRecord(hostname=host)

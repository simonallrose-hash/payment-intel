"""Common Crawl host-level web graph vertices: `<id>\\t<reversed host>` (FR-DS-01).

The monthly `cc-main-YYYY-MM-...-host-vertices.txt.gz` lists hosts in reversed
notation (`com.example.shop`); we flip them back. Plain `.txt` and `.gz` are
accepted.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

from payintel.core.models.base import DomainSourceKind
from payintel.discovery.sources.base import SourceRecord, register


def unreverse(reversed_host: str) -> str:
    return ".".join(reversed(reversed_host.split(".")))


@register(DomainSourceKind.COMMONCRAWL)
def parse_commoncrawl(path: Path) -> Iterator[SourceRecord]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            host = parts[-1].strip()
            if not host or not parts[0].strip().isdigit():
                continue
            yield SourceRecord(hostname=unreverse(host))

"""ICANN CZDS zone files (FR-DS-02, AS-22).

Parses RFC 1035 zone text: `<name> <ttl> IN NS ...`. Only NS owner names are
taken (one per delegated domain). The importer refuses to run unless the
`czds_import_enabled` flag is on; domains known only from this source are
excluded from C1 by `discovery.lineage.czds_only_domain_ids`.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

from payintel.core.models.base import DomainSourceKind
from payintel.discovery.sources.base import SourceRecord, register


@register(DomainSourceKind.CZDS)
def parse_czds(path: Path) -> Iterator[SourceRecord]:
    opener = gzip.open if path.suffix == ".gz" else open
    seen: set[str] = set()
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line or line[0] in ";$":
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            rtype = parts[3].upper() if parts[2].upper() == "IN" else parts[2].upper()
            if rtype != "NS":
                continue
            name = parts[0].rstrip(".").lower()
            if name and name not in seen:
                seen.add(name)
                yield SourceRecord(hostname=name)

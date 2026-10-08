"""Certificate Transparency: JSON lines in certstream shape (FR-DS-01).

Each line: `{"data": {"leaf_cert": {"all_domains": ["a.example", "*.b.example"]}}}`.
The CT client itself (log polling / certstream websocket) writes such files;
wildcards are stripped by normalisation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from payintel.core.models.base import DomainSourceKind
from payintel.discovery.sources.base import SourceRecord, register


@register(DomainSourceKind.CT)
def parse_ct(path: Path) -> Iterator[SourceRecord]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            leaf = doc.get("data", {}).get("leaf_cert", {}) if isinstance(doc, dict) else {}
            domains = leaf.get("all_domains", []) if isinstance(leaf, dict) else []
            for host in domains:
                if isinstance(host, str) and host:
                    yield SourceRecord(hostname=host)

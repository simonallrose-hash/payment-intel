"""IP → ASN lookup from an `ip2asn` range table (FR-OO-04).

The table is the public-domain `ip2asn-combined.tsv` (or `-v4` / `-v6`) from
iptoasn.com: one range per line, tab-separated `range_start range_end
AS_number country_code AS_description`; `AS_number` 0 marks unrouted space.
No external GeoIP/ASN database is bundled: the operator downloads the file
(hourly updates, PDDL licence) and points `scan.asn_table_path` at it.
Lookups are a binary search over the sorted range starts, IPv4 and IPv6 kept
apart.
"""

from __future__ import annotations

import bisect
import gzip
import ipaddress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AsnInfo:
    asn: int
    country: str | None
    name: str


@dataclass(frozen=True)
class _Family:
    starts: list[int]
    ends: list[int]
    infos: list[AsnInfo]

    def lookup(self, value: int) -> AsnInfo | None:
        i = bisect.bisect_right(self.starts, value) - 1
        if i < 0 or value > self.ends[i]:
            return None
        info = self.infos[i]
        return info if info.asn > 0 else None


class AsnTable:
    def __init__(self, rows: list[tuple[str, str, int, str | None, str]]) -> None:
        fam: dict[int, list[tuple[int, int, AsnInfo]]] = {4: [], 6: []}
        for start, end, asn, country, name in rows:
            a, b = ipaddress.ip_address(start), ipaddress.ip_address(end)
            if a.version != b.version or int(b) < int(a):
                raise ValueError(f"bad range {start}-{end}")
            fam[a.version].append((int(a), int(b), AsnInfo(asn, country or None, name)))
        self._fam: dict[int, _Family] = {}
        for version, items in fam.items():
            items.sort(key=lambda t: t[0])
            self._fam[version] = _Family(
                [s for s, _, _ in items], [e for _, e, _ in items], [i for _, _, i in items]
            )
        self.size = sum(len(f.starts) for f in self._fam.values())

    @classmethod
    def from_lines(cls, lines: list[str] | tuple[str, ...]) -> AsnTable:
        rows: list[tuple[str, str, int, str | None, str]] = []
        for raw in lines:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            country = parts[3].strip().upper() if len(parts) > 3 else ""
            name = parts[4].strip() if len(parts) > 4 else ""
            rows.append(
                (
                    parts[0].strip(),
                    parts[1].strip(),
                    int(parts[2]),
                    country if country and country != "NONE" else None,
                    name,
                )
            )
        return cls(rows)

    @classmethod
    def from_path(cls, path: Path) -> AsnTable:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as gz:
                return cls.from_lines(gz.readlines())
        return cls.from_lines(path.read_text(encoding="utf-8").splitlines())

    def lookup(self, ip: str) -> AsnInfo | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        fam = self._fam.get(addr.version)
        return fam.lookup(int(addr)) if fam else None


class EmptyAsnTable(AsnTable):
    def __init__(self) -> None:
        super().__init__([])

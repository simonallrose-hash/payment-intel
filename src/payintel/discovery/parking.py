"""Parking and placeholder detection (FR-DS-07): NS/CNAME suffixes, HTML patterns, page size."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from payintel.core.reference_loader import REFERENCE_DIR, validate_schema


@dataclass(frozen=True)
class ParkingSignature:
    id: str
    kind: str
    pattern: str
    source: str
    needs_verification: bool = False


@dataclass(frozen=True)
class ParkingVerdict:
    parked: bool
    signature_id: str | None = None


class ParkingDetector:
    def __init__(self, path: Path = REFERENCE_DIR / "parking_signatures.yaml") -> None:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        validate_schema(doc, "parking_signatures.schema.json", label=path.name)
        self.signatures = [ParkingSignature(**s) for s in doc["signatures"]]
        active = [s for s in self.signatures if not s.needs_verification]
        self._ns = [s for s in active if s.kind == "ns_suffix"]
        self._cname = [s for s in active if s.kind == "cname_suffix"]
        self._html = [(s, re.compile(s.pattern, re.I)) for s in active if s.kind == "html_pattern"]

    def needing_verification(self) -> list[ParkingSignature]:
        return [s for s in self.signatures if s.needs_verification]

    def check_dns(self, *, ns: list[str] | None, cname: str | None) -> ParkingVerdict:
        for name in ns or []:
            low = name.lower().rstrip(".")
            for sig in self._ns:
                if low == sig.pattern or low.endswith("." + sig.pattern):
                    return ParkingVerdict(True, sig.id)
        if cname:
            low = cname.lower().rstrip(".")
            for sig in self._cname:
                if low == sig.pattern or low.endswith("." + sig.pattern):
                    return ParkingVerdict(True, sig.id)
        return ParkingVerdict(False)

    def check_html(self, html: str, *, link_count: int, max_stub_bytes: int) -> ParkingVerdict:
        for sig, rx in self._html:
            if rx.search(html):
                return ParkingVerdict(True, sig.id)
        if len(html.encode("utf-8", "replace")) <= max_stub_bytes and link_count == 0:
            return ParkingVerdict(True, "stub_page")
        return ParkingVerdict(False)


@lru_cache(maxsize=1)
def get_parking_detector() -> ParkingDetector:
    return ParkingDetector()

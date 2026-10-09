"""ip2asn range table and the ASN politeness policy (FR-OO-04)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from payintel.crawl.asn import AsnTable, EmptyAsnTable
from payintel.scheduler.politeness import AsnDecision, AsnPolicy

LINES = [
    "# ip2asn-combined.tsv excerpt",
    "1.0.0.0\t1.0.0.255\t13335\tUS\tCLOUDFLARENET",
    "1.0.1.0\t1.0.3.255\t0\tNone\tNot routed",
    "203.0.113.0\t203.0.113.255\t64496\tDE\tHOSTER-EXAMPLE",
    "2001:db8::\t2001:db8:ffff:ffff:ffff:ffff:ffff:ffff\t64497\tFR\tEXAMPLE-V6",
    "bad line",
]


def test_lookup_ranges_and_gaps() -> None:
    table = AsnTable.from_lines(LINES)
    assert table.size == 4
    hit = table.lookup("203.0.113.7")
    assert hit is not None and (hit.asn, hit.country, hit.name) == (64496, "DE", "HOSTER-EXAMPLE")
    assert table.lookup("1.0.0.1") is not None and table.lookup("1.0.0.1").asn == 13335  # type: ignore[union-attr]
    assert table.lookup("1.0.2.1") is None  # AS0 = not routed
    assert table.lookup("1.0.4.1") is None  # gap after the last range
    assert table.lookup("203.0.114.1") is None
    v6 = table.lookup("2001:db8::1")
    assert v6 is not None and v6.asn == 64497 and v6.country == "FR"
    assert table.lookup("2001:db9::1") is None
    assert table.lookup("not-an-ip") is None
    assert EmptyAsnTable().lookup("203.0.113.7") is None
    with pytest.raises(ValueError, match="bad range"):
        AsnTable([("203.0.113.9", "203.0.113.1", 1, None, "x")])


def test_from_path_plain_and_gzip(tmp_path: Path) -> None:
    plain = tmp_path / "ip2asn-v4.tsv"
    plain.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    packed = tmp_path / "ip2asn-combined.tsv.gz"
    with gzip.open(packed, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    for path in (plain, packed):
        table = AsnTable.from_path(path)
        assert table.lookup("203.0.113.200") is not None


def test_policy_refreshes_limits_on_schedule() -> None:
    table = AsnTable.from_lines(LINES)
    loads: list[int] = []
    limits: dict[int, tuple[float, float]] = {}
    clock = [100.0]

    def loader() -> dict[int, tuple[float, float]]:
        loads.append(1)
        return dict(limits)

    policy = AsnPolicy(table.lookup, loader, refresh_seconds=60, now=lambda: clock[0])
    assert policy.decide("203.0.113.7") == AsnDecision(64496, 1.0, None)
    assert policy.decide("1.0.0.9") == AsnDecision(13335)
    assert policy.decide("8.8.8.8") == AsnDecision(None)
    assert len(loads) == 1
    limits[64496] = (0.1, 1.0)
    clock[0] += 30
    assert policy.decide("203.0.113.7").factor == 1.0  # not yet refreshed
    clock[0] += 31
    assert policy.decide("203.0.113.7") == AsnDecision(64496, 0.1, 1.0)
    assert len(loads) == 2
    limits.clear()
    assert policy.refresh(force=True) == {} and policy.decide("203.0.113.7").factor == 1.0

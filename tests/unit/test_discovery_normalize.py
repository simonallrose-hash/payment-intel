"""FR-DS-04/05: normalisation, IDNA, PSL split, dedup; source parsers (FR-DS-01/02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from payintel.core.errors import ReferenceError_, ValidationError
from payintel.core.models.base import DomainSourceKind
from payintel.discovery.normalize import normalize_hostname, to_unicode
from payintel.discovery.psl import SuffixList, get_suffix_list
from payintel.discovery.sources import parse_source
from payintel.discovery.sources.commoncrawl import unreverse

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("www.example.com", "example.com"),
        ("https://WWW.Shop.Example.co.uk:8443/path?q=1#x", "shop.example.co.uk"),
        ("*.brand.de", "brand.de"),
        ("münchen.de", "xn--mnchen-3ya.de"),
        ("XN--MNCHEN-3YA.de.", "xn--mnchen-3ya.de"),
        ("user:pw@host.example.com", "host.example.com"),
    ],
)
def test_normalize_ok(raw: str, expected: str) -> None:
    assert normalize_hostname(raw).hostname == expected


@pytest.mark.parametrize(
    "raw", ["", "localhost", "192.168.0.1", "[::1]", "a..b", "-bad.example", "x" * 64 + ".com"]
)
def test_normalize_rejects(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_hostname(raw)


def test_to_unicode_roundtrip() -> None:
    assert to_unicode("xn--mnchen-3ya.de") == "münchen.de"
    assert to_unicode("plain.example") == "plain.example"


def test_psl_split() -> None:
    psl = get_suffix_list()
    assert psl.rule_count > 5000
    s = psl.split("shop.brand.co.uk")
    assert s is not None and (s.etld1, s.public_suffix, s.is_subdomain) == (
        "brand.co.uk",
        "co.uk",
        True,
    )
    s = psl.split("brand.de")
    assert s is not None and s.etld1 == "brand.de" and not s.is_subdomain and s.tld == "de"
    # private-section suffix: each tenant is its own registrable name (FR-DS-05)
    s = psl.split("tenant.myshopify.com")
    assert s is not None and s.etld1 == "tenant.myshopify.com"
    assert psl.split("co.uk") is None  # bare public suffix


def test_psl_refuses_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "psl.dat"
    bad.write_text("com\nnet\n")
    with pytest.raises(ReferenceError_):
        SuffixList(bad)


def test_tranco_parser() -> None:
    recs = list(parse_source(DomainSourceKind.TRANCO, FIX / "tranco_sample.csv"))
    assert [r.rank for r in recs] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert recs[1].hostname == "shop.example.co.uk"


def test_commoncrawl_parser() -> None:
    assert unreverse("uk.co.example.shop") == "shop.example.co.uk"
    recs = list(parse_source(DomainSourceKind.COMMONCRAWL, FIX / "cc_vertices_sample.txt"))
    assert [r.hostname for r in recs] == [
        "shop.example.com",
        "brand.de",
        "shop.example.co.uk",
        "someuser.github.io",
    ]


def test_ct_parser_skips_bad_lines() -> None:
    recs = list(parse_source(DomainSourceKind.CT, FIX / "ct_sample.jsonl"))
    assert [r.hostname for r in recs] == [
        "*.brand.de",
        "checkout.brand.de",
        "new-shop.de",
        "WWW.Example.com",
    ]


def test_manual_and_czds_parsers() -> None:
    assert [
        r.hostname for r in parse_source(DomainSourceKind.MANUAL, FIX / "manual_sample.csv")
    ] == [
        "manual-shop.de",
        "www.other.de",
    ]
    assert [r.hostname for r in parse_source(DomainSourceKind.CZDS, FIX / "czds_sample.zone")] == [
        "zoneonly.shop",
        "storex.shop",
    ]


def test_parse_source_validates_path(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        list(parse_source(DomainSourceKind.TRANCO, tmp_path / "missing.csv"))
    with pytest.raises(ValidationError):
        list(parse_source(DomainSourceKind.CRAWL_LINK, FIX / "manual_sample.csv"))

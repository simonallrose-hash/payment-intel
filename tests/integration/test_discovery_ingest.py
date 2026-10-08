"""Ingest idempotence, dedup by eTLD+1, sub-domain hosts, lineage and CZDS gating."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.errors import ForbiddenError
from payintel.core.flags import FlagService
from payintel.core.models.base import DomainSourceKind
from payintel.core.models.domains import Domain, DomainSource, Host, ImportBatch
from payintel.core.settings import FlagDefaults
from payintel.discovery import lineage
from payintel.discovery.ingest import ingest
from payintel.discovery.sources import SourceRecord, parse_source

pytestmark = pytest.mark.integration
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


def test_ingest_tranco_twice_is_idempotent(db_session: Session, fixed_clock: FixedClock) -> None:
    recs = list(parse_source(DomainSourceKind.TRANCO, FIX / "tranco_sample.csv"))
    r1 = ingest(
        db_session,
        recs,
        source=DomainSourceKind.TRANCO,
        origin="tranco_sample.csv",
        clock=fixed_clock,
    )
    # 8 rows: 2 invalid ("not a host", IP); münchen twice (punycode + unicode) → 1 domain;
    # shop.example.co.uk + brand.de + shop.brand.de + google.com
    assert (r1.records_seen, r1.records_invalid) == (8, 2)
    assert r1.domains_new == 4 and r1.domains_updated == 0
    etld1s = set(db_session.execute(select(Domain.etld1)).scalars())
    assert etld1s == {"google.com", "example.co.uk", "brand.de", "xn--mnchen-3ya.de"}
    # best (lowest) rank wins for the eTLD+1
    brand = db_session.execute(select(Domain).where(Domain.etld1 == "brand.de")).scalar_one()
    assert brand.traffic_rank == 3 and brand.tld == "de"
    hosts = {h.hostname: h.is_primary for h in db_session.execute(select(Host)).scalars()}
    assert hosts["brand.de"] is True and hosts["shop.brand.de"] is False
    assert hosts["shop.example.co.uk"] is False and hosts["example.co.uk"] is True
    assert r1.hosts_new == 6
    batch = db_session.get(ImportBatch, r1.batch_id)
    assert batch is not None and batch.domains_new == 4 and batch.finished_at is not None

    fixed_clock.advance(seconds=3600)
    r2 = ingest(
        db_session,
        recs,
        source=DomainSourceKind.TRANCO,
        origin="tranco_sample.csv",
        clock=fixed_clock,
    )
    assert (r2.domains_new, r2.domains_updated, r2.hosts_new) == (0, 4, 0)
    src = db_session.execute(
        select(DomainSource).where(DomainSource.domain_id == brand.id)
    ).scalar_one()
    assert src.first_seen < src.last_seen and src.batch_id == r2.batch_id
    assert db_session.scalar(select(func.count()).select_from(Domain)) == 4


def test_lineage_czds_only(db_session: Session, fixed_clock: FixedClock) -> None:
    FlagService(db_session, FlagDefaults(), clock=fixed_clock).set(
        "czds_import_enabled", True, actor="test"
    )
    ingest(
        db_session,
        parse_source(DomainSourceKind.CZDS, FIX / "czds_sample.zone"),
        source=DomainSourceKind.CZDS,
        origin="czds_sample.zone",
        clock=fixed_clock,
    )
    ingest(
        db_session,
        [SourceRecord("storex.shop")],
        source=DomainSourceKind.MANUAL,
        origin="manual",
        clock=fixed_clock,
    )
    ids = {e: i for e, i in db_session.execute(select(Domain.etld1, Domain.id))}
    assert lineage.is_czds_only(db_session, ids["zoneonly.shop"])
    assert not lineage.is_czds_only(db_session, ids["storex.shop"])
    visible = set(
        db_session.execute(select(Domain.etld1).where(lineage.c1_visible_clause())).scalars()
    )
    assert visible == {"storex.shop"}
    assert lineage.sources_of(db_session, ids["storex.shop"]) == [
        DomainSourceKind.CZDS,
        DomainSourceKind.MANUAL,
    ]


def test_czds_import_requires_flag(db_session: Session, fixed_clock: FixedClock) -> None:
    with pytest.raises(ForbiddenError, match="czds_import_enabled"):
        ingest(
            db_session,
            [SourceRecord("x.de")],
            source=DomainSourceKind.CZDS,
            origin="z",
            clock=fixed_clock,
        )

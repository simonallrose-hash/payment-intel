"""The NFR-P stand generator (`scripts/synth_dataset.py`) on a fresh database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from payintel.api import read
from payintel.api.auth import keys as keys_mod
from payintel.bench.synthetic import SynthSpec, generate
from payintel.core.clock import FixedClock
from payintel.core.flags import FlagService
from payintel.core.models.base import FieldProfile
from payintel.core.models.domains import Domain
from payintel.core.models.store import StoreProfile, StoreProvider
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.settings import FlagDefaults
from payintel.entitlements.check import resolve_grant
from payintel.entitlements.lineage_guard import c1_visible_clause
from tests.conftest import alembic_config_for

pytestmark = pytest.mark.integration


def test_generate_builds_a_visible_c1_dataset_and_a_working_key(fresh_database: str) -> None:
    alembic_command.upgrade(alembic_config_for(fresh_database), "head")
    engine = create_engine(fresh_database, future=True)
    clock = FixedClock(datetime(2026, 10, 9, 12, 0, tzinfo=UTC))
    reference = load_reference()
    with Session(engine) as session:
        sync_reference(session, reference, clock=clock)
        session.commit()
    spec = SynthSpec(stores=1_500, seed=7)
    result = generate(
        engine, spec, clock=clock, pepper="test-pepper", key_prefix="pik_", reference=reference
    )
    assert result.stores == 1_500 and result.providers > 1_500 and result.methods > 0
    with Session(engine) as session:
        visible = session.execute(
            select(func.count())
            .select_from(StoreProfile)
            .join(Domain, Domain.id == StoreProfile.host_id)
            .where(c1_visible_clause())
        ).scalar_one()
        assert visible == 1_500  # every synthetic store is a C1 product (ecommerce, non-CZDS)
        assert session.execute(select(func.count()).select_from(StoreProvider)).scalar_one() == (
            result.providers
        )
        principal = keys_mod.authenticate(
            session, result.api_key, pepper="test-pepper", prefix="pik_", now=clock.now(), ip=None
        )
        assert principal.org_id is not None
        grant = resolve_grant(
            session,
            principal.org_id,
            today=clock.now().date(),
            flags=FlagService(session, FlagDefaults(), clock=clock),
        )
        assert grant.profile == FieldProfile.C1_FULL and grant.unrestricted_countries
        rows, cursor = read.search(session, grant, read.StoreFilters(), limit=1000)
        assert len(rows) == 1000 and cursor is not None
        page = read.build_stores(session, rows, profile=grant.profile, methodology_url="m")
        assert len(page) == 1000 and all(p.providers for p in page[:50])
        total, cells = read.market_share(
            session, grant, countries=[], platforms=[], role=None, min_cell=30
        )
        assert total == 1_500 and cells
        # sequences continue after the COPY-loaded ids
        session.execute(
            text("INSERT INTO domain (etld1, tld, status) VALUES ('new.de','de','ecommerce')")
        )
    with pytest.raises(RuntimeError):
        generate(
            engine, spec, clock=clock, pepper="test-pepper", key_prefix="pik_", reference=reference
        )
    again = generate(
        engine,
        SynthSpec(stores=100, seed=7),
        clock=clock,
        pepper="p",
        key_prefix="pik_",
        reset=True,
        reference=reference,
    )
    assert again.stores == 100
    engine.dispose()

"""Lineage queries (FR-DS-03, AS-22, LR-12).

`czds_only_clause()` is the single SQL predicate that the entitlements layer
(stage 3) and exports must apply so that domains known *only* from ICANN CZDS
never reach C1 or module A.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.orm import Session

from payintel.core.models.base import DomainSourceKind
from payintel.core.models.domains import Domain, DomainSource


def czds_only_clause() -> ColumnElement[bool]:
    """True for `Domain` rows whose every source is `czds`."""
    has_czds = exists().where(
        DomainSource.domain_id == Domain.id, DomainSource.source == DomainSourceKind.CZDS
    )
    has_other = exists().where(
        DomainSource.domain_id == Domain.id, DomainSource.source != DomainSourceKind.CZDS
    )
    return has_czds & ~has_other


def c1_visible_clause() -> ColumnElement[bool]:
    """Predicate for domains that may appear in C1 and module A (LR-12)."""
    return ~czds_only_clause()


def is_czds_only(session: Session, domain_id: int) -> bool:
    row = session.execute(
        select(Domain.id).where(Domain.id == domain_id, czds_only_clause())
    ).first()
    return row is not None


def sources_of(session: Session, domain_id: int) -> list[DomainSourceKind]:
    return list(
        session.execute(
            select(DomainSource.source)
            .where(DomainSource.domain_id == domain_id)
            .order_by(DomainSource.first_seen)
        ).scalars()
    )

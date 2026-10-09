"""Rows that may never reach a client (LR-12, AS-22, FR-OO-02, FR-DS-09).

* domains known **only** from ICANN CZDS are excluded from C1 and from module A;
* domains flagged `optout` or with a verified opt-out request are excluded
  (the queue drops them through `scheduler.planner`; this guard drops them
  from every API answer and export immediately, well inside the 72 h);
* only `ecommerce` domains are products of C1.

The clause is combined with the segment clause on every store query, so the
restriction is enforced in the entitlements layer on each request and each
export, as LR-12 requires, not in individual handlers.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, exists, not_, select

from payintel.core.models.base import DomainSourceKind, DomainStatus
from payintel.core.models.domains import Domain, DomainSource
from payintel.scheduler.planner import optout_clause


def non_czds_lineage_clause() -> ColumnElement[bool]:
    return exists(
        select(DomainSource.id).where(
            DomainSource.domain_id == Domain.id,
            DomainSource.source != DomainSourceKind.CZDS,
        )
    )


def c1_visible_clause(*, events: bool = False) -> ColumnElement[bool]:
    """Domain rows a C1 client may see. Join `Domain` before applying.

    With `events=True` (change feeds, alerts) a store that went `unreachable`
    stays visible so its `store_offline` event reaches the client; listings and
    exports keep showing `ecommerce` domains only.
    """
    statuses = [DomainStatus.ECOMMERCE]
    if events:
        statuses.append(DomainStatus.UNREACHABLE)
    return and_(
        Domain.status.in_(statuses),
        not_(optout_clause()),
        non_czds_lineage_clause(),
    )

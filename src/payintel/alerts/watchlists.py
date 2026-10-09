"""Watchlists (FR-AL-01, FR-AL-02).

Domains are normalised to eTLD+1-looking lower-case labels; the entitlement
`watchlist_limit` caps the total number of domains across all lists of the
organisation. Items feed `scheduler.planner` priority (FR-AL-02).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ConflictError, NotFoundError, ValidationError
from payintel.core.models.alerts import Watchlist, WatchlistItem
from payintel.entitlements.check import EntitlementDenied
from payintel.entitlements.model import DenialCode

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalise_domain(raw: str) -> str | None:
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0]
    d = d.removeprefix("www.")
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return d if _DOMAIN.match(d) else None


@dataclass(frozen=True)
class AddResult:
    added: int
    skipped: int
    total: int


def org_total(session: Session, org_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count(WatchlistItem.id))
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(Watchlist.org_id == org_id)
        ).scalar_one()
    )


def item_count(session: Session, watchlist_id: int) -> int:
    return int(
        session.execute(
            select(func.count(WatchlistItem.id)).where(WatchlistItem.watchlist_id == watchlist_id)
        ).scalar_one()
    )


def get_watchlist(session: Session, org_id: uuid.UUID, watchlist_id: int) -> Watchlist:
    wl = session.get(Watchlist, watchlist_id)
    if wl is None or wl.org_id != org_id:
        raise NotFoundError("watchlist not found", id=watchlist_id)
    return wl


def create_watchlist(
    session: Session, *, org_id: uuid.UUID, name: str, actor: str, clock: Clock
) -> Watchlist:
    exists = session.execute(
        select(Watchlist.id).where(Watchlist.org_id == org_id, Watchlist.name == name)
    ).first()
    if exists:
        raise ConflictError("watchlist name already exists", name=name)
    wl = Watchlist(org_id=org_id, name=name, created_at=clock.now())
    session.add(wl)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="watchlist.create",
        object_type="watchlist",
        object_id=str(wl.id),
        after={"org_id": str(org_id), "name": name},
        clock=clock,
    )
    return wl


def add_domains(
    session: Session, wl: Watchlist, domains: list[str], *, limit: int, actor: str, clock: Clock
) -> AddResult:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        d = normalise_domain(raw)
        if d is None or d in seen:
            continue
        seen.add(d)
        cleaned.append(d)
    if not cleaned:
        raise ValidationError("no valid domains")
    existing = set(
        session.execute(
            select(WatchlistItem.domain).where(
                WatchlistItem.watchlist_id == wl.id, WatchlistItem.domain.in_(cleaned)
            )
        ).scalars()
    )
    new = [d for d in cleaned if d not in existing]
    if org_total(session, wl.org_id) + len(new) > limit:
        raise EntitlementDenied(
            DenialCode.WATCHLIST_LIMIT, f"watchlist limit of {limit} domains exceeded", limit=limit
        )
    now = clock.now()
    session.add_all([WatchlistItem(watchlist_id=wl.id, domain=d, added_at=now) for d in new])
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="watchlist.add_domains",
        object_type="watchlist",
        object_id=str(wl.id),
        after={"added": len(new), "skipped": len(domains) - len(new)},
        clock=clock,
    )
    return AddResult(len(new), len(domains) - len(new), item_count(session, wl.id))


def remove_domain(session: Session, wl: Watchlist, domain: str) -> bool:
    d = normalise_domain(domain) or domain
    item = session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id, WatchlistItem.domain == d)
    ).scalar_one_or_none()
    if item is None:
        return False
    session.delete(item)
    session.flush()
    return True


def delete_watchlist(session: Session, wl: Watchlist, *, actor: str, clock: Clock) -> None:
    audit.record(
        session,
        actor=actor,
        action="watchlist.delete",
        object_type="watchlist",
        object_id=str(wl.id),
        before={"name": wl.name},
        clock=clock,
    )
    session.delete(wl)
    session.flush()


def parse_csv(text: str) -> list[str]:
    """CSV upload (FR-AL-01): first column, header `domain` optional."""
    out: list[str] = []
    for i, line in enumerate(text.splitlines()):
        cell = line.split(",")[0].strip().strip('"')
        if not cell or (i == 0 and cell.lower() == "domain"):
            continue
        out.append(cell)
    return out

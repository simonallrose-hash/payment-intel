"""Server-side portal sessions (FR-UI-04, NFR-S-05).

The cookie holds a random 32-byte token; the database holds its SHA-256,
the owner, the CSRF token and `last_seen_at`. A session is valid while it is
not revoked and was used within `idle_hours`. Every request that passes
`current_session` refreshes `last_seen_at`.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from payintel.core.models.portal import PortalSession

COOKIE_NAME = "pi_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    session: Session, *, user_id: uuid.UUID, ip: str | None, now: datetime
) -> tuple[str, PortalSession]:
    token = secrets.token_urlsafe(32)
    row = PortalSession(
        token_hash=_hash(token),
        user_id=user_id,
        csrf_token=secrets.token_urlsafe(32),
        ip=ip,
        created_at=now,
        last_seen_at=now,
    )
    session.add(row)
    session.flush()
    return token, row


def load_session(
    session: Session, token: str | None, *, now: datetime, idle_hours: int
) -> PortalSession | None:
    if not token:
        return None
    row = session.execute(
        select(PortalSession).where(PortalSession.token_hash == _hash(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.last_seen_at + timedelta(hours=idle_hours) <= now:
        row.revoked_at = now
        session.flush()
        return None
    row.last_seen_at = now
    return row


def revoke(session: Session, row: PortalSession, *, now: datetime) -> None:
    row.revoked_at = now
    session.flush()


def revoke_all(session: Session, user_id: uuid.UUID, *, now: datetime) -> int:
    result = session.execute(
        update(PortalSession)
        .where(PortalSession.user_id == user_id, PortalSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]

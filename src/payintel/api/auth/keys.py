"""API keys (FR-API-02, NFR-S-02).

Format: `<prefix><visible 8 chars>.<secret 43 chars>`; 32 random bytes of
secret. Only the visible part and HMAC-SHA256(pepper, key) are stored; the
full key is returned exactly once at creation.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.crypto import constant_time_equals, hash_api_key
from payintel.core.errors import ValidationError
from payintel.core.models.access import ApiKey
from payintel.core.models.base import Role
from payintel.entitlements.check import EntitlementDenied, ip_allowed
from payintel.entitlements.model import SCOPES, DenialCode, Principal

VISIBLE_CHARS = 8


@dataclass(frozen=True)
class IssuedKey:
    key: ApiKey
    raw: str  # shown once


def _random_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def parse_prefix(raw: str, prefix: str) -> str | None:
    """`pik_abcdefgh.secret…` → `pik_abcdefgh`, or None when malformed."""
    if not raw.startswith(prefix) or "." not in raw:
        return None
    visible, _, secret = raw.partition(".")
    if len(visible) != len(prefix) + VISIBLE_CHARS or len(secret) < 40:
        return None
    return visible


def issue_key(
    session: Session,
    *,
    org_id: uuid.UUID,
    name: str,
    scopes: list[str],
    expires_in_days: int | None,
    allowed_ips: list[str],
    created_by: uuid.UUID | None,
    actor: str,
    pepper: str,
    prefix: str,
    clock: Clock,
) -> IssuedKey:
    unknown = sorted(set(scopes) - SCOPES)
    if unknown:
        raise ValidationError("unknown scopes", scopes=unknown)
    if not scopes:
        raise ValidationError("at least one scope is required")
    visible = prefix + secrets.token_hex(VISIBLE_CHARS // 2)
    raw = f"{visible}.{_random_secret()}"
    now = clock.now()
    row = ApiKey(
        org_id=org_id,
        name=name,
        prefix=visible,
        hash=hash_api_key(raw, pepper),
        scopes=sorted(set(scopes)),
        expires_at=(now + timedelta(days=expires_in_days)) if expires_in_days else None,
        allowed_ips=list(allowed_ips),
        created_by=created_by,
        created_at=now,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="api_key.issue",
        object_type="api_key",
        object_id=str(row.id),
        after={"org_id": str(org_id), "name": name, "scopes": row.scopes, "prefix": visible},
        clock=clock,
    )
    return IssuedKey(row, raw)


def revoke_key(session: Session, key: ApiKey, *, actor: str, clock: Clock) -> None:
    if key.revoked_at is None:
        key.revoked_at = clock.now()
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="api_key.revoke",
            object_type="api_key",
            object_id=str(key.id),
            clock=clock,
        )


def authenticate(
    session: Session, raw: str, *, pepper: str, prefix: str, now: datetime, ip: str | None
) -> Principal:
    """Bearer token → `Principal`; any failure is a 403 with a reason, never a hint."""
    visible = parse_prefix(raw, prefix)
    if visible is None:
        raise EntitlementDenied(DenialCode.KEY_REVOKED, "invalid API key")
    key = session.execute(select(ApiKey).where(ApiKey.prefix == visible)).scalar_one_or_none()
    if key is None or not constant_time_equals(key.hash, hash_api_key(raw, pepper)):
        raise EntitlementDenied(DenialCode.KEY_REVOKED, "invalid API key")
    if key.revoked_at is not None:
        raise EntitlementDenied(DenialCode.KEY_REVOKED, "API key revoked")
    if key.expires_at is not None and key.expires_at <= now:
        raise EntitlementDenied(DenialCode.KEY_EXPIRED, "API key expired")
    if not ip_allowed(ip, key.allowed_ips):
        raise EntitlementDenied(DenialCode.IP_NOT_ALLOWED, "caller IP is not allowed for this key")
    key.last_used_at = now
    return Principal(
        kind="api_key",
        org_id=key.org_id,
        role=Role.API_CLIENT,
        api_key_id=key.id,
        scopes=frozenset(key.scopes),
        ip=ip,
    )

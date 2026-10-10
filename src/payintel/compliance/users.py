"""Portal and staff users (3.2, FR-UI-01, FR-ADM-01, UC-13)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.auth import sessions as sessions_mod
from payintel.api.auth.passwords import hash_password
from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ConflictError, NotFoundError, ValidationError
from payintel.core.models.access import Membership, User
from payintel.core.models.base import Role
from payintel.entitlements.model import ORG_ROLES, STAFF_ROLES, Principal

MIN_PASSWORD = 12
# LR-18 / NFR-S-04: a disabled user is a permanently locked one (no schema change);
# `register_success` never runs for them because the login check comes first.
DISABLED_UNTIL = datetime(9999, 12, 31, tzinfo=UTC)


def create_user(
    session: Session,
    *,
    email: str,
    password: str,
    role: Role,
    org_id: uuid.UUID | None,
    actor: str,
    clock: Clock,
) -> User:
    email = email.strip().lower()
    if "@" not in email:
        raise ValidationError("invalid e-mail")
    if len(password) < MIN_PASSWORD:
        raise ValidationError(f"password must be at least {MIN_PASSWORD} characters")
    if role == Role.API_CLIENT:
        raise ValidationError("api_client is a key role, not a user role")
    if (role in STAFF_ROLES) == (org_id is not None):
        raise ValidationError("staff users have no organisation; org users need one")
    if session.execute(select(User.id).where(User.email == email)).first():
        raise ConflictError("e-mail already registered", email=email)
    user = User(
        email=email,
        password_hash=hash_password(password),
        is_staff=role in STAFF_ROLES,
        created_at=clock.now(),
    )
    session.add(user)
    session.flush()
    session.add(Membership(user_id=user.id, org_id=org_id, role=role, created_at=clock.now()))
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="user.create",
        object_type="user",
        object_id=str(user.id),
        after={"email": email, "role": role.value, "org_id": str(org_id) if org_id else None},
        clock=clock,
    )
    return user


def membership_of(session: Session, user: User) -> Membership:
    m = session.execute(
        select(Membership).where(Membership.user_id == user.id)
    ).scalar_one_or_none()
    if m is None:
        raise NotFoundError("user has no role")
    return m


def principal_for(user: User, membership: Membership, *, ip: str | None) -> Principal:
    return Principal(
        kind="staff" if user.is_staff else "user",
        org_id=membership.org_id,
        role=membership.role,
        user_id=user.id,
        ip=ip,
        email=user.email,
    )


def users_of_org(session: Session, org_id: uuid.UUID) -> list[tuple[User, Membership]]:
    rows = session.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id)
        .order_by(User.email)
    ).all()
    return [(u, m) for u, m in rows]


def staff_users(session: Session) -> list[tuple[User, Membership]]:
    rows = session.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(User.is_staff.is_(True))
        .order_by(User.email)
    ).all()
    return [(u, m) for u, m in rows]


def set_role(
    session: Session, membership: Membership, role: Role, *, actor: str, clock: Clock
) -> None:
    if role not in ORG_ROLES - {Role.API_CLIENT} and role not in STAFF_ROLES:
        raise ValidationError("invalid role")
    before = membership.role.value
    membership.role = role
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="user.set_role",
        object_type="user",
        object_id=str(membership.user_id),
        before={"role": before},
        after={"role": role.value},
        clock=clock,
    )


def is_disabled(user: User) -> bool:
    return user.locked_until is not None and user.locked_until >= DISABLED_UNTIL


def disable_user(session: Session, *, email: str, actor: str, clock: Clock) -> User:
    """Access review (LR-18): lock the account for good and revoke its portal sessions."""
    user = session.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("user not found")
    if is_disabled(user):
        raise ConflictError("user already disabled", email=user.email)
    now = clock.now()
    user.locked_until = DISABLED_UNTIL
    revoked = sessions_mod.revoke_all(session, user.id, now=now)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="user.disable",
        object_type="user",
        object_id=str(user.id),
        after={"email": user.email, "sessions_revoked": revoked},
        clock=clock,
    )
    return user


def all_users(session: Session) -> list[tuple[User, Membership]]:
    rows = session.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .order_by(User.email)
    ).all()
    return [(u, m) for u, m in rows]

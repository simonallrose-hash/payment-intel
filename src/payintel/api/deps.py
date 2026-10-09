"""Application state and FastAPI dependencies shared by /v1, portal and admin.

`AppState` is built once in `create_app`; tests construct it with a session
factory bound to their transactional connection, a `FixedClock` and an
in-memory rate-limit store, so nothing here touches global singletons.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from payintel.api.auth import keys as keys_mod
from payintel.api.read import EvidenceSource, NoEvidence
from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.crypto import SecretBox
from payintel.core.errors import ConfigurationError
from payintel.core.flags import FlagService
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.entitlements.check import EntitlementDenied, check_ip, require_scope, resolve_grant
from payintel.entitlements.model import DenialCode, Grant, Principal
from payintel.entitlements.quotas import MemoryCounterStore, RateLimiter, check_record_quota


@dataclass
class AppState:
    settings: Settings
    session_factory: Callable[[], Session]
    clock: Clock = SYSTEM_CLOCK
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(MemoryCounterStore()))
    evidence: EvidenceSource = field(default_factory=NoEvidence)
    store: ObjectStore | None = None
    ch: Any = None
    dns_txt: Callable[[str], list[str]] | None = None
    http_get: Callable[[str], tuple[int, str]] | None = None
    webhook_transport: Any = None
    telegram_transport: Any = None

    @property
    def secret_box(self) -> SecretBox:
        return SecretBox.from_base64(self.settings.secrets.encryption_key.get_secret_value())

    @property
    def pepper(self) -> str:
        pepper = self.settings.secrets.api_key_pepper.get_secret_value()
        if not pepper:
            raise ConfigurationError("PAYINTEL_SECRETS__API_KEY_PEPPER is not set")
        return pepper


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.payintel
    return state


StateDep = Annotated[AppState, Depends(get_state)]


def get_session(state: StateDep) -> Iterator[Session]:
    session = state.session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def client_ip(request: Request) -> str | None:
    """First `X-Forwarded-For` hop (Caddy sets it) or the socket peer; None when
    the value is not an IP address (e.g. the test client's "testclient")."""
    forwarded = request.headers.get("x-forwarded-for")
    raw = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    if not raw:
        return None
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return None
    return raw


def api_principal(
    request: Request,
    state: StateDep,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """`Authorization: Bearer <key>` → Principal (FR-API-02)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise EntitlementDenied(DenialCode.KEY_REVOKED, "missing bearer token")
    raw = authorization[7:].strip()
    principal = keys_mod.authenticate(
        session,
        raw,
        pepper=state.pepper,
        prefix=state.settings.api.api_key_prefix,
        now=state.clock.now(),
        ip=client_ip(request),
    )
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(api_principal)]


@dataclass(frozen=True)
class Access:
    principal: Principal
    grant: Grant

    @property
    def org_id(self) -> uuid.UUID:
        return self.grant.org_id


def _resolve(session: Session, state: AppState, principal: Principal) -> Grant:
    if principal.org_id is None:
        raise EntitlementDenied(DenialCode.ORG_NOT_ACTIVE, "no organisation")
    flags = FlagService(session, state.settings.flags, clock=state.clock)
    grant = resolve_grant(session, principal.org_id, today=state.clock.now().date(), flags=flags)
    check_ip(principal, grant)
    return grant


def api_access(
    request: Request, state: StateDep, session: SessionDep, principal: PrincipalDep
) -> Access:
    """Entitlements + rate limit for every /v1 call (FR-API-06, FR-API-07)."""
    grant = _resolve(session, state, principal)
    now = state.clock.now()
    state.limiter.check(grant.org_id, grant.api_rps, now=now)
    check_record_quota(
        session,
        grant.org_id,
        daily_limit=grant.daily_records,
        monthly_limit=grant.monthly_records,
        now=now,
    )
    request.state.grant = grant
    return Access(principal, grant)


AccessDep = Annotated[Access, Depends(api_access)]


def scoped(scope: str) -> Callable[[Access], Access]:
    def dep(access: AccessDep) -> Access:
        require_scope(access.principal, scope)
        return access

    return dep

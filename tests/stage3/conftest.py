"""Stage-3 fixtures: an application bound to the test transaction and a seeded world.

`app_state` wires `AppState` to the `db_session` connection (savepoints per
request commit), a `FixedClock`, an in-memory rate limiter, MinIO for exports
and fake proof resolvers. `world` seeds reference data, one active client
organisation with a `c1_full` entitlement for DE, staff and org users, an API
key with every scope, and a handful of stores inside and outside the segment.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from payintel.api.app import create_app
from payintel.api.auth import keys as keys_mod
from payintel.api.deps import AppState
from payintel.compliance import contracts as contracts_mod
from payintel.compliance import kyc as kyc_mod
from payintel.compliance import lifecycle
from payintel.compliance import users as users_mod
from payintel.core.clock import FixedClock
from payintel.core.flags import FlagService
from payintel.core.models.base import (
    ChangeEventType,
    ConfidenceLevel,
    DomainSourceKind,
    DomainStatus,
    FieldProfile,
    KycDecision,
    OrgStatus,
    Product,
    ProviderRole,
    Role,
    ScanStatus,
    ScanType,
)
from payintel.core.models.domains import Domain, Host
from payintel.core.models.orgs import Contract, Organization
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import (
    ChangeEvent,
    StoreCheckoutHost,
    StorePaymentMethod,
    StoreProfile,
    StoreProvider,
)
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import S3Settings, Settings
from payintel.discovery.ingest import ingest
from payintel.discovery.sources.base import SourceRecord
from payintel.entitlements.model import SCOPES, Principal
from payintel.entitlements.quotas import MemoryCounterStore, RateLimiter

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=__import__("datetime").UTC)
ENCRYPTION_KEY = base64.b64encode(b"\x01" * 32).decode()
PEPPER = "test-pepper"
PASSWORD = "correct horse battery staple 42"


def staff_principal(email: str = "seed@payintel.test") -> Principal:
    return Principal(kind="staff", org_id=None, role=Role.STAFF_ADMIN, email=email)


@dataclass
class FakeDns:
    records: dict[str, list[str]] = field(default_factory=dict)

    def __call__(self, name: str) -> list[str]:
        return list(self.records.get(name, []))


@dataclass
class FakeHttp:
    pages: dict[str, tuple[int, str]] = field(default_factory=dict)

    def __call__(self, url: str) -> tuple[int, str]:
        return self.pages.get(url, (404, ""))


@pytest.fixture
def test_settings(s3_settings: S3Settings) -> Settings:
    os.environ["PAYINTEL_SECRETS__ENCRYPTION_KEY"] = ENCRYPTION_KEY
    os.environ["PAYINTEL_SECRETS__API_KEY_PEPPER"] = PEPPER
    try:
        settings = Settings()
    finally:
        os.environ.pop("PAYINTEL_SECRETS__ENCRYPTION_KEY", None)
        os.environ.pop("PAYINTEL_SECRETS__API_KEY_PEPPER", None)
    settings.s3 = s3_settings
    settings.api.cookie_secure = False
    return settings


@pytest.fixture
def export_store(test_settings: Settings) -> ObjectStore:
    store = ObjectStore(make_s3_client(test_settings.s3))
    store.ensure_bucket(test_settings.s3.bucket_exports)
    store.ensure_bucket(test_settings.s3.bucket_artifacts)
    return store


@pytest.fixture
def app_state(
    db_session: Session, fixed_clock: FixedClock, test_settings: Settings, export_store: ObjectStore
) -> AppState:
    factory = sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return AppState(
        settings=test_settings,
        session_factory=factory,
        clock=fixed_clock,
        limiter=RateLimiter(MemoryCounterStore()),
        store=export_store,
        dns_txt=FakeDns(),
        http_get=FakeHttp(),
    )


@pytest.fixture
def app(app_state: AppState) -> FastAPI:
    return create_app(app_state)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# --- world ------------------------------------------------------------------------


@dataclass
class StoreSpec:
    domain: str
    country: str = "DE"
    platform: str = "shopify"
    providers: tuple[tuple[str, ProviderRole], ...] = (("adyen", ProviderRole.GATEWAY),)
    methods: tuple[str, ...] = ("visa", "paypal")
    rank: int = 1000
    source: DomainSourceKind = DomainSourceKind.MANUAL
    status: DomainStatus = DomainStatus.ECOMMERCE
    optout: bool = False
    vertical: str | None = "fashion"
    third_party_hosts: tuple[tuple[str, str], ...] = (
        ("google-analytics.com", "analytics"),
        ("adyen.com", "psp"),
    )


def make_store(session: Session, spec: StoreSpec, *, clock: FixedClock) -> Host:
    now = clock.now()
    if spec.source == DomainSourceKind.CZDS:
        domain = Domain(etld1=spec.domain, tld=spec.domain.rsplit(".", 1)[-1], created_at=now)
        session.add(domain)
        session.flush()
        host = Host(domain_id=domain.id, hostname=spec.domain, is_primary=True, created_at=now)
        session.add(host)
        session.flush()
        from payintel.core.models.domains import DomainSource

        session.add(
            DomainSource(
                domain_id=domain.id,
                source=DomainSourceKind.CZDS,
                first_seen=now,
                last_seen=now,
                batch_id="czds-test",
            )
        )
    else:
        ingest(
            session,
            [SourceRecord(spec.domain, rank=spec.rank)],
            source=spec.source,
            origin="test",
            clock=clock,
        )
        host = session.execute(select(Host).where(Host.hostname == spec.domain)).scalar_one()
        found = session.get(Domain, host.domain_id)
        assert found is not None
        domain = found
    domain.status = spec.status
    domain.optout = spec.optout
    domain.traffic_rank = spec.rank
    today = now.date()
    profile = StoreProfile(
        host_id=host.id,
        platform_id=spec.platform,
        platform_confidence=ConfidenceLevel.HIGH,
        platform_version="9.1.2",
        country=spec.country,
        country_confidence=ConfidenceLevel.HIGH,
        currency="EUR",
        vertical_id=spec.vertical,
        checkout_status=ScanStatus.REACHED_PAYMENT_STEP,
        coverage=__import__(
            "payintel.core.models.base", fromlist=["Coverage"]
        ).Coverage.PAYMENT_STEP,
        traffic_rank=spec.rank,
        last_light_scan_at=now - timedelta(days=1),
        last_checkout_scan_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
    )
    session.add(profile)
    for pid, role in spec.providers:
        session.add(
            StoreProvider(
                host_id=host.id,
                provider_id=pid,
                role=role,
                confidence=ConfidenceLevel.HIGH,
                confidence_score=0.95,
                active_on_checkout=True,
                first_seen=today - timedelta(days=30),
                last_seen=today - timedelta(days=2),
                confirmations=3,
                misses=0,
            )
        )
    for mid in spec.methods:
        session.add(
            StorePaymentMethod(
                host_id=host.id,
                method_id=mid,
                provider_id=spec.providers[0][0] if spec.providers else None,
                confidence=ConfidenceLevel.MEDIUM,
                confidence_score=0.7,
                first_seen=today - timedelta(days=30),
                last_seen=today - timedelta(days=2),
                confirmations=3,
                misses=0,
            )
        )
    for tp, cat in spec.third_party_hosts:
        session.add(
            StoreCheckoutHost(
                host_id=host.id,
                third_party_etld1=tp,
                category=cat,
                provider_id="adyen" if cat == "psp" else None,
                request_count=4,
                first_seen=today - timedelta(days=30),
                last_seen=today - timedelta(days=2),
                confirmations=3,
                misses=0,
            )
        )
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host.id,
        scan_type=ScanType.CHECKOUT,
        started_at=now - timedelta(days=2),
        finished_at=now - timedelta(days=2),
        status=ScanStatus.REACHED_PAYMENT_STEP,
        worker_id="test",
        ruleset_version="test",
    )
    session.add(run)
    session.flush()
    if spec.providers:
        session.add(
            ChangeEvent(
                host_id=host.id,
                event_type=ChangeEventType.PROVIDER_ADDED,
                entity_type="provider",
                entity_id=spec.providers[0][0],
                new_value=spec.providers[0][0],
                detected_at=now - timedelta(days=2),
                scan_run_id=run.id,
            )
        )
    session.flush()
    return host


@dataclass
class World:
    org: Organization
    contract: Contract
    staff_admin_email: str
    staff_support_email: str
    staff_compliance_email: str
    staff_analyst_email: str
    org_admin_email: str
    org_viewer_email: str
    api_key: str
    api_key_id: uuid.UUID
    hosts: dict[str, Host]
    other_org: Organization
    other_api_key: str

    def totp(self, session: Session, email: str, box: object, at: datetime) -> str:
        from payintel.core.models.access import User

        user = session.execute(select(User).where(User.email == email)).scalar_one()
        secret = box.decrypt(  # type: ignore[attr-defined]
            user.totp_secret_encrypted, associated_data=str(user.id)
        )
        return pyotp.TOTP(secret).at(at)


def activate_org(
    session: Session,
    org: Organization,
    *,
    clock: FixedClock,
    settings: Settings,
    profile: FieldProfile = FieldProfile.C1_FULL,
    countries: list[str] | None = None,
    platforms: list[str] | None = None,
    **entitlement: object,
) -> Contract:
    principal = staff_principal()
    lifecycle.transition(
        session, org, OrgStatus.KYC_IN_PROGRESS, principal=principal, comment="kyc", clock=clock
    )
    kyc_mod.update_dossier(
        session,
        org,
        kyc_mod.Dossier(
            address="Musterstr. 1, Berlin",
            website="https://client.example",
            beneficiaries=[{"name": "Erika Muster", "share": 100}],
            contact_name="Erika Muster",
            contact_title="CEO",
            purpose="market_research",
        ),
        principal=principal,
        clock=clock,
    )
    kyc_mod.record_sanctions(
        session,
        org,
        result="clear",
        source="OpenSanctions export",
        checked_at=clock.now(),
        principal=principal,
        clock=clock,
    )
    kyc_mod.decide(
        session, org, KycDecision.APPROVED, principal=principal, comment="ok", clock=clock
    )
    lifecycle.transition(
        session, org, OrgStatus.APPROVED, principal=principal, comment="approved", clock=clock
    )
    lifecycle.transition(
        session, org, OrgStatus.ACTIVE, principal=principal, comment="go live", clock=clock
    )
    today = clock.now().date()
    contract = contracts_mod.create_contract(
        session,
        org,
        number=f"C-{org.legal_name[:6]}",
        product=Product.C_DATA,
        starts_on=today - timedelta(days=10),
        ends_on=today + timedelta(days=355),
        allowed_purposes=["market_research"],
        file_key=None,
        principal=principal,
        clock=clock,
    )
    spec = contracts_mod.EntitlementSpec(
        field_profile=profile,
        countries=["DE"] if countries is None else countries,
        platforms=[] if platforms is None else platforms,
        api_rps=int(entitlement.get("api_rps", 10)),  # type: ignore[call-overload]
        daily_records=int(entitlement.get("daily_records", 50_000)),  # type: ignore[call-overload]
        monthly_records=int(entitlement.get("monthly_records", 1_000_000)),  # type: ignore[call-overload]
        export_max_rows=int(entitlement.get("export_max_rows", 1_000)),  # type: ignore[call-overload]
        export_schedule=str(entitlement.get("export_schedule", "monthly")),
        watchlist_limit=int(entitlement.get("watchlist_limit", 50)),  # type: ignore[call-overload]
        allowed_ips=list(entitlement.get("allowed_ips", [])),  # type: ignore[call-overload]
    )
    flags = FlagService(session, settings.flags, clock=clock)
    contracts_mod.set_entitlement(
        session, contract, spec, principal=principal, settings=settings, flags=flags, clock=clock
    )
    return contract


def issue_key(
    session: Session,
    org: Organization,
    *,
    clock: FixedClock,
    settings: Settings,
    scopes: list[str] | None = None,
    name: str = "test",
    expires_in_days: int | None = None,
    allowed_ips: list[str] | None = None,
) -> keys_mod.IssuedKey:
    return keys_mod.issue_key(
        session,
        org_id=org.id,
        name=name,
        scopes=sorted(SCOPES) if scopes is None else scopes,
        expires_in_days=expires_in_days,
        allowed_ips=allowed_ips or [],
        created_by=None,
        actor="test",
        pepper=settings.secrets.api_key_pepper.get_secret_value(),
        prefix=settings.api.api_key_prefix,
        clock=clock,
    )


def new_user(
    session: Session, email: str, role: Role, org: Organization | None, *, clock: FixedClock
) -> None:
    users_mod.create_user(
        session,
        email=email,
        password=PASSWORD,
        role=role,
        org_id=org.id if org else None,
        actor="test",
        clock=clock,
    )


STORES: tuple[StoreSpec, ...] = (
    StoreSpec("alpha-shop.de", providers=(("adyen", ProviderRole.GATEWAY),)),
    StoreSpec(
        "beta-store.de",
        platform="woocommerce",
        providers=(("stripe", ProviderRole.GATEWAY), ("klarna", ProviderRole.BNPL)),
        methods=("visa", "klarna"),
        rank=500,
    ),
    StoreSpec("gamma-market.de", providers=(("paypal", ProviderRole.WALLET),), rank=2000),
    StoreSpec("delta-boutique.fr", country="FR", providers=(("mollie", ProviderRole.GATEWAY),)),
    StoreSpec("czds-only.de", source=DomainSourceKind.CZDS),
    StoreSpec("optedout.de", optout=True, status=DomainStatus.OPTOUT),
    StoreSpec("not-a-shop.de", status=DomainStatus.NOT_ECOMMERCE, providers=()),
)


@pytest.fixture
def world(
    db_session: Session, fixed_clock: FixedClock, test_settings: Settings, app_state: AppState
) -> World:
    sync_reference(db_session, load_reference(), clock=fixed_clock)
    org = Organization(legal_name="Client GmbH", reg_number="HRB 1", country="DE", created_at=T0)
    other = Organization(legal_name="Other SA", reg_number="RCS 2", country="FR", created_at=T0)
    db_session.add_all([org, other])
    db_session.flush()
    contract = activate_org(db_session, org, clock=fixed_clock, settings=test_settings)
    activate_org(
        db_session,
        other,
        clock=fixed_clock,
        settings=test_settings,
        profile=FieldProfile.C1_BASIC,
        countries=["FR"],
    )
    new_user(db_session, "admin@payintel.test", Role.STAFF_ADMIN, None, clock=fixed_clock)
    new_user(db_session, "support@payintel.test", Role.STAFF_SUPPORT, None, clock=fixed_clock)
    new_user(db_session, "compliance@payintel.test", Role.STAFF_COMPLIANCE, None, clock=fixed_clock)
    new_user(db_session, "analyst@payintel.test", Role.STAFF_ANALYST, None, clock=fixed_clock)
    new_user(db_session, "owner@client.example", Role.ORG_ADMIN, org, clock=fixed_clock)
    new_user(db_session, "viewer@client.example", Role.ORG_VIEWER, org, clock=fixed_clock)
    key = issue_key(db_session, org, clock=fixed_clock, settings=test_settings)
    other_key = issue_key(db_session, other, clock=fixed_clock, settings=test_settings)
    hosts = {spec.domain: make_store(db_session, spec, clock=fixed_clock) for spec in STORES}
    db_session.flush()
    return World(
        org=org,
        contract=contract,
        staff_admin_email="admin@payintel.test",
        staff_support_email="support@payintel.test",
        staff_compliance_email="compliance@payintel.test",
        staff_analyst_email="analyst@payintel.test",
        org_admin_email="owner@client.example",
        org_viewer_email="viewer@client.example",
        api_key=key.raw,
        api_key_id=key.key.id,
        hosts=hosts,
        other_org=other,
        other_api_key=other_key.raw,
    )


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def login(client: TestClient, email: str, *, state: AppState, session: Session) -> str:
    """Password + TOTP (enrolling on first login); returns the CSRF token."""
    from payintel.api.auth import totp as totp_mod
    from payintel.core.models.access import User

    r = client.post(
        "/portal/login", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    user = session.execute(select(User).where(User.email == email)).scalar_one()
    if not totp_mod.enrolled(user):
        r = client.get("/portal/login/enrol", follow_redirects=True)
        assert r.status_code == 200, r.text
        secret = r.url.params["s"]
        csrf = _csrf_from(r.text)
        code = pyotp.TOTP(secret).at(state.clock.now())
        r = client.post(
            "/portal/login/enrol",
            data={"csrf": csrf, "secret": secret, "code": code},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
    else:
        r = client.get("/portal/login/totp")
        csrf = _csrf_from(r.text)
        secret = state.secret_box.decrypt(
            user.totp_secret_encrypted or "", associated_data=str(user.id)
        )
        code = pyotp.TOTP(secret).at(state.clock.now())
        r = client.post(
            "/portal/login/totp", data={"csrf": csrf, "code": code}, follow_redirects=False
        )
        assert r.status_code == 303, r.text
    return csrf


def _csrf_from(html: str) -> str:
    marker = 'name="csrf" value="'
    i = html.index(marker) + len(marker)
    return html[i : html.index('"', i)]


__all__ = [
    "PASSWORD",
    "T0",
    "FakeDns",
    "FakeHttp",
    "StoreSpec",
    "World",
    "activate_org",
    "auth",
    "issue_key",
    "login",
    "make_store",
    "new_user",
    "staff_principal",
]
_ = date

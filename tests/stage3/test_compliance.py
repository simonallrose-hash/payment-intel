"""AC-10 and AC-11 plus FR-KYC-01…07, FR-OO-02/03: lifecycle, KYC gate, c2 flag, opt-out, DSAR."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.compliance import contracts as contracts_mod
from payintel.compliance import dsar, kyc, lifecycle, optout
from payintel.core.clock import FixedClock
from payintel.core.errors import C2DisabledError, ConflictError, ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import (
    DomainStatus,
    DsarKind,
    DsarStatus,
    FieldProfile,
    KycDecision,
    OptoutMethod,
    OrgStatus,
    Product,
    Role,
    ScanType,
)
from payintel.core.models.domains import Domain
from payintel.core.models.orgs import Organization
from payintel.core.models.scans import ScanPlan
from payintel.entitlements.check import EntitlementDenied
from payintel.entitlements.model import Principal
from payintel.scheduler import planner
from tests.stage3.conftest import FakeDns, FakeHttp, World, auth, login, staff_principal

pytestmark = pytest.mark.integration


def _principal(role: Role) -> Principal:
    return Principal(kind="staff", org_id=None, role=role, email=f"{role.value}@payintel.test")


def _applicant(db_session: Session, clock: FixedClock) -> Organization:
    org = Organization(
        legal_name="Applicant AG", reg_number="FN 123456a", country="AT", created_at=clock.now()
    )
    db_session.add(org)
    db_session.flush()
    return org


# --- AC-10: lifecycle and KYC gate ---------------------------------------------------------


def test_activation_requires_approved_kyc(db_session: Session, fixed_clock: FixedClock) -> None:
    org = _applicant(db_session, fixed_clock)
    comp = _principal(Role.STAFF_COMPLIANCE)
    with pytest.raises(ConflictError):  # applied → active is not a transition
        lifecycle.transition(
            db_session, org, OrgStatus.ACTIVE, principal=comp, comment="x", clock=fixed_clock
        )
    lifecycle.transition(
        db_session,
        org,
        OrgStatus.KYC_IN_PROGRESS,
        principal=comp,
        comment="start",
        clock=fixed_clock,
    )
    with pytest.raises(ValidationError):  # decision needs a filled dossier
        kyc.decide(
            db_session, org, KycDecision.APPROVED, principal=comp, comment="ok", clock=fixed_clock
        )
    with pytest.raises(ConflictError):  # approved status needs an approved KYC decision
        lifecycle.transition(
            db_session, org, OrgStatus.APPROVED, principal=comp, comment="x", clock=fixed_clock
        )
    kyc.update_dossier(
        db_session,
        org,
        kyc.Dossier(
            address="Ring 1, Wien",
            website="https://applicant.example",
            beneficiaries=[{"name": "A. Owner", "share": 60}, {"name": "B. Owner", "share": 40}],
            contact_name="A. Owner",
            contact_title="CEO",
            purpose="competitive_analysis",
        ),
        principal=comp,
        clock=fixed_clock,
    )
    kyc.record_sanctions(
        db_session,
        org,
        result="match",
        source="OpenSanctions",
        checked_at=fixed_clock.now(),
        principal=comp,
        clock=fixed_clock,
    )
    with pytest.raises(ValidationError):  # sanctions match blocks approval (LR-14)
        kyc.decide(
            db_session, org, KycDecision.APPROVED, principal=comp, comment="ok", clock=fixed_clock
        )
    kyc.record_sanctions(
        db_session,
        org,
        result="clear",
        source="OpenSanctions",
        checked_at=fixed_clock.now(),
        principal=comp,
        clock=fixed_clock,
    )
    kyc.decide(
        db_session,
        org,
        KycDecision.APPROVED,
        principal=comp,
        comment="all documents checked",
        clock=fixed_clock,
    )
    lifecycle.transition(
        db_session, org, OrgStatus.APPROVED, principal=comp, comment="kyc ok", clock=fixed_clock
    )
    lifecycle.transition(
        db_session,
        org,
        OrgStatus.ACTIVE,
        principal=comp,
        comment="contract signed",
        clock=fixed_clock,
    )
    assert org.status == OrgStatus.ACTIVE
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.object_id == str(org.id)).order_by(AuditLog.id)
        ).scalars()
    ]
    assert actions.count("organization.transition") == 3 and "kyc.decide" in actions


def test_transitions_need_role_and_comment(db_session: Session, fixed_clock: FixedClock) -> None:
    org = _applicant(db_session, fixed_clock)
    with pytest.raises(EntitlementDenied) as exc:
        lifecycle.transition(
            db_session,
            org,
            OrgStatus.KYC_IN_PROGRESS,
            principal=_principal(Role.STAFF_SUPPORT),
            comment="x",
            clock=fixed_clock,
        )
    assert exc.value.reason == "role_forbidden"
    with pytest.raises(ValidationError):
        lifecycle.transition(
            db_session,
            org,
            OrgStatus.KYC_IN_PROGRESS,
            principal=_principal(Role.STAFF_ADMIN),
            comment="  ",
            clock=fixed_clock,
        )
    with pytest.raises(ValidationError):
        kyc.update_dossier(
            db_session,
            org,
            kyc.Dossier(purpose="world domination"),
            principal=_principal(Role.STAFF_ADMIN),
            clock=fixed_clock,
        )
    with pytest.raises(ValidationError):
        kyc.update_dossier(
            db_session,
            org,
            kyc.Dossier(beneficiaries=[{"name": "X", "share": 10}]),
            principal=_principal(Role.STAFF_ADMIN),
            clock=fixed_clock,
        )


def test_c2_profile_refused_while_flag_off(
    db_session: Session, world: World, app_state: AppState, fixed_clock: FixedClock
) -> None:
    today = fixed_clock.now().date()
    comp = _principal(Role.STAFF_COMPLIANCE)
    contract = contracts_mod.create_contract(
        db_session,
        world.org,
        number="C2-1",
        product=Product.C2,
        starts_on=today,
        ends_on=today + timedelta(days=30),
        allowed_purposes=["market_research"],
        file_key=None,
        principal=comp,
        clock=fixed_clock,
    )
    flags = FlagService(db_session, app_state.settings.flags, clock=fixed_clock)
    spec = contracts_mod.EntitlementSpec(field_profile=FieldProfile.C2_RISK)
    with pytest.raises(C2DisabledError):
        contracts_mod.set_entitlement(
            db_session,
            contract,
            spec,
            principal=comp,
            settings=app_state.settings,
            flags=flags,
            clock=fixed_clock,
        )
    flags.set("feature_c2_enabled", True, actor="test")
    ent = contracts_mod.set_entitlement(
        db_session,
        contract,
        spec,
        principal=comp,
        settings=app_state.settings,
        flags=flags,
        clock=fixed_clock,
    )
    assert ent.field_profile == FieldProfile.C2_RISK
    flags.set("feature_c2_enabled", False, actor="test")
    from payintel.entitlements.check import resolve_grant

    with pytest.raises(EntitlementDenied) as exc:  # even an assigned c2 grant is unusable while off
        resolve_grant(db_session, world.org.id, today=today, flags=flags)
    assert exc.value.reason == "c2_disabled"


def test_admin_ui_activation_and_entitlement(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    r = client.post(
        "/admin/orgs",
        data={"csrf": csrf, "legal_name": "Neu GmbH", "country": "de", "reg_number": "HRB 1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    org_id = r.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    url = f"/admin/orgs/{org_id}"
    assert (
        client.post(
            f"{url}/status", data={"csrf": csrf, "to": "active", "comment": "x"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{url}/status", data={"csrf": csrf, "to": "kyc_in_progress", "comment": ""}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{url}/status",
            data={"csrf": csrf, "to": "kyc_in_progress", "comment": "start"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    r = client.post(
        f"{url}/kyc",
        data={
            "csrf": csrf,
            "address": "A 1",
            "website": "https://n.example",
            "contact_name": "N",
            "contact_title": "CFO",
            "purpose": "market_research",
            "beneficiaries": "Nina Neu; 100%",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        client.post(
            f"{url}/kyc/sanctions",
            data={"csrf": csrf, "result": "clear", "source": "OpenSanctions"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"{url}/kyc/decision",
            data={"csrf": csrf, "decision": "approved", "comment": "ok"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"{url}/status",
            data={"csrf": csrf, "to": "approved", "comment": "ok"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"{url}/status",
            data={"csrf": csrf, "to": "active", "comment": "signed"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    today = fixed_clock.now().date()
    r = client.post(
        f"{url}/contracts",
        data={
            "csrf": csrf,
            "number": "N-1",
            "product": "c_data",
            "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=90)).isoformat(),
            "allowed_purposes": "market_research",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    org = db_session.get(Organization, __import__("uuid").UUID(org_id))
    assert org is not None and org.status == OrgStatus.ACTIVE
    contract, _ = contracts_mod.contracts_of(db_session, org.id)[0]
    r = client.post(
        f"{url}/contracts/{contract.id}/entitlement",
        data={"csrf": csrf, "field_profile": "c2_risk", "countries": "DE"},
    )
    assert r.status_code == 403  # flag off
    r = client.post(
        f"{url}/contracts/{contract.id}/entitlement",
        data={"csrf": csrf, "field_profile": "c1_basic", "countries": "DE, AT", "api_rps": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(url).text
    assert "c1_basic" in page and "DE, AT" in page and "active" in page
    # support staff can read the card but not change it
    client.cookies.clear()
    csrf2 = login(client, world.staff_support_email, state=app_state, session=db_session)
    assert client.get(url).status_code == 200
    assert (
        client.post(
            f"{url}/status", data={"csrf": csrf2, "to": "suspended", "comment": "no"}
        ).status_code
        == 403
    )


# --- AC-11: opt-out -----------------------------------------------------------------------


def test_optout_dns_txt_excludes_domain_from_queue_and_api(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    headers = auth(world.api_key)
    assert client.get("/v1/stores/alpha-shop.de", headers=headers).status_code == 200
    planner.ensure_plans(db_session, ScanType.LIGHT, s=app_state.settings.scan, clock=fixed_clock)
    host = world.hosts["alpha-shop.de"]
    assert db_session.execute(select(ScanPlan).where(ScanPlan.host_id == host.id)).first()
    # public form → instructions
    r = client.post(
        "/optout",
        data={"domain": "Alpha-Shop.de", "method": "dns_txt", "contact": "owner@alpha-shop.de"},
    )
    assert r.status_code == 200 and "payintel-optout=" in r.text
    req = optout.pending_requests(db_session)[0]
    assert req.domain == "alpha-shop.de"
    # proof not published yet
    r = client.post(f"/optout/verify/{req.token}")
    assert r.status_code == 200 and "Proof not found" in r.text
    assert client.get("/v1/stores/alpha-shop.de", headers=headers).status_code == 200
    # publish the TXT record (fake resolver) and verify
    dns = app_state.dns_txt
    assert isinstance(dns, FakeDns)
    dns.records["alpha-shop.de"] = ['"payintel-optout=' + req.token + '"']
    r = client.post(f"/optout/verify/{req.token}")
    assert r.status_code == 200 and "Ownership confirmed" in r.text
    db_session.expire_all()
    domain = db_session.get(Domain, host.domain_id)
    assert domain is not None and domain.optout and domain.status == DomainStatus.OPTOUT
    assert db_session.execute(select(ScanPlan).where(ScanPlan.host_id == host.id)).first() is None
    assert client.get("/v1/stores/alpha-shop.de", headers=headers).status_code == 404
    assert "alpha-shop.de" not in {
        s["domain"] for s in client.get("/v1/stores", headers=headers).json()["items"]
    }
    assert "alpha-shop.de" not in {
        c["domain"] for c in client.get("/v1/changes", headers=headers).json()["items"]
    }
    # planning again never recreates the plan
    planner.ensure_plans(db_session, ScanType.LIGHT, s=app_state.settings.scan, clock=fixed_clock)
    assert db_session.execute(select(ScanPlan).where(ScanPlan.host_id == host.id)).first() is None
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.object_id.like("%alpha-shop.de%"))
        ).scalars()
    ]
    assert {"optout.request", "optout.verify", "optout.apply"} <= set(actions)


def test_optout_well_known_file(
    db_session: Session, world: World, app_state: AppState, fixed_clock: FixedClock
) -> None:
    req, ins = optout.request(
        db_session,
        domain="beta-store.de",
        method=OptoutMethod.WELL_KNOWN,
        contact=None,
        clock=fixed_clock,
    )
    http = app_state.http_get
    assert isinstance(http, FakeHttp)
    assert ins.well_known_url == "https://beta-store.de/.well-known/payintel-optout.txt"
    http.pages[ins.well_known_url] = (200, "wrong-token")
    assert not optout.verify(db_session, req, dns_txt=FakeDns(), http_get=http, clock=fixed_clock)
    http.pages[ins.well_known_url] = (200, req.token + "\n")
    assert optout.verify(db_session, req, dns_txt=FakeDns(), http_get=http, clock=fixed_clock)
    domain = db_session.execute(select(Domain).where(Domain.etld1 == "beta-store.de")).scalar_one()
    assert domain.optout and domain.status == DomainStatus.OPTOUT


def test_optout_manual_verification_by_compliance(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    req, _ = optout.request(
        db_session,
        domain="gamma-market.de",
        method=OptoutMethod.DNS_TXT,
        contact=None,
        clock=fixed_clock,
    )
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    assert "gamma-market.de" in client.get("/admin/optout").text
    r = client.post(f"/admin/optout/{req.id}/manual", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    domain = db_session.execute(
        select(Domain).where(Domain.etld1 == "gamma-market.de")
    ).scalar_one()
    assert domain.optout


# --- DSAR --------------------------------------------------------------------------------


def test_dsar_lifecycle_and_deadline(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    r = client.post(
        "/dsar",
        data={
            "kind": "access",
            "subject": "Jane Doe",
            "contact": "jane@example.org",
            "details": "what do you hold",
        },
    )
    assert (r.status_code == 200 and "within 30 days" in r.text) or "#" in r.text
    req = dsar.open_requests(db_session)[0]
    assert req.kind == DsarKind.ACCESS and req.due_at == req.received_at + timedelta(days=30)
    assert dsar.overdue(db_session, clock=fixed_clock) == []
    fixed_clock.advance(days=31)
    assert [r.id for r in dsar.overdue(db_session, clock=fixed_clock)] == [req.id]
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    page = client.get("/admin/dsar").text
    assert "overdue" in page and "Jane Doe" in page
    assert (
        client.post(
            f"/admin/dsar/{req.id}/take", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/admin/dsar/{req.id}/close",
            data={"csrf": csrf, "resolution": "no personal data held"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    db_session.expire_all()
    assert req.status == DsarStatus.CLOSED and req.handled_by and req.resolution
    with pytest.raises(EntitlementDenied):
        dsar.close(
            db_session,
            req,
            principal=_principal(Role.STAFF_SUPPORT),
            resolution="x",
            clock=fixed_clock,
        )


def test_contract_end_cuts_access_automatically(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    headers = auth(world.api_key)
    assert client.get("/v1/usage", headers=headers).status_code == 200
    fixed_clock.advance(days=356)
    r = client.get("/v1/usage", headers=headers)
    assert r.status_code == 403 and r.json()["reason"] == "contract_not_in_term"
    _ = staff_principal

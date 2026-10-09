"""FR-KYC-06 re-KYC schedule with 30-day reminders and FR-KYC-03 OpenSanctions screening."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.compliance import kyc, sanctions
from payintel.core.clock import FixedClock
from payintel.core.errors import ConfigurationError, ValidationError
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import KycDecision, Role
from payintel.core.models.orgs import KycRecord, Organization
from payintel.core.settings import ComplianceSettings
from payintel.entitlements.model import Principal
from tests.stage3.conftest import T0, World, login

pytestmark = pytest.mark.integration

COMP = Principal(kind="staff", org_id=None, role=Role.STAFF_COMPLIANCE, email="c@payintel.test")


def _rec(session: Session, org: Organization) -> KycRecord:
    session.expire_all()
    return session.execute(select(KycRecord).where(KycRecord.org_id == org.id)).scalar_one()


def _actions(session: Session, org: Organization) -> list[str]:
    return [
        a.action
        for a in session.execute(
            select(AuditLog)
            .where(AuditLog.object_type == "kyc_record", AuditLog.object_id == str(org.id))
            .order_by(AuditLog.id)
        ).scalars()
    ]


def test_review_schedule_and_reminders(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    rec = _rec(db_session, world.org)
    assert rec.next_review_at == T0 + timedelta(days=365) and rec.reminder_sent_at is None
    assert kyc.due_for_review(db_session, now=fixed_clock.now()) == []
    fixed_clock.advance(days=336)  # 29 days before the due date
    due = kyc.due_for_review(db_session, now=fixed_clock.now())
    assert [(d.org.id, d.overdue, d.reminded) for d in due] == [
        (world.org.id, False, False),
        (world.other_org.id, False, False),
    ]
    notes: list[str] = []
    sent = kyc.remind(db_session, now=fixed_clock.now(), clock=fixed_clock, notify=notes.append)
    assert [d.org.legal_name for d in sent] == ["Client GmbH", "Other SA"]
    assert notes[0].startswith("re-KYC due 2027-10-08: Client GmbH")
    assert _rec(db_session, world.org).reminder_sent_at == fixed_clock.now()
    assert _actions(db_session, world.org)[-1] == "kyc.review_reminder"
    # the reminder goes out once per due date
    fixed_clock.advance(days=1)
    assert (
        kyc.remind(db_session, now=fixed_clock.now(), clock=fixed_clock, notify=notes.append) == []
    )
    assert len(notes) == 2
    fixed_clock.advance(days=40)
    due = kyc.due_for_review(db_session, now=fixed_clock.now())
    assert all(d.overdue and d.reminded for d in due)
    # opening the re-KYC clears decision and screening; the dossier stays
    rec = kyc.start_review(db_session, world.org, principal=COMP, clock=fixed_clock)
    assert rec.decision is None and rec.sanctions_result is None and rec.next_review_at is None
    assert rec.beneficiaries == [{"name": "Erika Muster", "share": 100}]
    assert [d.org.id for d in kyc.due_for_review(db_session, now=fixed_clock.now())] == [
        world.other_org.id
    ]
    with pytest.raises(ValidationError):
        kyc.start_review(db_session, world.org, principal=COMP, clock=fixed_clock)
    with pytest.raises(ValidationError):  # approval needs a fresh sanctions check
        kyc.decide(
            db_session,
            world.org,
            KycDecision.APPROVED,
            principal=COMP,
            comment="ok",
            clock=fixed_clock,
        )
    kyc.record_sanctions(
        db_session,
        world.org,
        result="clear",
        source="manual",
        checked_at=fixed_clock.now(),
        principal=COMP,
        clock=fixed_clock,
    )
    rec = kyc.decide(
        db_session,
        world.org,
        KycDecision.APPROVED,
        principal=COMP,
        comment="renewed",
        clock=fixed_clock,
        review_interval_days=200,
    )
    assert rec.next_review_at == fixed_clock.now() + timedelta(days=200)
    assert rec.reminder_sent_at is None
    # a change of beneficial owners makes the review due at once
    dossier = kyc.Dossier(
        address=rec.address,
        website=rec.website,
        beneficiaries=[{"name": "Erika Muster", "share": 100}],
        contact_name=rec.contact_name,
        contact_title=rec.contact_title,
        purpose=rec.purpose,
    )
    kyc.update_dossier(db_session, world.org, dossier, principal=COMP, clock=fixed_clock)
    assert _rec(db_session, world.org).next_review_at == fixed_clock.now() + timedelta(days=200)
    dossier.beneficiaries = [
        {"name": "Erika Muster", "share": 60},
        {"name": "Max Muster", "share": 40},
    ]
    fixed_clock.advance(seconds=5)
    kyc.update_dossier(db_session, world.org, dossier, principal=COMP, clock=fixed_clock)
    rec = _rec(db_session, world.org)
    assert rec.next_review_at == fixed_clock.now() and rec.reminder_sent_at is None
    assert _actions(db_session, world.org)[-2:] == ["kyc.update", "kyc.review_due"]
    mine = [d for d in kyc.due_for_review(db_session, now=fixed_clock.now()) if d.org is world.org]
    assert len(mine) == 1 and mine[0].overdue
    # a rejected decision schedules nothing
    rec = kyc.decide(
        db_session, world.org, KycDecision.REJECTED, principal=COMP, comment="no", clock=fixed_clock
    )
    assert rec.next_review_at is None


class FakeOpenSanctions:
    """httpx handler that records requests and answers from a script of results per query."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "boom"})
        body = json.loads(request.content)
        responses = {
            key: {
                "status": 200,
                "results": self.results.get(key, []),
                "total": {"value": len(self.results.get(key, [])), "relation": "eq"},
                "query": q,
            }
            for key, q in body["queries"].items()
        }
        return httpx.Response(200, json={"responses": responses, "limit": 5})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _candidate(score: float, match: bool, *, caption: str = "Erika MUSTER") -> dict[str, Any]:
    return {
        "id": "Q-1",
        "caption": caption,
        "schema": "Person",
        "properties": {"name": [caption], "topics": ["sanction"]},
        "datasets": ["eu_fsf", "us_ofac_sdn"],
        "referents": [],
        "target": True,
        "score": score,
        "explanations": {},
        "match": match,
    }


def test_opensanctions_screening(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    fake = FakeOpenSanctions()
    settings = ComplianceSettings(sanctions_cutoff=0.5)
    with pytest.raises(ConfigurationError):
        sanctions.OpenSanctionsClient("", settings=settings)
    client = sanctions.OpenSanctionsClient(
        "test-key", settings=settings, transport=fake.transport()
    )
    assert client.source == "opensanctions:default"
    rec, result = sanctions.screen(
        db_session, world.org, client=client, principal=COMP, clock=fixed_clock
    )
    assert result.result == "clear" and result.hits == [] and result.queries == 2
    req = fake.requests[-1]
    assert req.method == "POST" and req.url.path == "/match/default"
    assert req.headers["authorization"] == "ApiKey test-key"
    assert dict(req.url.params) == {
        "algorithm": "best",
        "threshold": "0.7",
        "cutoff": "0.5",
        "limit": "5",
    }
    assert json.loads(req.content) == {
        "queries": {
            "org": {
                "schema": "Company",
                "properties": {
                    "name": ["Client GmbH"],
                    "country": ["de"],
                    "registrationNumber": ["HRB 1"],
                },
            },
            "beneficiary:0": {"schema": "Person", "properties": {"name": ["Erika Muster"]}},
        }
    }
    assert rec.sanctions_result == "clear" and rec.sanctions_source == "opensanctions:default"
    assert rec.sanctions_checked_at == fixed_clock.now() and rec.sanctions_details == []
    # a candidate below the threshold: potential match, approval still possible after review
    fake.results = {"beneficiary:0": [_candidate(0.62, False)]}
    fixed_clock.advance(seconds=10)
    rec, result = sanctions.screen(
        db_session, world.org, client=client, principal=COMP, clock=fixed_clock
    )
    assert result.result == "potential_match" and len(result.hits) == 1
    hit = result.hits[0]
    assert (hit.query_name, hit.caption, hit.score, hit.match) == (
        "Erika Muster",
        "Erika MUSTER",
        0.62,
        False,
    )
    assert hit.datasets == ["eu_fsf", "us_ofac_sdn"] and hit.topics == ["sanction"]
    assert rec.sanctions_details == [hit.as_dict()]
    # a flagged match blocks approval (LR-14)
    fake.results = {
        "beneficiary:0": [_candidate(0.95, True), _candidate(0.55, False, caption="E. Muster")]
    }
    rec, result = sanctions.screen(
        db_session, world.org, client=client, principal=COMP, clock=fixed_clock
    )
    assert result.result == "match" and [h.score for h in result.hits] == [0.95, 0.55]
    with pytest.raises(ValidationError, match="sanctions match"):
        kyc.decide(
            db_session,
            world.org,
            KycDecision.APPROVED,
            principal=COMP,
            comment="ok",
            clock=fixed_clock,
        )
    audit = (
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "kyc.sanctions").order_by(AuditLog.id.desc())
        )
        .scalars()
        .first()
    )
    assert audit is not None and audit.after == {
        "result": "match",
        "source": "opensanctions:default",
        "checked_at": fixed_clock.now().isoformat(),
        "hits": 2,
    }
    # service errors never change the recorded result
    fake.status = 500
    with pytest.raises(sanctions.SanctionsUnavailable):
        sanctions.screen(db_session, world.org, client=client, principal=COMP, clock=fixed_clock)
    assert _rec(db_session, world.org).sanctions_result == "match"
    client.close()
    down = sanctions.OpenSanctionsClient(
        "k",
        settings=settings,
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
    )
    with pytest.raises(sanctions.SanctionsUnavailable, match="unreachable"):
        down.match({"org": {"schema": "Company", "properties": {"name": ["x"]}}})
    down.close()


def test_admin_pages(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    page = client.get("/admin")
    assert "<strong>0</strong> re-KYC due within 30 days (0 overdue)" in page.text
    page = client.get(f"/admin/orgs/{world.org.id}")
    assert "OpenSanctions screening is not configured" in page.text
    assert "next review 2027-10-08" in page.text and "Open re-KYC" in page.text
    assert (
        client.post(
            f"/admin/orgs/{world.org.id}/kyc/screen", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 500
    )
    fake = FakeOpenSanctions()
    fake.results = {"org": [_candidate(0.9, True, caption="CLIENT GMBH")]}
    app_state.sanctions_transport = fake.transport()
    app_state.settings.secrets.opensanctions_api_key = SecretStr("k")
    page = client.get(f"/admin/orgs/{world.org.id}")
    assert "Screen via OpenSanctions" in page.text
    r = client.post(
        f"/admin/orgs/{world.org.id}/kyc/screen", data={"csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and "Screening%3A+match" in r.headers["location"]
    page = client.get(f"/admin/orgs/{world.org.id}")
    assert "CLIENT GMBH" in page.text and "eu_fsf, us_ofac_sdn" in page.text
    fake.status = 503
    r = client.post(
        f"/admin/orgs/{world.org.id}/kyc/screen", data={"csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 503
    fixed_clock.advance(days=350)
    client.cookies.clear()  # the staff session has long expired
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    page = client.get("/admin")
    assert "<strong>2</strong> re-KYC due within 30 days (0 overdue)" in page.text
    r = client.post(
        f"/admin/orgs/{world.org.id}/kyc/review", data={"csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and "Re-KYC+opened" in r.headers["location"]
    page = client.get(f"/admin/orgs/{world.org.id}")
    assert "not scheduled" in page.text and "Open re-KYC" not in page.text
    page = client.get("/admin")
    assert "<strong>1</strong> re-KYC due within 30 days" in page.text
    client.cookies.clear()
    login(client, world.staff_support_email, state=app_state, session=db_session)
    for path in ("kyc/review", "kyc/screen"):
        r = client.post(
            f"/admin/orgs/{world.org.id}/{path}", data={"csrf": "x"}, follow_redirects=False
        )
        assert r.status_code == 403


def test_rekyc_cli(cli_runner: Any) -> None:
    from payintel.cli import app

    result = cli_runner.invoke(app, ["orgs", "rekyc"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "0 organisation(s) due within 30 days, 0 reminder(s)" in result.output
    result = cli_runner.invoke(app, ["orgs", "rekyc", "--remind", "--within-days", "400"])
    assert result.exit_code == 0 and "0 reminder(s)" in result.output
    result = cli_runner.invoke(app, ["orgs", "screen", "00000000-0000-0000-0000-000000000001"])
    assert result.exit_code != 0 and isinstance(result.exception, ConfigurationError)

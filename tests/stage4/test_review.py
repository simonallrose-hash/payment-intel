"""FR-QA-05: analyst review queue, evidence, confirm/reject → gold set and suppression."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.api.read import StoreFilters, search
from payintel.core.clock import FixedClock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import GoldEntityType
from payintel.core.models.quality import FindingReview, GoldLabel
from payintel.core.models.store import StoreProvider
from payintel.core.settings import Settings
from payintel.entitlements.check import resolve_grant
from payintel.quality import review
from tests.stage3.conftest import World, auth, login

pytestmark = pytest.mark.integration


@dataclass
class _Result:
    result_rows: list[tuple[Any, ...]]


class FakeCH:
    def __init__(self, rows: list[tuple[Any, ...]], fail: bool = False) -> None:
        self.rows, self.fail = rows, fail
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        self.queries.append((sql, parameters))
        if self.fail:
            raise RuntimeError("ch down")
        return _Result(self.rows)


def test_queue_evidence_and_decisions(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    beta = world.hosts["beta-store.de"]
    q = review.queue(db_session)
    assert q and all(f.review is None for f in q)
    # payment methods (medium, 0.7) sort after providers (high, 0.95) → lowest score first
    assert q[0].entity_type == "payment_method" and q[0].confidence == "medium"
    only = review.queue(db_session, review.QueueFilters(entity_type="provider", entity_id="klarna"))
    assert [(f.hostname, f.entity_id) for f in only] == [("beta-store.de", "klarna")]
    assert review.queue(db_session, review.QueueFilters(max_confidence="low")) == []
    by_domain = review.queue(db_session, review.QueueFilters(domain="BETA"))
    assert {f.hostname for f in by_domain} == {"beta-store.de"}
    with pytest.raises(ValidationError):
        review.queue(db_session, review.QueueFilters(entity_type="platform"))
    with pytest.raises(ValidationError):
        review.queue(db_session, review.QueueFilters(max_confidence="huge"))

    ch = FakeCH(
        [
            (
                fixed_clock.now(),
                "network_host",
                "api.klarna.com",
                "checkout",
                "https://beta-store.de/checkout",
                "klarna.host",
                3,
                "high",
                "artifacts/beta/payment_step.png",
            )
        ]
    )
    rows = review.evidence(ch, "provider", beta.id, "klarna")
    assert len(rows) == 1 and rows[0].rule_id == "klarna.host" and rows[0].rule_version == 3
    assert "obs_provider" in ch.queries[0][0] and ch.queries[0][1] == {
        "h": beta.id,
        "e": "klarna",
        "n": review.EVIDENCE_LIMIT,
    }
    ch2 = FakeCH([])
    assert review.evidence(ch2, "payment_method", beta.id, "klarna") == []
    assert "obs_payment_method" in ch2.queries[0][0]
    assert review.evidence(FakeCH([], fail=True), "provider", beta.id, "klarna") == []
    assert review.evidence(None, "payment_method", beta.id, "klarna") == []

    # reject → gold label absent, suppressed, review row with evidence snapshot, audit
    r = review.decide(
        db_session,
        entity_type="provider",
        host_id=beta.id,
        entity_id="klarna",
        decision="rejected",
        note="BNPL widget only on the product page",
        evidence_rows=rows,
        actor="staff_analyst:a",
        ip="203.0.113.9",
        clock=fixed_clock,
    )
    assert r.decision == "rejected" and r.evidence[0]["rule_id"] == "klarna.host"
    sp = db_session.execute(
        select(StoreProvider).where(
            StoreProvider.host_id == beta.id, StoreProvider.provider_id == "klarna"
        )
    ).scalar_one()
    assert sp.suppressed is True
    gold = db_session.execute(
        select(GoldLabel).where(GoldLabel.host_id == beta.id, GoldLabel.entity_id == "klarna")
    ).scalar_one()
    assert gold.present is False and gold.entity_type == GoldEntityType.PROVIDER
    assert gold.labeled_by == "staff_analyst:a"
    f = review.get(db_session, "provider", beta.id, "klarna")
    assert f.suppressed and f.gold_present is False and f.review is r
    providers_only = review.QueueFilters(entity_type="provider")
    assert all(x.entity_id != "klarna" for x in review.queue(db_session, providers_only))
    assert any(
        x.entity_id == "klarna"
        for x in review.queue(
            db_session, review.QueueFilters(entity_type="provider", include_reviewed=True)
        )
    )
    # confirm again → gold present, visible again, same review row updated
    fixed_clock.advance(seconds=60)
    r2 = review.decide(
        db_session,
        entity_type="provider",
        host_id=beta.id,
        entity_id="klarna",
        decision="confirmed",
        note=None,
        evidence_rows=[],
        actor="staff_analyst:b",
        ip=None,
        clock=fixed_clock,
    )
    assert r2.id == r.id and r2.decision == "confirmed" and r2.evidence == []
    db_session.expire_all()
    sp2 = db_session.get(StoreProvider, sp.id)
    gold2 = db_session.get(GoldLabel, gold.id)
    assert sp2 is not None and gold2 is not None
    assert sp2.suppressed is False and gold2.present is True
    assert gold2.labeled_by == "staff_analyst:b"
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.object_id == f"{beta.id}:klarna").order_by(AuditLog.id)
        ).scalars()
    ]
    assert actions == ["finding.review", "finding.review"]
    assert len(review.recent(db_session)) == 1
    with pytest.raises(ValidationError):
        review.decide(
            db_session,
            entity_type="provider",
            host_id=beta.id,
            entity_id="klarna",
            decision="maybe",
            note=None,
            evidence_rows=[],
            actor="x",
            ip=None,
            clock=fixed_clock,
        )
    with pytest.raises(NotFoundError):
        review.get(db_session, "payment_method", beta.id, "bitcoin")
    with pytest.raises(ValidationError):
        review.get(db_session, "platform", beta.id, "shopify")


def test_rejected_findings_disappear_from_client_reads(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    beta = world.hosts["beta-store.de"]
    headers = auth(world.api_key)
    before = client.get("/v1/stores/beta-store.de", headers=headers).json()
    assert {p["id"] for p in before["providers"]} == {"stripe", "klarna"}
    assert {m["id"] for m in before["payment_methods"]} == {"visa", "klarna"}
    for etype, eid in (("provider", "klarna"), ("payment_method", "klarna")):
        review.decide(
            db_session,
            entity_type=etype,
            host_id=beta.id,
            entity_id=eid,
            decision="rejected",
            note=None,
            evidence_rows=[],
            actor="staff_analyst:a",
            ip=None,
            clock=fixed_clock,
        )
    after = client.get("/v1/stores/beta-store.de", headers=headers).json()
    assert {p["id"] for p in after["providers"]} == {"stripe"}
    assert {m["id"] for m in after["payment_methods"]} == {"visa"}
    flags = FlagService(db_session, Settings().flags, clock=fixed_clock)
    grant = resolve_grant(db_session, world.org.id, today=fixed_clock.now().date(), flags=flags)
    # search filters ignore suppressed findings as well
    hits, _ = search(db_session, grant, StoreFilters(providers=["klarna"]), limit=10)
    assert [r.domain for r in hits] == []
    hits, _ = search(db_session, grant, StoreFilters(methods=["klarna"]), limit=10)
    assert [r.domain for r in hits] == []
    hits, _ = search(db_session, grant, StoreFilters(without_providers=["klarna"]), limit=10)
    assert "beta-store.de" in [r.domain for r in hits]
    hits, _ = search(db_session, grant, StoreFilters(providers_min=2), limit=10)
    assert "beta-store.de" not in [r.domain for r in hits]
    assert (
        client.get("/v1/stores/beta-store.de", headers=headers).json()["providers"][0]["id"]
        == "stripe"
    )


def test_review_pages(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    beta = world.hosts["beta-store.de"]
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    page = client.get("/admin/review")
    assert page.status_code == 200 and "beta-store.de" in page.text and "klarna" in page.text
    page = client.get("/admin/review?entity_type=provider&entity_id=klarna&max_confidence=high")
    assert page.text.count("review</a>") == 1
    page = client.get(f"/admin/review/provider/{beta.id}/klarna")
    assert page.status_code == 200 and "No observations recorded" in page.text
    assert "Confirm (gold: present)" in page.text
    r = client.post(
        f"/admin/review/provider/{beta.id}/klarna",
        data={"csrf": csrf, "decision": "rejected", "note": "false positive"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "rejected" in r.headers["location"]
    page = client.get("/admin/review")
    assert "false positive" in page.text and "staff_analyst" in page.text
    page = client.get(f"/admin/review/provider/{beta.id}/klarna")
    assert "no (suppressed)" in page.text and "absent" in page.text
    assert client.get(f"/admin/review/provider/{beta.id}/nope").status_code == 404
    assert client.get("/admin/review/platform/1/shopify").status_code == 400
    assert (
        client.post(
            f"/admin/review/provider/{beta.id}/klarna",
            data={"csrf": csrf, "decision": "maybe"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    client.cookies.clear()
    login(client, world.staff_support_email, state=app_state, session=db_session)
    assert client.get("/admin/review").status_code == 403
    assert client.get("/admin/review/provider/1/stripe").status_code == 403


def test_finding_review_model_round_trip(db_session: Session, world: World) -> None:
    row = FindingReview(
        host_id=world.hosts["alpha-shop.de"].id,
        entity_type="provider",
        entity_id="adyen",
        decision="confirmed",
        evidence=[],
        reviewed_by="t",
        reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(row)
    db_session.flush()
    assert db_session.get(FindingReview, row.id) is row

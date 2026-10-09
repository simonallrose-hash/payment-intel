"""FR-OO-04: hoster complaints reduce crawl rates for the ASN until staff lift the limit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.compliance import complaints
from payintel.core.clock import FixedClock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.audit import AuditLog
from payintel.core.models.scans import AsnLimit
from payintel.crawl.asn import AsnTable
from payintel.crawl.egress import EgressGuard
from payintel.crawl.light.fetcher import Fetcher
from payintel.scheduler.politeness import AsnPolicy
from tests.stage3.conftest import World, login

pytestmark = pytest.mark.integration

TABLE = AsnTable.from_lines(
    [
        "203.0.113.0\t203.0.113.255\t64496\tDE\tHOSTER-EXAMPLE",
        "198.51.100.0\t198.51.100.255\t64497\tNL\tOTHER-HOSTER",
    ]
)


def _record(session: Session, clock: FixedClock, target: str, **kw: Any) -> AsnLimit:
    args: dict[str, Any] = {
        "source": "abuse@hoster.example",
        "note": None,
        "actor": "staff_admin:a",
        "clock": clock,
        "table": TABLE,
        "factor": 0.1,
        "asn_rps": 1.0,
    }
    args.update(kw)
    return complaints.record(session, target=target, **args)


def test_complaints_apply_tighten_and_lift(db_session: Session, fixed_clock: FixedClock) -> None:
    row = _record(db_session, fixed_clock, "203.0.113.7")
    assert (row.asn, row.factor, row.asn_rps, row.ip, row.asn_name) == (
        64496,
        0.1,
        1.0,
        "203.0.113.7",
        "HOSTER-EXAMPLE",
    )
    assert row.complaints == 1 and row.lifted_at is None
    assert complaints.active_factors(db_session) == {64496: (0.1, 1.0)}
    # a second complaint on the same ASN halves the factor
    fixed_clock.advance(seconds=60)
    row = _record(db_session, fixed_clock, "AS64496", source="ticket 42", note="again")
    assert row.factor == 0.05 and row.complaints == 2 and row.source == "ticket 42"
    assert row.last_complaint_at == fixed_clock.now()
    # by number, without a table
    row2 = _record(db_session, fixed_clock, "64497", table=None, factor=0.5, asn_rps=2.0)
    assert row2.asn == 64497 and row2.ip is None and row2.asn_name is None
    assert set(complaints.active_factors(db_session)) == {64496, 64497}
    with pytest.raises(ValidationError):
        _record(db_session, fixed_clock, "8.8.8.8")  # not in the table
    with pytest.raises(ValidationError):
        _record(db_session, fixed_clock, "203.0.113.7", table=None)
    with pytest.raises(ValidationError):
        _record(db_session, fixed_clock, "203.0.113.7", source="  ")
    with pytest.raises(ValidationError):
        _record(db_session, fixed_clock, "203.0.113.7", factor=1.5)
    lifted = complaints.lift(db_session, 64496, actor="staff_admin:b", note="ok", clock=fixed_clock)
    assert lifted.lifted_by == "staff_admin:b" and lifted.note == "ok"
    assert complaints.active_factors(db_session) == {64497: (0.5, 2.0)}
    assert [r.asn for r in complaints.recent_lifted(db_session)] == [64496]
    with pytest.raises(NotFoundError):
        complaints.lift(db_session, 64496, actor="x", note=None, clock=fixed_clock)
    with pytest.raises(NotFoundError):
        complaints.lift(db_session, 1, actor="x", note=None, clock=fixed_clock)
    # a new complaint after a lift re-applies the configured factor
    row = _record(db_session, fixed_clock, "203.0.113.9", factor=0.2)
    assert row.factor == 0.2 and row.lifted_at is None and row.complaints == 3
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.object_id == "64496").order_by(AuditLog.id)
        ).scalars()
    ]
    assert actions == ["asn_limit.set", "asn_limit.tighten", "asn_limit.lift", "asn_limit.tighten"]


def test_ingest_lines(db_session: Session, fixed_clock: FixedClock) -> None:
    items = complaints.parse_lines(
        [
            "# exported from abuse@",
            "2026-10-08T10:00:00Z 203.0.113.7 abuse@hoster.example load on 203.0.113.7",
            "2026-10-08T10:05:00+00:00 AS64497 ticket-7",
            "2026-10-08T10:06:00 8.8.8.8 abuse@unknown.example",
            "short",
        ]
    )
    assert [(i.target, i.source) for i in items] == [
        ("203.0.113.7", "abuse@hoster.example"),
        ("AS64497", "ticket-7"),
        ("8.8.8.8", "abuse@unknown.example"),
    ]
    assert items[0].note == "load on 203.0.113.7"
    r = complaints.ingest(
        db_session,
        items,
        actor="system:mailbox",
        clock=fixed_clock,
        table=TABLE,
        factor=0.1,
        asn_rps=1.0,
    )
    assert (r.lines, r.applied, r.unresolved) == (3, 2, 1)
    assert set(complaints.active_factors(db_session)) == {64496, 64497}


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float]] = []

    async def acquire(self, key: str, rate: float, burst: float = 1.0) -> float:
        self.calls.append((key, rate, burst))
        return 0.0

    async def take_slot(self, key: str, max_slots: int, ttl_seconds: int) -> bool:
        return True

    async def release_slot(self, key: str) -> None:
        return None


@pytest.mark.asyncio
async def test_fetcher_applies_the_asn_limit(db_session: Session, fixed_clock: FixedClock) -> None:
    _record(db_session, fixed_clock, "203.0.113.7")
    policy = AsnPolicy(TABLE.lookup, lambda: complaints.active_factors(db_session))
    limiter = RecordingLimiter()

    async def resolve(_: str) -> list[str]:
        return []

    fetcher = Fetcher(
        user_agent="t",
        guard=EgressGuard(resolve, allow_private=True),
        limiter=limiter,
        connect_timeout=1,
        read_timeout=1,
        max_bytes=1000,
        host_rps=1.0,
        ip_rps=5.0,
        asn_policy=policy,
    )
    await fetcher._polite_wait("shop.example", ("203.0.113.7",))
    assert limiter.calls == [
        ("host:shop.example", 0.1, 1.0),
        ("ip:203.0.113.7", 0.5, 0.5),
        ("asn:64496", 1.0, 1.0),
    ]
    limiter.calls.clear()
    await fetcher._polite_wait("other.example", ("198.51.100.3",))
    assert limiter.calls == [("host:other.example", 1.0, 1.0), ("ip:198.51.100.3", 5.0, 5.0)]
    limiter.calls.clear()
    await fetcher._polite_wait("nowhere.example", ())
    assert limiter.calls == [("host:nowhere.example", 1.0, 1.0)]
    await fetcher.aclose()


def test_admin_crawler_page(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
    tmp_path: Path,
) -> None:
    csrf = login(client, world.staff_admin_email, state=app_state, session=db_session)
    page = client.get("/admin/crawler")
    assert "No active ASN limits" in page.text and "No ip2asn table is configured" in page.text
    r = client.post(
        "/admin/crawler/asn-limits",
        data={"csrf": csrf, "target": "203.0.113.7", "source": "abuse@hoster.example"},
        follow_redirects=False,
    )
    assert r.status_code == 400  # no table: an IP cannot be mapped
    table = tmp_path / "ip2asn.tsv"
    table.write_text("203.0.113.0\t203.0.113.255\t64496\tDE\tHOSTER-EXAMPLE\n", encoding="utf-8")
    app_state.settings.scan.asn_table_path = table
    r = client.post(
        "/admin/crawler/asn-limits",
        data={"csrf": csrf, "target": "203.0.113.7", "source": "abuse@hoster.example", "note": "n"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "AS64496+limited" in r.headers["location"]
    page = client.get("/admin/crawler")
    assert (
        "AS64496 HOSTER-EXAMPLE" in page.text and "abuse@hoster.example · 203.0.113.7" in page.text
    )
    assert "No ip2asn table" not in page.text
    r = client.post(
        "/admin/crawler/asn-limits/64496/lift",
        data={"csrf": csrf, "note": "resolved with the hoster"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/admin/crawler")
    assert "No active ASN limits" in page.text and "Recently lifted: AS64496" in page.text
    assert (
        client.post(
            "/admin/crawler/asn-limits/64496/lift", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 404
    )
    client.cookies.clear()
    login(client, world.staff_support_email, state=app_state, session=db_session)
    page = client.get("/admin/crawler")
    assert page.status_code == 200 and "Apply limit" not in page.text
    assert (
        client.post(
            "/admin/crawler/asn-limits",
            data={"csrf": "x", "target": "AS1", "source": "s"},
            follow_redirects=False,
        ).status_code
        == 403
    )


def test_crawler_cli(cli_runner: Any, tmp_path: Path) -> None:
    from payintel.cli import app

    result = cli_runner.invoke(app, ["crawler", "complaint", "AS64496", "--source", "abuse@h"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "AS64496: factor 0.1 asn_rps 1.0 complaints 1" in result.output
    result = cli_runner.invoke(app, ["crawler", "complaint", "203.0.113.7", "--source", "abuse@h"])
    assert result.exit_code != 0  # no ip2asn table configured
    feed = tmp_path / "complaints.txt"
    feed.write_text("2026-10-08T10:00:00Z 64497 ticket-7 heavy load\n", encoding="utf-8")
    result = cli_runner.invoke(app, ["crawler", "complaints", str(feed)])
    assert result.exit_code == 0 and "lines=1 applied=1 unresolved=0" in result.output
    result = cli_runner.invoke(app, ["crawler", "asn-limits"])
    assert result.exit_code == 0 and "2 active ASN limit(s)" in result.output
    result = cli_runner.invoke(app, ["crawler", "lift", "64497", "--note", "ok"])
    assert result.exit_code == 0 and "AS64497: lifted" in result.output
    result = cli_runner.invoke(app, ["crawler", "asn-limits"])
    assert "1 active ASN limit(s)" in result.output

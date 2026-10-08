"""Feature flags with audit (FR-ADM-05) and the audit hash chain (FR-AB-01, NFR-S-11)."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import FixedClock
from payintel.core.errors import ValidationError
from payintel.core.flags import KNOWN_FLAGS, SAFETY_FLAGS, FlagService
from payintel.core.models.audit import AuditLog
from payintel.core.settings import FlagDefaults

pytestmark = pytest.mark.integration


def test_flag_defaults_and_set_with_audit(db_session: Session, fixed_clock: FixedClock) -> None:
    svc = FlagService(db_session, FlagDefaults(), clock=fixed_clock)
    assert svc.is_enabled("feature_c2_enabled") is False
    assert svc.is_enabled("allow_payment_field_fill") is True
    assert set(svc.all()) == set(KNOWN_FLAGS)
    assert SAFETY_FLAGS <= KNOWN_FLAGS

    svc.set("allow_payment_field_fill", False, actor="staff_admin:1", ip="10.0.0.1")
    assert svc.is_enabled("allow_payment_field_fill") is False
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.object_type == "feature_flag")
    ).scalar_one()
    assert entry.action == "feature_flag.set"
    assert entry.before == {"enabled": True} and entry.after == {"enabled": False}
    assert entry.ts == fixed_clock.now()
    assert str(entry.ip) == "10.0.0.1"

    svc.set("allow_payment_field_fill", True, actor="staff_admin:1")
    assert svc.is_enabled("allow_payment_field_fill") is True
    assert len(svc.history()) == 1


def test_unknown_flag_rejected(db_session: Session) -> None:
    svc = FlagService(db_session, FlagDefaults())
    with pytest.raises(ValidationError):
        svc.is_enabled("allow_everything")
    with pytest.raises(ValidationError):
        svc.set("allow_everything", True, actor="x")


def test_audit_chain_verifies_and_detects_tampering(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    before = len(db_session.execute(select(AuditLog)).scalars().all())
    for i in range(5):
        audit.record(
            db_session,
            actor="t",
            action="a",
            object_type="o",
            object_id=str(i),
            after={"i": i},
            clock=fixed_clock,
        )
    rows = db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    assert rows[0].prev_hash == audit.GENESIS_HASH
    for prev, cur in itertools.pairwise(rows):
        assert cur.prev_hash == prev.row_hash
    assert audit.verify_chain(db_session).ok

    # A forged row appended with a wrong hash (UPDATE is blocked by the trigger; INSERT is not)
    db_session.execute(
        text(
            "INSERT INTO audit_log"
            "(actor, action, object_type, object_id, ts, prev_hash, row_hash) "
            "VALUES ('evil', 'a', 'o', 'x', :ts, :prev, 'forged')"
        ),
        {"ts": fixed_clock.now(), "prev": rows[-1].row_hash},
    )
    result = audit.verify_chain(db_session)
    assert not result.ok and result.first_broken_id is not None
    assert result.rows == before + 6

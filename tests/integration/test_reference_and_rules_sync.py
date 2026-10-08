"""Reference and rule sync into Postgres: versioning, audit, idempotence (FR-NR-04, FR-DT-01)."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.errors import RuleError
from payintel.core.models.audit import AuditLog
from payintel.core.models.reference import PaymentMethod, Platform, Provider, Vertical
from payintel.core.models.rules import DetectionRule
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.detect.rules import Rule, RuleSet, load_rules, sync_rules

pytestmark = pytest.mark.integration


def test_sync_reference_inserts_and_is_idempotent(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    ref = load_reference()
    first = sync_reference(db_session, ref, actor="test", clock=fixed_clock)
    total = len(ref.providers) + len(ref.payment_methods) + len(ref.platforms) + len(ref.verticals)
    assert (first.inserted, first.updated, first.unchanged) == (total, 0, 0)
    assert db_session.scalar(select(func.count()).select_from(Provider)) == len(ref.providers)
    assert db_session.scalar(select(func.count()).select_from(PaymentMethod)) == len(
        ref.payment_methods
    )
    assert db_session.scalar(select(func.count()).select_from(Platform)) == len(ref.platforms)
    assert db_session.scalar(select(func.count()).select_from(Vertical)) == len(ref.verticals)
    assert db_session.get(Provider, "braintree").parent_provider_id == "paypal"  # type: ignore[union-attr]
    assert db_session.get(Platform, "woocommerce").parent_id == "wordpress"  # type: ignore[union-attr]
    audits = db_session.scalar(select(func.count()).select_from(AuditLog))
    assert audits == total  # one audit row per insert (FR-NR-04)

    second = sync_reference(db_session, ref, actor="test", clock=fixed_clock)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, total)
    assert db_session.scalar(select(func.count()).select_from(AuditLog)) == total
    assert all(v == 1 for v in db_session.execute(select(Provider.version)).scalars())


def test_sync_reference_versions_changes_and_never_deletes(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    ref = load_reference()
    sync_reference(db_session, ref, actor="test", clock=fixed_clock)
    changed = copy.deepcopy(ref)
    stripe = next(p for p in changed.providers if p["id"] == "stripe")
    stripe["name"] = "Stripe (renamed)"
    mollie = next(p for p in changed.providers if p["id"] == "mollie")
    mollie["status"] = "deprecated"
    result = sync_reference(db_session, changed, actor="analyst", clock=fixed_clock)
    assert (result.inserted, result.updated) == (0, 2)
    row = db_session.get(Provider, "stripe")
    assert row is not None and row.name == "Stripe (renamed)" and row.version == 2
    assert db_session.get(Provider, "mollie").status.value == "deprecated"  # type: ignore[union-attr]
    entry = db_session.execute(
        select(AuditLog).where(
            AuditLog.object_id == "stripe", AuditLog.action == "reference.update"
        )
    ).scalar_one()
    assert entry.actor == "analyst"
    assert entry.before["name"] == "Stripe" and entry.after["name"] == "Stripe (renamed)"  # type: ignore[index]
    # the removed-from-YAML row stays in the DB (FR-NR-04: no deletes)
    assert db_session.get(Provider, "mollie") is not None


def test_sync_rules(db_session: Session, fixed_clock: FixedClock) -> None:
    ref = load_reference()
    sync_reference(db_session, ref, clock=fixed_clock)
    ruleset = load_rules(reference=ref)
    first = sync_rules(db_session, ruleset, clock=fixed_clock)
    assert first.inserted == len(ruleset.rules)
    assert db_session.scalar(select(func.count()).select_from(DetectionRule)) == len(ruleset.rules)
    second = sync_rules(db_session, ruleset, clock=fixed_clock)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, len(ruleset.rules))
    # flipping `enabled` on the same version is allowed and counted as an update
    r0 = ruleset.rules[0]
    toggled = RuleSet(
        rules=(Rule(**{**r0.__dict__, "enabled": not r0.enabled, "needs_verification": False}),)
    )
    third = sync_rules(db_session, toggled, clock=fixed_clock)
    assert third.updated == 1
    # same id+version with a different pattern must be rejected
    mutated = RuleSet(rules=(Rule(**{**r0.__dict__, "pattern": "changed.example"}),))
    with pytest.raises(RuleError, match="bump the version"):
        sync_rules(db_session, mutated, clock=fixed_clock)

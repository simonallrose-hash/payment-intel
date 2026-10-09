"""FR-ADM-02 (ADR-0020): rule versions from the admin, overlay on the YAML set,
enable/disable as versions, gold-set preview on observed signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import GoldEntityType, PageScope, RuleTargetType, SignalType
from payintel.core.models.quality import GoldLabel
from payintel.core.models.rules import DetectionRule
from payintel.detect import admin
from payintel.detect.rules import Rule, RuleSet, load_rules, sync_rules
from tests.stage3.conftest import World

pytestmark = pytest.mark.integration


def _draft(rule_id: str = "adyen.custom.host", **over: Any) -> admin.RuleDraft:
    base: dict[str, Any] = {
        "rule_id": rule_id,
        "target_type": RuleTargetType.PROVIDER,
        "target_id": "adyen",
        "signal_type": SignalType.NETWORK_HOST,
        "pattern": "*.adyen.com",
        "weight": 0.7,
        "page_scope": PageScope.CHECKOUT,
        "note": "from the admin",
    }
    base.update(over)
    return admin.RuleDraft(**base)


def test_versions_enable_disable_and_audit(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    assert admin.current_version(db_session, "adyen.custom.host") is None
    v1 = admin.save_version(
        db_session, _draft(), actor="staff_analyst:a", ip=None, clock=fixed_clock
    )
    assert v1.version == 1 and v1.source_file == "admin" and v1.enabled
    fixed_clock.advance(seconds=60)
    v2 = admin.save_version(
        db_session, _draft(weight=0.9), actor="staff_analyst:a", ip="203.0.113.5", clock=fixed_clock
    )
    assert v2.version == 2 and admin.current_version(db_session, "adyen.custom.host") is v2
    assert [r.version for r in admin.versions_of(db_session, "adyen.custom.host")] == [2, 1]
    disabled = admin.set_enabled(
        db_session, "adyen.custom.host", False, actor="staff_analyst:a", ip=None, clock=fixed_clock
    )
    assert disabled.enabled is False and disabled.version == 2
    with pytest.raises(NotFoundError):
        admin.set_enabled(db_session, "nope", True, actor="x", ip=None, clock=fixed_clock)
    with pytest.raises(ValidationError):
        admin.save_version(db_session, _draft(weight=2.0), actor="x", ip=None, clock=fixed_clock)
    current = admin.list_current(db_session, target_type=RuleTargetType.PROVIDER, query="custom")
    assert [r.rule_id for r in current] == ["adyen.custom.host"]
    assert admin.list_current(db_session, query="no-such-rule") == []
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.object_id == "adyen.custom.host").order_by(AuditLog.id)
        ).scalars()
    ]
    assert actions == ["rule.create", "rule.update", "rule.disable"]
    rule = admin.rule_of(v2)
    assert isinstance(rule, Rule) and rule.weight == 0.9 and rule.match == "host_suffix"


def test_overlay_prefers_database_versions_and_admin_edits(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    base = load_rules()
    sync_rules(db_session, base, clock=fixed_clock)
    yaml_rule = next(
        r for r in base.rules if r.enabled and r.target_type == RuleTargetType.PROVIDER
    )
    # 1) disabling a YAML rule in the admin wins over the YAML copy of the same version
    admin.set_enabled(
        db_session, yaml_rule.rule_id, False, actor="staff_analyst:a", ip=None, clock=fixed_clock
    )
    effective = admin.overlay(db_session, base)
    assert not next(r for r in effective.rules if r.rule_id == yaml_rule.rule_id).enabled
    assert effective.version != base.version  # FR-DT-07: ruleset version changes
    # 2) a new admin rule is added; 3) a new version of a YAML rule replaces it
    admin.save_version(db_session, _draft(), actor="a", ip=None, clock=fixed_clock)
    admin.save_version(
        db_session,
        _draft(
            rule_id=yaml_rule.rule_id,
            target_type=yaml_rule.target_type,
            target_id=yaml_rule.target_id,
            signal_type=SignalType.NETWORK_HOST,
            pattern="*.example-override.test",
        ),
        actor="a",
        ip=None,
        clock=fixed_clock,
    )
    effective = admin.overlay(db_session, base)
    by_id = {r.rule_id: r for r in effective.rules}
    assert by_id["adyen.custom.host"].source_file == "admin"
    assert by_id[yaml_rule.rule_id].pattern == "*.example-override.test"
    assert by_id[yaml_rule.rule_id].version == yaml_rule.version + 1
    assert len(effective.rules) == len({r.rule_id for r in base.rules}) + 1


@dataclass
class _Result:
    result_rows: list[tuple[Any, ...]]


class FakeCH:
    """Answers `_signals_of_gold_hosts`: a fixed set of observed signals per host."""

    def __init__(self, rows: dict[str, list[tuple[int, str, str, str]]]) -> None:
        self.rows = rows

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        table = sql.split("FROM ")[1].split(" ")[0]
        hosts = set((parameters or {}).get("hosts", []))
        return _Result([r for r in self.rows.get(table, []) if r[0] in hosts])


def test_preview_scores_draft_against_gold_labels(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    alpha, beta, gamma = (
        world.hosts[d].id for d in ("alpha-shop.de", "beta-store.de", "gamma-market.de")
    )
    now = fixed_clock.now()
    for host, present in ((alpha, True), (beta, False), (gamma, True)):
        db_session.add(
            GoldLabel(
                host_id=host,
                entity_type=GoldEntityType.PROVIDER,
                entity_id="adyen",
                present=present,
                labeled_by="analyst",
                labeled_at=now,
            )
        )
    db_session.flush()
    ch = FakeCH(
        {
            "obs_provider": [
                (alpha, "network_host", "checkoutshopper-live.adyen.com", "checkout"),
                (beta, "network_host", "checkoutshopper-live.adyen.com", "checkout"),
                (gamma, "script_src", "https://cdn.example/other.js", "checkout"),
            ],
            "obs_tech": [(alpha, "js_global", "AdyenCheckout", "checkout")],
        }
    )
    p = admin.preview(db_session, _draft(), ch=ch)
    assert (p.gold_hosts, p.observed_hosts, p.fired) == (3, 3, 2)
    assert (p.tp, p.fp, p.fn) == (1, 1, 1)
    assert p.precision == 0.5 and p.recall == 0.5
    assert p.fired_hosts == sorted([alpha, beta]) and p.false_positive_hosts == [beta]
    assert "gold hosts" in p.note
    # without ClickHouse: only the gold size, with a note
    p0 = admin.preview(db_session, _draft(), ch=None)
    assert p0.gold_hosts == 3 and p0.fired == 0 and "ClickHouse unavailable" in p0.note
    # tech rules have no gold labels: firings only
    tech = admin.preview(
        db_session,
        _draft(
            rule_id="tech.custom",
            target_type=RuleTargetType.TECH,
            target_id="adyen_web",
            signal_type=SignalType.JS_GLOBAL,
            pattern="AdyenCheckout",
        ),
        ch=ch,
    )
    assert tech.gold_hosts == 0 and tech.fired == 0 and "no gold labels" in tech.note
    with pytest.raises(ValidationError):
        admin.preview(db_session, _draft(pattern="not a host!"), ch=ch)


def test_rule_set_of_admin_rules_is_usable_by_the_engine(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    row = admin.save_version(db_session, _draft(), actor="a", ip=None, clock=fixed_clock)
    rs = RuleSet(rules=(admin.rule_of(row),))
    assert rs.enabled() and rs.version
    assert db_session.get(DetectionRule, row.id) is row

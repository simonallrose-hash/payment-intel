"""Alert rules (FR-AL-03): event types, provider, method, minimum confidence, channel."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.alerts import AlertRule
from payintel.core.models.base import ChangeEventType

CHANNELS: tuple[str, ...] = ("telegram", "webhook")
DIGESTS: tuple[str, ...] = ("immediate", "daily", "weekly")
CONFIDENCES: tuple[str, ...] = ("low", "medium", "high")


def create_rule(
    session: Session,
    *,
    org_id: uuid.UUID,
    watchlist_id: int | None,
    event_types: list[str],
    provider_id: str | None,
    method_id: str | None,
    min_confidence: str,
    channel: str,
    webhook_id: int | None,
    telegram_chat_id: str | None,
    digest: str,
    actor: str,
    clock: Clock,
) -> AlertRule:
    known = {e.value for e in ChangeEventType}
    bad = [e for e in event_types if e not in known]
    if bad or not event_types:
        raise ValidationError("unknown or empty event types", event_types=bad)
    if channel not in CHANNELS:
        raise ValidationError("channel must be telegram or webhook (no e-mail, AS-19)")
    if channel == "webhook" and webhook_id is None:
        raise ValidationError("webhook_id is required for the webhook channel")
    if channel == "telegram" and not telegram_chat_id:
        raise ValidationError("telegram_chat_id is required for the telegram channel")
    if digest not in DIGESTS:
        raise ValidationError("digest must be immediate, daily or weekly")
    if min_confidence not in CONFIDENCES:
        raise ValidationError("min_confidence must be low, medium or high")
    rule = AlertRule(
        org_id=org_id,
        watchlist_id=watchlist_id,
        event_types=sorted(set(event_types)),
        provider_id=provider_id,
        method_id=method_id,
        min_confidence=min_confidence,
        channel=channel,
        webhook_id=webhook_id,
        telegram_chat_id=telegram_chat_id,
        digest=digest,
        created_at=clock.now(),
    )
    session.add(rule)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="alert_rule.create",
        object_type="alert_rule",
        object_id=str(rule.id),
        after={
            "org_id": str(org_id),
            "event_types": rule.event_types,
            "channel": channel,
            "digest": digest,
        },
        clock=clock,
    )
    return rule


def get_rule(session: Session, org_id: uuid.UUID, rule_id: int) -> AlertRule:
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.org_id != org_id:
        raise NotFoundError("alert rule not found", id=rule_id)
    return rule


def rules_of(session: Session, org_id: uuid.UUID) -> list[AlertRule]:
    return list(
        session.execute(
            select(AlertRule).where(AlertRule.org_id == org_id).order_by(AlertRule.id)
        ).scalars()
    )


def set_enabled(
    session: Session, rule: AlertRule, enabled: bool, *, actor: str, clock: Clock
) -> None:
    rule.enabled = enabled
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="alert_rule.toggle",
        object_type="alert_rule",
        object_id=str(rule.id),
        after={"enabled": enabled},
        clock=clock,
    )


def delete_rule(session: Session, rule: AlertRule, *, actor: str, clock: Clock) -> None:
    audit.record(
        session,
        actor=actor,
        action="alert_rule.delete",
        object_type="alert_rule",
        object_id=str(rule.id),
        clock=clock,
    )
    session.delete(rule)
    session.flush()

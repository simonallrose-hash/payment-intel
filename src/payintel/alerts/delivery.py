"""Delivery of pending alerts with retries (FR-AL-04): 5 attempts within 24 hours.

Webhooks: one POST per event (or per digest batch) with the HMAC header.
Telegram: one message per (rule, batch). Failures schedule the next attempt
after `retry_delays_hours[attempt-1]`; the fifth failure marks the delivery
`failed`. Every attempt updates the journal row (`delivery`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.alerts import telegram as tg
from payintel.alerts import webhook as wh
from payintel.core.clock import Clock
from payintel.core.crypto import SecretBox
from payintel.core.models.alerts import AlertRule, Delivery, Webhook
from payintel.core.models.base import DeliveryStatus
from payintel.core.settings import Settings

Sender = Callable[[Delivery, AlertRule], None]


@dataclass(frozen=True)
class DispatchResult:
    attempted: int
    delivered: int
    failed: int


class WebhookSender:
    def __init__(
        self,
        box: SecretBox,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Clock,
    ) -> None:
        self._box = box
        self._settings = settings
        self._client = httpx.Client(
            transport=transport, timeout=settings.alerts.webhook_timeout_seconds
        )
        self._clock = clock

    def send(self, session: Session, delivery: Delivery, rule: AlertRule) -> None:
        if rule.webhook_id is None:
            raise RuntimeError("rule has no webhook")
        hook = session.get(Webhook, rule.webhook_id)
        if hook is None or not hook.enabled:
            raise RuntimeError("webhook missing or disabled")
        body = json.dumps(delivery.payload, sort_keys=True, separators=(",", ":")).encode()
        ts = wh.unix_ts(self._clock.now())
        headers = {
            "Content-Type": "application/json",
            self._settings.alerts.signature_header: wh.sign(
                wh.decrypt_secret(hook, self._box), body, ts=ts
            ),
            "X-PayIntel-Delivery": str(delivery.id),
        }
        response = self._client.post(hook.url, content=body, headers=headers)
        if response.status_code >= 300:
            raise RuntimeError(f"webhook responded {response.status_code}")

    def close(self) -> None:
        self._client.close()


class TelegramSender:
    def __init__(self, client: tg.TelegramClient) -> None:
        self._client = client

    def send(self, session: Session, delivery: Delivery, rule: AlertRule) -> None:
        if not rule.telegram_chat_id:
            raise RuntimeError("rule has no telegram chat")
        events = delivery.payload.get("events") or [delivery.payload]
        self._client.send(rule.telegram_chat_id, tg.format_digest(events, title="PayIntel alerts"))


def _due_query(now: datetime) -> Any:
    return (
        select(Delivery, AlertRule)
        .join(AlertRule, AlertRule.id == Delivery.alert_rule_id)
        .where(Delivery.status == DeliveryStatus.PENDING, Delivery.next_attempt_at <= now)
        .order_by(Delivery.next_attempt_at, Delivery.id)
    )


def _immediate_or_due_digest(rule: AlertRule, now: datetime, settings: Settings) -> bool:
    """Digest rules send once a day/week at `digest_hour_utc`; immediate rules any time."""
    if rule.digest == "immediate":
        return True
    if now.hour != settings.alerts.digest_hour_utc:
        return False
    return rule.digest == "daily" or now.weekday() == 0


def dispatch(
    session: Session,
    *,
    settings: Settings,
    clock: Clock,
    webhook_sender: WebhookSender | None,
    telegram_sender: TelegramSender | None,
    limit: int = 500,
    force_digests: bool = False,
) -> DispatchResult:
    now = clock.now()
    attempted = delivered = failed = 0
    rows = session.execute(_due_query(now).limit(limit)).all()
    # Group digest deliveries per rule into one message; immediate ones go alone.
    groups: dict[int, list[Delivery]] = {}
    rules: dict[int, AlertRule] = {}
    for delivery, rule in rows:
        if not force_digests and not _immediate_or_due_digest(rule, now, settings):
            continue
        rules[rule.id] = rule
        groups.setdefault(rule.id, []).append(delivery)
    for rule_id, deliveries in groups.items():
        rule = rules[rule_id]
        batches = [deliveries] if rule.digest != "immediate" else [[d] for d in deliveries]
        for batch in batches:
            attempted += len(batch)
            lead = batch[0]
            if len(batch) > 1:
                lead.payload = {"digest": rule.digest, "events": [d.payload for d in batch]}
            try:
                if rule.channel == "webhook":
                    if webhook_sender is None:
                        raise RuntimeError("webhook sender not configured")
                    webhook_sender.send(session, lead, rule)
                else:
                    if telegram_sender is None:
                        raise RuntimeError("telegram sender not configured")
                    telegram_sender.send(session, lead, rule)
            except Exception as exc:
                for d in batch:
                    _schedule_retry(d, str(exc)[:500], now, settings)
                failed += len(batch)
            else:
                for d in batch:
                    d.status = DeliveryStatus.DELIVERED
                    d.attempts += 1
                    d.delivered_at = now
                    d.last_error = None
                delivered += len(batch)
    session.flush()
    return DispatchResult(attempted, delivered, failed)


def _schedule_retry(d: Delivery, error: str, now: datetime, settings: Settings) -> None:
    d.attempts += 1
    d.last_error = error
    delays = settings.alerts.retry_delays_hours
    if d.attempts >= settings.api.webhook_max_attempts:
        d.status = DeliveryStatus.FAILED
        d.next_attempt_at = None
        return
    hours = delays[min(d.attempts - 1, len(delays) - 1)]
    d.next_attempt_at = now + timedelta(hours=hours)


def deliveries_of_org(
    session: Session, org_id: Any, *, limit: int = 100
) -> list[tuple[Delivery, AlertRule]]:
    rows = session.execute(
        select(Delivery, AlertRule)
        .join(AlertRule, AlertRule.id == Delivery.alert_rule_id)
        .where(AlertRule.org_id == org_id)
        .order_by(Delivery.id.desc())
        .limit(limit)
    ).all()
    return [(d, r) for d, r in rows]

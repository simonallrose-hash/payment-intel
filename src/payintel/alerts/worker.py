"""`payintel alerts dispatch`: match events → deliver (FR-AL-04, NFR-P-07)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from payintel.alerts import delivery as dl
from payintel.alerts import matcher
from payintel.alerts.telegram import TelegramClient
from payintel.core.clock import Clock
from payintel.core.crypto import SecretBox
from payintel.core.settings import Settings


@dataclass(frozen=True)
class CycleResult:
    matched: matcher.MatchResult
    dispatched: dl.DispatchResult


def build_senders(
    settings: Settings,
    *,
    clock: Clock,
    webhook_transport: httpx.BaseTransport | None = None,
    telegram_transport: httpx.BaseTransport | None = None,
) -> tuple[dl.WebhookSender | None, dl.TelegramSender | None]:
    key = settings.secrets.encryption_key.get_secret_value()
    webhook = (
        dl.WebhookSender(
            SecretBox.from_base64(key), settings, transport=webhook_transport, clock=clock
        )
        if key
        else None
    )
    token = settings.secrets.telegram_bot_token.get_secret_value()
    telegram = (
        dl.TelegramSender(
            TelegramClient(
                token, api_base=settings.alerts.telegram_api_base, transport=telegram_transport
            )
        )
        if token
        else None
    )
    return webhook, telegram


def run_cycle(
    session_factory: Callable[[], Session],
    *,
    settings: Settings,
    clock: Clock,
    webhook_sender: dl.WebhookSender | None,
    telegram_sender: dl.TelegramSender | None,
    force_digests: bool = False,
) -> CycleResult:
    session = session_factory()
    try:
        matched = matcher.match_new_events(session, settings=settings, clock=clock)
        session.commit()
        dispatched = dl.dispatch(
            session,
            settings=settings,
            clock=clock,
            webhook_sender=webhook_sender,
            telegram_sender=telegram_sender,
            force_digests=force_digests,
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return CycleResult(matched, dispatched)

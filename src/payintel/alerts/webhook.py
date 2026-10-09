"""Webhook endpoints and HMAC-SHA256 signatures (FR-AL-04).

Header `X-PayIntel-Signature: t=<unix ts>,v1=<hex>` where
`v1 = HMAC_SHA256(secret, f"{t}.{body}")`. The receiver recomputes it and
rejects old timestamps; the secret is shown once at creation and stored
AES-GCM encrypted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.crypto import SecretBox
from payintel.core.errors import NotFoundError
from payintel.core.models.alerts import Webhook

SIGNATURE_VERSION = "v1"


def sign(secret: str, body: bytes, *, ts: int) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},{SIGNATURE_VERSION}={mac}"


def verify(secret: str, body: bytes, header: str, *, now_ts: int, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(now_ts - ts) > tolerance:
        return False
    expected = sign(secret, body, ts=ts)
    return hmac.compare_digest(expected, f"t={ts},{SIGNATURE_VERSION}={parts.get('v1', '')}")


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def create_webhook(
    session: Session,
    *,
    org_id: uuid.UUID,
    url: str,
    enabled: bool,
    box: SecretBox,
    actor: str,
    clock: Clock,
) -> tuple[Webhook, str]:
    secret = new_secret()
    hook = Webhook(
        org_id=org_id,
        url=url,
        secret_encrypted=box.encrypt(secret, associated_data=url),
        enabled=enabled,
        created_at=clock.now(),
    )
    session.add(hook)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="webhook.create",
        object_type="webhook",
        object_id=str(hook.id),
        after={"org_id": str(org_id), "url": url},
        clock=clock,
    )
    return hook, secret


def get_webhook(session: Session, org_id: uuid.UUID, webhook_id: int) -> Webhook:
    hook = session.get(Webhook, webhook_id)
    if hook is None or hook.org_id != org_id:
        raise NotFoundError("webhook not found", id=webhook_id)
    return hook


def decrypt_secret(hook: Webhook, box: SecretBox) -> str:
    return box.decrypt(hook.secret_encrypted, associated_data=hook.url)


def update_webhook(
    session: Session,
    hook: Webhook,
    *,
    url: str | None,
    enabled: bool | None,
    box: SecretBox,
    actor: str,
    clock: Clock,
) -> Webhook:
    before = {"url": hook.url, "enabled": hook.enabled}
    if url is not None and url != hook.url:
        secret = decrypt_secret(hook, box)
        hook.url = url
        hook.secret_encrypted = box.encrypt(secret, associated_data=url)
    if enabled is not None:
        hook.enabled = enabled
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="webhook.update",
        object_type="webhook",
        object_id=str(hook.id),
        before=before,
        after={"url": hook.url, "enabled": hook.enabled},
        clock=clock,
    )
    return hook


def delete_webhook(session: Session, hook: Webhook, *, actor: str, clock: Clock) -> None:
    audit.record(
        session,
        actor=actor,
        action="webhook.delete",
        object_type="webhook",
        object_id=str(hook.id),
        before={"url": hook.url},
        clock=clock,
    )
    session.delete(hook)
    session.flush()


def list_webhooks(session: Session, org_id: uuid.UUID) -> list[Webhook]:
    return list(
        session.execute(
            select(Webhook).where(Webhook.org_id == org_id).order_by(Webhook.id)
        ).scalars()
    )


def unix_ts(at: datetime) -> int:
    return int(at.timestamp())

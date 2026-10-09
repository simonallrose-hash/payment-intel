"""Mandatory TOTP (FR-UI-01, NFR-S-03). Secrets are AES-GCM encrypted at rest."""

from __future__ import annotations

from datetime import datetime

import pyotp

from payintel.core.crypto import SecretBox
from payintel.core.models.access import User


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def store_secret(user: User, secret: str, box: SecretBox) -> None:
    user.totp_secret_encrypted = box.encrypt(secret, associated_data=str(user.id))
    user.totp_confirmed_at = None


def verify_code(user: User, code: str, box: SecretBox, *, at: datetime) -> bool:
    if not user.totp_secret_encrypted:
        return False
    secret = box.decrypt(user.totp_secret_encrypted, associated_data=str(user.id))
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), for_time=at, valid_window=1)


def enrolled(user: User) -> bool:
    return bool(user.totp_secret_encrypted) and user.totp_confirmed_at is not None

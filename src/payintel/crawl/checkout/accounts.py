"""System-created shop accounts (FR-CW-12, AS-25): one per host, password AES-GCM encrypted.

Accounts are created only by the scanner, only when guest checkout is
unavailable and `allow_account_registration` is on, only with the probe
e-mail of the host. The plaintext password exists in memory for the duration
of the walk; the database holds `v1:` AES-GCM ciphertext under the
`PAYINTEL_SECRETS__ENCRYPTION_KEY`. Logins into accounts the system did not
create are impossible by construction: the only credentials the walker can
use come from this table.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.crypto import SecretBox
from payintel.core.models.base import StoreAccountStatus
from payintel.core.models.scans import StoreAccount
from payintel.crawl.checkout.identities import probe_email

PASSWORD_BYTES = 24


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    account_id: int
    status: StoreAccountStatus


class AccountManager:
    def __init__(self, box: SecretBox, *, company_domain: str) -> None:
        self._box = box
        self.company_domain = company_domain

    def email_for(self, host_id: int) -> str:
        return probe_email(f"h{host_id}", self.company_domain)

    @staticmethod
    def new_password() -> str:
        # 24 random bytes, URL-safe; shops with "must contain a digit/upper/lower" rules
        # are satisfied by appending a fixed suffix (the entropy is in the prefix).
        return secrets.token_urlsafe(PASSWORD_BYTES) + "-Aa1"

    def get(self, session: Session, host_id: int) -> Credentials | None:
        acc = session.scalar(select(StoreAccount).where(StoreAccount.host_id == host_id))
        if acc is None or acc.status == StoreAccountStatus.DELETED:
            return None
        password = self._box.decrypt(acc.password_encrypted, associated_data=f"host:{host_id}")
        return Credentials(acc.email, password, acc.id, acc.status)

    def create(
        self,
        session: Session,
        *,
        host_id: int,
        scan_run_id: uuid.UUID | None,
        status: StoreAccountStatus,
        password: str,
        now: datetime,
    ) -> Credentials:
        """Persist the account the walker just registered (or is about to)."""
        acc = session.scalar(select(StoreAccount).where(StoreAccount.host_id == host_id))
        email = self.email_for(host_id)
        encrypted = self._box.encrypt(password, associated_data=f"host:{host_id}")
        if acc is None:
            acc = StoreAccount(
                host_id=host_id,
                email=email,
                password_encrypted=encrypted,
                status=status,
                created_scan_run_id=scan_run_id,
                last_used_at=now,
            )
            session.add(acc)
        else:
            acc.email = email
            acc.password_encrypted = encrypted
            acc.status = status
            acc.created_scan_run_id = scan_run_id
            acc.last_used_at = now
        session.flush()
        return Credentials(email, password, acc.id, status)

    @staticmethod
    def touch(
        session: Session, account_id: int, *, status: StoreAccountStatus, now: datetime
    ) -> None:
        acc = session.get(StoreAccount, account_id)
        if acc is not None:
            acc.status = status
            acc.last_used_at = now
            session.flush()

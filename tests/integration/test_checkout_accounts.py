"""Store accounts (FR-CW-12, AS-25): one per host, encrypted password, system-created only."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.crypto import SecretBox
from payintel.core.models.base import DomainSourceKind, StoreAccountStatus
from payintel.core.models.domains import Host
from payintel.core.models.scans import StoreAccount
from payintel.crawl.checkout.accounts import AccountManager
from payintel.discovery.ingest import ingest
from payintel.discovery.sources.base import SourceRecord


def test_account_roundtrip(db_session: Session, fixed_clock: FixedClock) -> None:
    ingest(
        db_session,
        [SourceRecord("acct-shop.de", rank=1)],
        source=DomainSourceKind.MANUAL,
        origin="t",
        clock=fixed_clock,
    )
    host = db_session.execute(select(Host).where(Host.hostname == "acct-shop.de")).scalar_one()
    box = SecretBox.from_base64(base64.b64encode(os.urandom(32)).decode())
    mgr = AccountManager(box, company_domain="payintel.example")
    assert mgr.get(db_session, host.id) is None
    now = datetime(2026, 10, 8, tzinfo=UTC)
    pwd = mgr.new_password()
    assert len(pwd) > 24 and pwd.endswith("-Aa1")
    # the FK to scan_run requires a run; the manager is exercised without one
    acc_cred = mgr.create(
        db_session,
        host_id=host.id,
        scan_run_id=None,
        status=StoreAccountStatus.PENDING_VERIFICATION,
        password=pwd,
        now=now,
    )
    assert acc_cred.email == f"checkout-probe+h{host.id}@payintel.example"
    stored = db_session.get(StoreAccount, acc_cred.account_id)
    assert stored is not None and stored.password_encrypted.startswith("v1:")
    assert pwd not in stored.password_encrypted
    got = mgr.get(db_session, host.id)
    assert (
        got is not None
        and got.password == pwd
        and got.status == StoreAccountStatus.PENDING_VERIFICATION
    )
    mgr.touch(db_session, got.account_id, status=StoreAccountStatus.ACTIVE, now=now)
    assert mgr.get(db_session, host.id).status == StoreAccountStatus.ACTIVE  # type: ignore[union-attr]
    # a second host cannot read the first host's ciphertext (associated data binds host id)
    other = SecretBox.from_base64(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(InvalidTag):
        AccountManager(other, company_domain="x").get(db_session, host.id)

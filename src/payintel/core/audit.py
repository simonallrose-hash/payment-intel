"""Append-only audit log with a hash chain (FR-AB-01, NFR-S-11).

Each row's `row_hash` = SHA-256 over (`prev_hash` || canonical JSON of the row
payload). `prev_hash` is the previous row's `row_hash` (or the genesis constant).
`verify_chain` recomputes the chain and reports the first broken row; the
migration adds a trigger that rejects UPDATE/DELETE on the table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.models.audit import AuditLog

GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> bytes:
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def compute_row_hash(
    *,
    prev_hash: str,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    ts: datetime,
    ip: str | None,
) -> str:
    payload = {
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "before": before,
        "after": after,
        "ts": ts.isoformat(),
        "ip": ip,
    }
    return hashlib.sha256(prev_hash.encode() + _canonical(payload)).hexdigest()


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> AuditLog:
    """Append one audit row inside the caller's transaction.

    The previous row is read with `FOR UPDATE` so concurrent writers serialise
    on the chain head instead of forking it.
    """
    last = session.execute(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(1).with_for_update()
    ).scalar_one_or_none()
    prev_hash = last.row_hash if last else GENESIS_HASH
    ts = clock.now()
    row = AuditLog(
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
        ts=ts,
        ip=ip,
        prev_hash=prev_hash,
        row_hash=compute_row_hash(
            prev_hash=prev_hash,
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=object_id,
            before=before,
            after=after,
            ts=ts,
            ip=ip,
        ),
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class ChainVerification:
    rows: int
    ok: bool
    first_broken_id: int | None = None


def verify_chain(session: Session) -> ChainVerification:
    """Recompute every hash in id order (daily job, NFR-S-11)."""
    prev = GENESIS_HASH
    count = 0
    for row in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        count += 1
        expected = compute_row_hash(
            prev_hash=prev,
            actor=row.actor,
            action=row.action,
            object_type=row.object_type,
            object_id=row.object_id,
            before=row.before,
            after=row.after,
            ts=row.ts,
            ip=row.ip,
        )
        if row.prev_hash != prev or row.row_hash != expected:
            return ChainVerification(rows=count, ok=False, first_broken_id=row.id)
        prev = row.row_hash
    return ChainVerification(rows=count, ok=True)

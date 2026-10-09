"""Usage journal for every client request and every export (FR-API-09, LR-17)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from payintel.core.models.audit import UsageLog

MAX_PARAM_CHARS = 512


def _trim(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        text = v if isinstance(v, str) else str(v)
        out[k] = text[:MAX_PARAM_CHARS]
    return out


def log_usage(
    session: Session,
    *,
    org_id: uuid.UUID,
    endpoint: str,
    params: dict[str, Any],
    records: int,
    ip: str | None,
    ts: datetime,
    duration_ms: int,
    api_key_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> UsageLog:
    row = UsageLog(
        ts=ts,
        org_id=org_id,
        api_key_id=api_key_id,
        user_id=user_id,
        endpoint=endpoint[:128],
        params=_trim(params),
        records=records,
        ip=ip,
        duration_ms=duration_ms,
        request_id=request_id,
    )
    session.add(row)
    session.flush()
    return row

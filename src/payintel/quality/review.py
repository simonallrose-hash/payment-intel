"""FR-QA-05: manual review of findings.

An analyst sees a finding (a provider or payment method detected on a store),
its evidence (the last observations from ClickHouse: signal, page, rule,
screenshot key) and confirms or rejects it. Both decisions write a gold label
(`gold_label.present` = confirmed), so rejections feed the gold set and the
rule evaluation; a rejected finding is also `suppressed` and disappears from
client responses, exports and reports until an analyst confirms it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.base import ConfidenceLevel, GoldEntityType
from payintel.core.models.domains import Host
from payintel.core.models.quality import FindingReview, GoldLabel
from payintel.core.models.store import StorePaymentMethod, StoreProfile, StoreProvider

DECISIONS: tuple[str, ...] = ("confirmed", "rejected")
ENTITY_TYPES: tuple[str, ...] = ("provider", "payment_method")
EVIDENCE_LIMIT = 20
_RANK = {ConfidenceLevel.LOW: 0, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.HIGH: 2}


@dataclass(frozen=True)
class Finding:
    host_id: int
    hostname: str
    entity_type: str
    entity_id: str
    confidence: str
    confidence_score: float
    first_seen: datetime | Any
    last_seen: datetime | Any
    confirmations: int
    misses: int
    suppressed: bool
    platform_id: str | None
    country: str | None
    gold_present: bool | None = None
    review: FindingReview | None = None


@dataclass(frozen=True)
class EvidenceRow:
    scan_ts: str
    signal_type: str
    signal_value: str
    page_type: str
    page_url: str
    rule_id: str
    rule_version: int
    confidence: str
    evidence_key: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class QueueFilters:
    entity_type: str | None = None
    entity_id: str | None = None
    domain: str | None = None
    max_confidence: str | None = None  # low | medium | high: show findings up to this level
    include_reviewed: bool = False
    limit: int = 50


def _level(value: str | None) -> ConfidenceLevel | None:
    if not value:
        return None
    try:
        return ConfidenceLevel(value)
    except ValueError as exc:
        raise ValidationError("confidence must be low, medium or high") from exc


def _reviewed_clause(entity_type: str, host_col: Any, id_col: Any) -> Any:
    return exists().where(
        and_(
            FindingReview.host_id == host_col,
            FindingReview.entity_type == entity_type,
            FindingReview.entity_id == id_col,
        )
    )


def queue(session: Session, f: QueueFilters | None = None) -> list[Finding]:
    """Findings waiting for a decision: lowest confidence first, then newest."""
    f = f or QueueFilters()
    if f.entity_type and f.entity_type not in ENTITY_TYPES:
        raise ValidationError("entity_type must be provider or payment_method")
    max_level = _level(f.max_confidence)
    allowed = (
        {lvl for lvl in ConfidenceLevel if _RANK[lvl] <= _RANK[max_level]} if max_level else None
    )
    out: list[Finding] = []
    specs: list[tuple[str, Any, Any]] = []
    if f.entity_type in (None, "provider"):
        specs.append(("provider", StoreProvider, StoreProvider.provider_id))
    if f.entity_type in (None, "payment_method"):
        specs.append(("payment_method", StorePaymentMethod, StorePaymentMethod.method_id))
    for etype, model, id_col in specs:
        q = (
            select(model, Host.hostname, StoreProfile.platform_id, StoreProfile.country)
            .join(Host, Host.id == model.host_id)
            .outerjoin(StoreProfile, StoreProfile.host_id == model.host_id)
        )
        if not f.include_reviewed:
            q = q.where(~_reviewed_clause(etype, model.host_id, id_col))
        if f.entity_id:
            q = q.where(id_col == f.entity_id)
        if f.domain:
            q = q.where(Host.hostname.ilike(f"%{f.domain.strip().lower()}%"))
        if allowed is not None:
            q = q.where(model.confidence.in_(sorted(allowed, key=lambda c: _RANK[c])))
        q = q.order_by(model.confidence_score, model.first_seen.desc(), id_col).limit(f.limit)
        for row, hostname, platform_id, country in session.execute(q).all():
            out.append(_finding(row, etype, hostname, platform_id, country))
    out.sort(key=lambda x: (x.confidence_score, x.hostname, x.entity_id))
    return out[: f.limit]


def _finding(
    row: Any, etype: str, hostname: str, platform_id: str | None, country: str | None
) -> Finding:
    return Finding(
        host_id=row.host_id,
        hostname=hostname,
        entity_type=etype,
        entity_id=row.provider_id if etype == "provider" else row.method_id,
        confidence=row.confidence.value,
        confidence_score=float(row.confidence_score),
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        confirmations=row.confirmations,
        misses=row.misses,
        suppressed=row.suppressed,
        platform_id=platform_id,
        country=country,
    )


def _row(session: Session, entity_type: str, host_id: int, entity_id: str) -> Any:
    if entity_type == "provider":
        return session.execute(
            select(StoreProvider).where(
                StoreProvider.host_id == host_id, StoreProvider.provider_id == entity_id
            )
        ).scalar_one_or_none()
    if entity_type == "payment_method":
        return session.execute(
            select(StorePaymentMethod).where(
                StorePaymentMethod.host_id == host_id, StorePaymentMethod.method_id == entity_id
            )
        ).scalar_one_or_none()
    raise ValidationError("entity_type must be provider or payment_method")


def get(session: Session, entity_type: str, host_id: int, entity_id: str) -> Finding:
    row = _row(session, entity_type, host_id, entity_id)
    host = session.get(Host, host_id)
    if row is None or host is None:
        raise NotFoundError("finding not found", host_id=host_id, entity_id=entity_id)
    profile = session.get(StoreProfile, host_id)
    gold = session.execute(
        select(GoldLabel.present).where(
            GoldLabel.host_id == host_id,
            GoldLabel.entity_type == GoldEntityType(entity_type),
            GoldLabel.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    review = session.execute(
        select(FindingReview).where(
            FindingReview.host_id == host_id,
            FindingReview.entity_type == entity_type,
            FindingReview.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    base = _finding(
        row,
        entity_type,
        host.hostname,
        profile.platform_id if profile else None,
        profile.country if profile else None,
    )
    return Finding(**{**base.__dict__, "gold_present": gold, "review": review})


def evidence(
    ch: Any, entity_type: str, host_id: int, entity_id: str, *, limit: int = EVIDENCE_LIMIT
) -> list[EvidenceRow]:
    """Latest observations of the finding from ClickHouse; empty when unavailable."""
    if ch is None:
        return []
    table, col = (
        ("obs_provider", "provider_id")
        if entity_type == "provider"
        else ("obs_payment_method", "method_id")
    )
    try:
        result = ch.query(
            f"SELECT scan_ts, signal_type, signal_value, page_type, page_url, rule_id, "  # noqa: S608 - closed set of identifiers
            f"rule_version, confidence, evidence_key FROM {table} "
            f"WHERE host_id = %(h)s AND {col} = %(e)s ORDER BY scan_ts DESC LIMIT %(n)s",
            parameters={"h": host_id, "e": entity_id, "n": limit},
        )
    except Exception:
        return []
    out = []
    for r in result.result_rows:
        ts = r[0]
        out.append(
            EvidenceRow(
                scan_ts=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                signal_type=str(r[1]),
                signal_value=str(r[2])[:500],
                page_type=str(r[3]),
                page_url=str(r[4])[:500],
                rule_id=str(r[5]),
                rule_version=int(r[6] or 0),
                confidence=str(r[7]),
                evidence_key=str(r[8]),
            )
        )
    return out


def decide(
    session: Session,
    *,
    entity_type: str,
    host_id: int,
    entity_id: str,
    decision: str,
    note: str | None,
    evidence_rows: list[EvidenceRow],
    actor: str,
    ip: str | None,
    clock: Clock,
) -> FindingReview:
    """Record the decision, write the gold label and (un)suppress the finding."""
    if decision not in DECISIONS:
        raise ValidationError("decision must be confirmed or rejected")
    row = _row(session, entity_type, host_id, entity_id)
    if row is None:
        raise NotFoundError("finding not found", host_id=host_id, entity_id=entity_id)
    now = clock.now()
    present = decision == "confirmed"
    gold = session.execute(
        select(GoldLabel).where(
            GoldLabel.host_id == host_id,
            GoldLabel.entity_type == GoldEntityType(entity_type),
            GoldLabel.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if gold is None:
        session.add(
            GoldLabel(
                host_id=host_id,
                entity_type=GoldEntityType(entity_type),
                entity_id=entity_id,
                present=present,
                labeled_by=actor,
                labeled_at=now,
                notes=note,
            )
        )
    else:
        gold.present, gold.labeled_by, gold.labeled_at, gold.notes = present, actor, now, note
    review = session.execute(
        select(FindingReview).where(
            FindingReview.host_id == host_id,
            FindingReview.entity_type == entity_type,
            FindingReview.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    before = {"decision": review.decision} if review else None
    snapshot = [e.as_dict() for e in evidence_rows[:EVIDENCE_LIMIT]]
    if review is None:
        review = FindingReview(
            host_id=host_id,
            entity_type=entity_type,
            entity_id=entity_id,
            decision=decision,
            note=note,
            evidence=snapshot,
            reviewed_by=actor,
            reviewed_at=now,
        )
        session.add(review)
    else:
        review.decision, review.note, review.evidence = decision, note, snapshot
        review.reviewed_by, review.reviewed_at = actor, now
    row.suppressed = not present
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="finding.review",
        object_type=f"store_{entity_type}",
        object_id=f"{host_id}:{entity_id}",
        before=before,
        after={"decision": decision, "suppressed": row.suppressed, "evidence": len(snapshot)},
        ip=ip,
        clock=clock,
    )
    return review


def recent(session: Session, *, limit: int = 50) -> list[tuple[FindingReview, str]]:
    rows = session.execute(
        select(FindingReview, Host.hostname)
        .join(Host, Host.id == FindingReview.host_id)
        .order_by(FindingReview.reviewed_at.desc())
        .limit(limit)
    ).all()
    return [(r, h) for r, h in rows]

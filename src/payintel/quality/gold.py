"""Gold set import (FR-QA-01).

CSV columns: `domain,entity_type,entity_id,present,labeled_by,labeled_at,notes`.
- `domain` is a hostname; the eTLD+1/host rows are created if missing;
- `entity_type` ∈ provider | payment_method | platform | country;
- `entity_id` must exist in the reference (country: ISO 3166-1 alpha-2);
- `present` ∈ true/false/1/0/yes/no;
- `labeled_by`/`labeled_at` may be empty and are then taken from the CLI
  options (`--labeled-by`, import time).

Labels are upserted on (host, entity_type, entity_id). The stage-0 gold set is
a structure plus a synthetic sample used by tests; real labels come from the
analyst (ADR-0006).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ValidationError
from payintel.core.models.base import DomainStatus, GoldEntityType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.quality import GoldLabel
from payintel.core.models.reference import PaymentMethod, Platform, Provider

REQUIRED_COLUMNS = ("domain", "entity_type", "entity_id", "present")
_TRUE = {"true", "1", "yes", "y"}
_FALSE = {"false", "0", "no", "n"}
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


@dataclass
class GoldImportResult:
    inserted: int = 0
    updated: int = 0
    hosts_created: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_bool(value: str, *, line: int) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValidationError(f"line {line}: present must be true/false, got {value!r}")


def _etld1_naive(hostname: str) -> str:
    """Stage-0 placeholder for the PSL-based eTLD+1 (FR-DS-04 lands in stage 1).

    Takes the last two labels; multi-label public suffixes (co.uk) are handled
    by `payintel.discovery.psl` from stage 1, which re-links hosts on import.
    """
    parts = hostname.split(".")
    return ".".join(parts[-2:])


def get_or_create_host(
    session: Session, hostname: str, *, result: GoldImportResult | None = None
) -> Host:
    hostname = hostname.strip().lower().rstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not _HOST_RE.match(hostname):
        raise ValidationError(f"invalid hostname {hostname!r}")
    host = session.execute(select(Host).where(Host.hostname == hostname)).scalar_one_or_none()
    if host is not None:
        return host
    etld1 = _etld1_naive(hostname)
    domain = session.execute(select(Domain).where(Domain.etld1 == etld1)).scalar_one_or_none()
    if domain is None:
        domain = Domain(etld1=etld1, tld=etld1.rsplit(".", 1)[-1], status=DomainStatus.CANDIDATE)
        session.add(domain)
        session.flush()
    host = Host(domain_id=domain.id, hostname=hostname, is_primary=(hostname == etld1))
    session.add(host)
    session.flush()
    if result is not None:
        result.hosts_created += 1
    return host


def _known_ids(session: Session) -> dict[GoldEntityType, set[str] | None]:
    return {
        GoldEntityType.PROVIDER: set(session.execute(select(Provider.id)).scalars()),
        GoldEntityType.PAYMENT_METHOD: set(session.execute(select(PaymentMethod.id)).scalars()),
        GoldEntityType.PLATFORM: set(session.execute(select(Platform.id)).scalars()),
        GoldEntityType.COUNTRY: None,
    }


def import_gold_csv(
    session: Session,
    path: Path,
    *,
    default_labeled_by: str,
    clock: Clock = SYSTEM_CLOCK,
) -> GoldImportResult:
    result = GoldImportResult()
    known = _known_ids(session)
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValidationError(f"{path.name}: missing columns {missing}")
        for line_no, row in enumerate(reader, start=2):
            try:
                entity_type = GoldEntityType(row["entity_type"].strip())
            except ValueError as exc:
                raise ValidationError(
                    f"line {line_no}: unknown entity_type {row['entity_type']!r}"
                ) from exc
            entity_id = row["entity_id"].strip()
            ids = known[entity_type]
            if ids is None:
                if not _COUNTRY_RE.match(entity_id):
                    raise ValidationError(
                        f"line {line_no}: country must be ISO alpha-2, got {entity_id!r}"
                    )
            elif entity_id not in ids:
                raise ValidationError(
                    f"line {line_no}: {entity_type.value} {entity_id!r} "
                    "is not in the reference (FR-NR-05)"
                )
            present = _parse_bool(row["present"], line=line_no)
            labeled_by = (row.get("labeled_by") or "").strip() or default_labeled_by
            labeled_at_raw = (row.get("labeled_at") or "").strip()
            labeled_at = datetime.fromisoformat(labeled_at_raw) if labeled_at_raw else clock.now()
            if labeled_at.tzinfo is None:
                raise ValidationError(f"line {line_no}: labeled_at must carry a timezone")
            host = get_or_create_host(session, row["domain"], result=result)
            existing = session.execute(
                select(GoldLabel).where(
                    GoldLabel.host_id == host.id,
                    GoldLabel.entity_type == entity_type,
                    GoldLabel.entity_id == entity_id,
                )
            ).scalar_one_or_none()
            notes = (row.get("notes") or "").strip() or None
            if existing is None:
                session.add(
                    GoldLabel(
                        host_id=host.id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        present=present,
                        labeled_by=labeled_by,
                        labeled_at=labeled_at,
                        notes=notes,
                    )
                )
                result.inserted += 1
            else:
                existing.present = present
                existing.labeled_by = labeled_by
                existing.labeled_at = labeled_at
                existing.notes = notes
                result.updated += 1
    session.flush()
    return result


def gold_size(session: Session) -> int:
    """Number of distinct labelled hosts (target ≥ 500, FR-QA-01)."""
    return len(set(session.execute(select(GoldLabel.host_id)).scalars()))

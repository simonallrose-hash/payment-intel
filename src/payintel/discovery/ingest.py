"""Ingest source records into `domain`, `host`, `domain_source` with lineage (FR-DS-03..05).

Deduplication happens on eTLD+1 (`domain.etld1` is unique); every eTLD+1 gets a
primary host, and sub-domain hosts (`shop.brand.com`) become additional `host`
rows linked to the same domain (FR-DS-05). Records are upserted in chunks with
`INSERT … ON CONFLICT`, so re-importing the same file is idempotent and only
moves `last_seen` (plus a better Tranco rank).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ForbiddenError, ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.base import DomainSourceKind
from payintel.core.models.domains import Domain, DomainSource, Host, ImportBatch
from payintel.core.settings import get_settings
from payintel.discovery.normalize import normalize_hostname
from payintel.discovery.psl import SuffixList, get_suffix_list
from payintel.discovery.sources.base import SourceRecord

CHUNK = 2_000


@dataclass
class IngestResult:
    batch_id: str
    records_seen: int = 0
    records_invalid: int = 0
    domains_new: int = 0
    domains_updated: int = 0
    hosts_new: int = 0
    invalid_samples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Prepared:
    etld1: str
    tld: str
    hostname: str
    rank: int | None


def make_batch_id(source: DomainSourceKind, origin: str, clock: Clock) -> str:
    digest = hashlib.sha256(origin.encode()).hexdigest()[:8]
    return f"{source.value}:{clock.now():%Y%m%dT%H%M%S}:{digest}"


def prepare(record: SourceRecord, psl: SuffixList) -> _Prepared | None:
    try:
        norm = normalize_hostname(record.hostname)
    except ValidationError:
        return None
    split = psl.split(norm.hostname)
    if split is None:
        return None
    return _Prepared(etld1=split.etld1, tld=split.tld, hostname=norm.hostname, rank=record.rank)


def ingest(
    session: Session,
    records: Iterable[SourceRecord],
    *,
    source: DomainSourceKind,
    origin: str,
    clock: Clock = SYSTEM_CLOCK,
    psl: SuffixList | None = None,
) -> IngestResult:
    if source == DomainSourceKind.CZDS:
        flags = FlagService(session, get_settings().flags, clock=clock)
        if not flags.is_enabled("czds_import_enabled"):
            raise ForbiddenError("CZDS import is disabled (flag czds_import_enabled, AS-22)")
    psl = psl or get_suffix_list()
    now = clock.now()
    batch_id = make_batch_id(source, origin, clock)
    batch = ImportBatch(id=batch_id, source=source, origin=origin, started_at=now)
    session.add(batch)
    session.flush()
    result = IngestResult(batch_id=batch_id)

    chunk: dict[str, _Prepared] = {}
    extra_hosts: dict[str, str] = {}  # hostname -> etld1

    def flush() -> None:
        if not chunk:
            return
        _upsert_chunk(session, chunk, extra_hosts, source, batch_id, now, result)
        chunk.clear()
        extra_hosts.clear()

    for record in records:
        result.records_seen += 1
        prepared = prepare(record, psl)
        if prepared is None:
            result.records_invalid += 1
            if len(result.invalid_samples) < 10:
                result.invalid_samples.append(record.hostname)
            continue
        current = chunk.get(prepared.etld1)
        if current is None or (
            prepared.rank is not None and (current.rank is None or prepared.rank < current.rank)
        ):
            chunk[prepared.etld1] = _Prepared(
                prepared.etld1, prepared.tld, prepared.etld1, prepared.rank
            )
        if prepared.hostname != prepared.etld1:
            extra_hosts[prepared.hostname] = prepared.etld1
        if len(chunk) >= CHUNK:
            flush()
    flush()

    batch.finished_at = clock.now()
    batch.records_seen = result.records_seen
    batch.records_invalid = result.records_invalid
    batch.domains_new = result.domains_new
    batch.domains_updated = result.domains_updated
    batch.hosts_new = result.hosts_new
    session.flush()
    return result


def _upsert_chunk(
    session: Session,
    chunk: dict[str, _Prepared],
    extra_hosts: dict[str, str],
    source: DomainSourceKind,
    batch_id: str,
    now: object,
    result: IngestResult,
) -> None:
    rows = [
        {"etld1": p.etld1, "tld": p.tld, "traffic_rank": p.rank, "created_at": now}
        for p in chunk.values()
    ]
    base = pg_insert(Domain).values(rows)
    stmt = base.on_conflict_do_update(
        index_elements=[Domain.etld1],
        set_={
            "traffic_rank": _least_rank(base.excluded.traffic_rank, Domain.traffic_rank),
        },
    ).returning(Domain.id, Domain.etld1, Domain.created_at)
    inserted = session.execute(stmt).all()
    ids = {etld1: did for did, etld1, _created in inserted}
    new_count = sum(1 for _d, _e, created in inserted if created == now)
    result.domains_new += new_count
    result.domains_updated += len(inserted) - new_count

    src_rows = [
        {
            "domain_id": ids[e],
            "source": source,
            "first_seen": now,
            "last_seen": now,
            "batch_id": batch_id,
        }
        for e in chunk
    ]
    src_stmt = pg_insert(DomainSource).values(src_rows)
    session.execute(
        src_stmt.on_conflict_do_update(
            constraint="uq_domain_source_domain_id_source",
            set_={"last_seen": src_stmt.excluded.last_seen, "batch_id": src_stmt.excluded.batch_id},
        )
    )

    host_rows = [
        {"domain_id": ids[e], "hostname": e, "is_primary": True, "created_at": now} for e in chunk
    ] + [
        {"domain_id": ids[etld1], "hostname": h, "is_primary": False, "created_at": now}
        for h, etld1 in extra_hosts.items()
        if etld1 in ids
    ]
    host_stmt = (
        pg_insert(Host)
        .values(host_rows)
        .on_conflict_do_nothing(constraint="uq_host_hostname")
        .returning(Host.id)
    )
    result.hosts_new += len(session.execute(host_stmt).all())


def _least_rank(excluded: object, existing: object) -> object:
    from sqlalchemy import func

    return func.least(func.coalesce(excluded, existing), func.coalesce(existing, excluded))


def domain_ids_by_etld1(session: Session, etld1s: Iterable[str]) -> dict[str, int]:
    rows = session.execute(select(Domain.etld1, Domain.id).where(Domain.etld1.in_(list(etld1s))))
    return {e: i for e, i in rows}

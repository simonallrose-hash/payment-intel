"""Third-party scripts: recorded by scans, fetched once by the script pool (FR-LS-03, ADR-0038).

A light scan downloads only first-party scripts (ADR-0011): a CDN host such as
`js.stripe.com` is shared by thousands of shops, and the 1 rps per host rule
(FR-SC-06) would otherwise serialise the whole fleet on it. The scan records
the third-party URLs it saw (`record_script_requests`, in the persistence
phase); `fetch_script_urls` — a background pass of worker-light — leases
pending URLs, fetches each one once through the same polite fetcher, stores the
body under its SHA-256 and links every host that referenced the URL
(`host_js_asset`, the «hash → hosts» relation). A fetched URL is re-checked after
`light.third_party_js_ttl_days`; a failing one is retried with backoff and parked
as `error` after `light.third_party_js_max_attempts`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from payintel.core import metrics
from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.logging import get_logger
from payintel.core.models.assets import HostJsAsset, JsAssetUrl, JsAssetUrlHost
from payintel.core.settings import LightScanSettings
from payintel.crawl.light.artifacts import (
    ArtifactWriter,
    ScriptMeta,
    record_asset,
    record_script,
    sha256_hex,
)
from payintel.crawl.light.fetcher import Fetcher

log = get_logger("payintel.crawl.light.scripts")

MAX_SCRIPT_BYTES = 2 * 1024 * 1024  # FR-LS-07 / ADR-0011 p.4, same as first-party scripts
LEASE_SECONDS = 300
RETRY_BACKOFF = timedelta(hours=1)  # × attempts
STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_ERROR = "error"


def third_party_script_urls(script_srcs: list[str], etld1: str, *, limit: int) -> list[str]:
    """http(s) script URLs on another eTLD+1 than the shop, in page order, deduplicated
    and capped at `limit` (the fragment is dropped; the query stays, it versions CDN files)."""
    out: list[str] = []
    if limit <= 0:
        return out
    for src in script_srcs:
        parts = urlsplit(src)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host:
            continue
        if host == etld1 or host.endswith("." + etld1):
            continue
        url = parts._replace(fragment="").geturl()
        if len(url) > 2048 or url in out:
            continue
        out.append(url)
        if len(out) >= limit:
            break
    return out


@dataclass
class RequestStats:
    linked: int = 0  # hosts linked right away to an already fetched body
    queued: int = 0  # URLs the pool still has to fetch for this host


def record_script_requests(
    session: Session, *, host_id: int, urls: list[str], now: datetime, ttl: timedelta
) -> RequestStats:
    """Persistence phase of a scan: link the host to bodies the pool already has;
    queue the rest (and stale ones, which keep their last hash until re-fetched)."""
    stats = RequestStats()
    for url in urls:
        row = session.get(JsAssetUrl, url)
        if row is None:
            session.execute(
                insert(JsAssetUrl)
                .values(
                    url=url,
                    hostname=(urlsplit(url).hostname or "").lower()[:255],
                    status=STATUS_PENDING,
                    attempts=0,
                    next_fetch_at=now,
                    first_seen=now,
                    last_seen=now,
                )
                .on_conflict_do_nothing(index_elements=["url"])
            )
        else:
            row.last_seen = now
            if row.sha256 is not None:
                _link(session, host_id=host_id, url=url, sha256=row.sha256, now=now)
                stats.linked += 1
                if row.status == STATUS_OK and row.next_fetch_at > now:
                    continue  # fresh: nothing to fetch
                if row.status == STATUS_OK:  # stale: refresh, keep the hash meanwhile
                    row.status = STATUS_PENDING
                    row.next_fetch_at = now
            elif row.status == STATUS_ERROR and row.next_fetch_at > now:
                continue  # parked after repeated failures; retried when next_fetch_at passes
        session.execute(
            insert(JsAssetUrlHost)
            .values(url=url, host_id=host_id, requested_at=now)
            .on_conflict_do_nothing(constraint="uq_js_asset_url_host")
        )
        stats.queued += 1
    session.flush()
    return stats


def _link(session: Session, *, host_id: int, url: str, sha256: str, now: datetime) -> None:
    link = session.execute(
        select(HostJsAsset).where(HostJsAsset.host_id == host_id, HostJsAsset.sha256 == sha256)
    ).scalar_one_or_none()
    if link is None:
        session.add(
            HostJsAsset(
                host_id=host_id, sha256=sha256, src_url=url[:2048], first_seen=now, last_seen=now
            )
        )
    else:
        link.last_seen = now
        link.src_url = url[:2048]
    session.flush()


def lease_script_urls(
    session: Session, *, worker_id: str, limit: int, clock: Clock = SYSTEM_CLOCK
) -> list[JsAssetUrl]:
    """Due pending URLs, oldest first, `FOR UPDATE SKIP LOCKED` so pools on several
    workers never fetch the same URL twice."""
    now = clock.now()
    candidates = (
        select(JsAssetUrl.url)
        .where(
            JsAssetUrl.status.in_((STATUS_PENDING, STATUS_ERROR)),
            JsAssetUrl.next_fetch_at <= now,
            or_(JsAssetUrl.locked_until.is_(None), JsAssetUrl.locked_until < now),
        )
        .order_by(JsAssetUrl.first_seen, JsAssetUrl.url)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(JsAssetUrl)
        .where(JsAssetUrl.url.in_(candidates))
        .values(locked_until=now + timedelta(seconds=LEASE_SECONDS), locked_by=worker_id)
        .returning(JsAssetUrl.url)
    )
    urls = [row[0] for row in session.execute(stmt)]
    if not urls:
        return []
    return list(
        session.execute(
            select(JsAssetUrl).where(JsAssetUrl.url.in_(urls)).order_by(JsAssetUrl.first_seen)
        ).scalars()
    )


@dataclass
class FetchStats:
    leased: int = 0
    fetched: int = 0
    failed: int = 0
    parked: int = 0
    linked_hosts: int = 0
    errors: dict[str, int] = field(default_factory=dict)


def _finish_ok(
    session: Session, *, url: str, meta: ScriptMeta, now: datetime, ttl: timedelta
) -> int:
    row = session.get(JsAssetUrl, url)
    if row is None:  # pragma: no cover - deleted under us
        return 0
    record_asset(session, meta=meta, now=now)  # a refresh may have nobody waiting
    waiting = list(
        session.execute(select(JsAssetUrlHost).where(JsAssetUrlHost.url == url)).scalars()
    )
    for req in waiting:
        record_script(session, host_id=req.host_id, meta=meta, now=now)
        session.delete(req)
    row.status = STATUS_OK
    row.sha256 = meta.sha256
    row.attempts = 0
    row.last_error = None
    row.fetched_at = now
    row.next_fetch_at = now + ttl
    row.locked_until = None
    row.locked_by = None
    session.flush()
    return len(waiting)


def _finish_failed(
    session: Session, *, url: str, error: str, now: datetime, ttl: timedelta, max_attempts: int
) -> bool:
    """Returns True when the URL is parked (`error`) after `max_attempts`."""
    row = session.get(JsAssetUrl, url)
    if row is None:  # pragma: no cover
        return False
    row.attempts += 1
    row.last_error = error[:300]
    row.locked_until = None
    row.locked_by = None
    parked = row.attempts >= max_attempts
    if parked:
        row.status = STATUS_ERROR
        row.attempts = 0
        row.next_fetch_at = now + ttl
    else:
        row.status = STATUS_PENDING
        row.next_fetch_at = now + RETRY_BACKOFF * row.attempts
    session.flush()
    return parked


async def fetch_script_urls(
    factory: sessionmaker[Session],
    *,
    fetcher: Fetcher,
    writer: ArtifactWriter,
    settings: LightScanSettings,
    worker_id: str,
    clock: Clock = SYSTEM_CLOCK,
) -> FetchStats:
    """One pass of the script pool: lease due URLs, fetch them (politeness limits apply
    per host as for any request), store bodies by SHA-256, link the waiting hosts."""
    stats = FetchStats()
    ttl = timedelta(days=settings.third_party_js_ttl_days)
    max_attempts = settings.third_party_js_max_attempts

    def lease() -> list[tuple[str, str]]:
        with factory() as session:
            rows = lease_script_urls(
                session, worker_id=worker_id, limit=settings.third_party_js_batch, clock=clock
            )
            out = [(r.url, r.hostname) for r in rows]
            session.commit()
        return out

    leased = await asyncio.to_thread(lease)
    stats.leased = len(leased)
    if not leased:
        return stats
    sem = asyncio.Semaphore(settings.third_party_js_concurrency)

    async def one(url: str) -> None:
        async with sem:
            r = await fetcher.fetch(url, max_bytes=MAX_SCRIPT_BYTES)
        now = clock.now()
        if r.error or r.status != 200 or not r.body or r.truncated:
            error = r.error or ("truncated" if r.truncated else f"http_{r.status}")
            stats.failed += 1
            stats.errors[error.split(":")[0]] = stats.errors.get(error.split(":")[0], 0) + 1
            metrics.JS_FETCH_TOTAL.labels("failed").inc()

            def fail() -> bool:
                with factory() as session:
                    parked = _finish_failed(
                        session,
                        url=url,
                        error=error,
                        now=now,
                        ttl=ttl,
                        max_attempts=max_attempts,
                    )
                    session.commit()
                return parked

            if await asyncio.to_thread(fail):
                stats.parked += 1
            return
        digest = sha256_hex(r.body)
        await asyncio.to_thread(writer.put_script, digest, r.body)
        meta = ScriptMeta(
            src_url=url,
            sha256=digest,
            size_bytes=len(r.body),
            content_type=r.headers.get("content-type"),
            s3_key=writer.script_key(digest),
        )

        def ok() -> int:
            with factory() as session:
                linked = _finish_ok(session, url=url, meta=meta, now=now, ttl=ttl)
                session.commit()
            return linked

        linked = await asyncio.to_thread(ok)
        stats.linked_hosts += linked  # not `+= await …`: the read would happen before the await
        stats.fetched += 1
        metrics.JS_FETCH_TOTAL.labels("ok").inc()

    results = await asyncio.gather(*(one(u) for u, _ in leased), return_exceptions=True)
    for (url, _), res in zip(leased, results, strict=True):
        if isinstance(res, BaseException):
            log.error("script fetch crashed", url=url, error=repr(res))
    log.info(
        "script pool pass",
        leased=stats.leased,
        fetched=stats.fetched,
        failed=stats.failed,
        parked=stats.parked,
        linked_hosts=stats.linked_hosts,
        errors=stats.errors,
    )
    return stats


def status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(JsAssetUrl.status, func.count()).group_by(JsAssetUrl.status)
    ).all()
    counts = {str(status): int(n) for status, n in rows}
    counts["waiting_hosts"] = int(
        session.execute(select(func.count(JsAssetUrlHost.id))).scalar_one()
    )
    return counts

"""Production wiring of `AppState` (`payintel api serve`).

Tests build `AppState` by hand with a transactional session factory and a
`FixedClock`; this module is the only place that touches real engines,
Redis, ClickHouse, S3 and network resolvers.
"""

from __future__ import annotations

from typing import Any

from redis import Redis
from sqlalchemy.orm import sessionmaker

from payintel.api.deps import AppState
from payintel.api.read import ClickHouseEvidence, NoEvidence
from payintel.compliance import resolvers
from payintel.core.ch import make_ch_client
from payintel.core.clock import SYSTEM_CLOCK
from payintel.core.db import get_engine
from payintel.core.logging import get_logger
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import Settings
from payintel.entitlements.quotas import RateLimiter, RedisCounterStore

log = get_logger(__name__)


def build_state(settings: Settings) -> AppState:
    engine = get_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    ch: Any = None
    try:
        ch = make_ch_client(settings.clickhouse)
        ch.command("SELECT 1")
    except Exception as exc:
        log.warning("clickhouse unavailable; evidence and dashboards degrade", error=str(exc))
        ch = None
    store = ObjectStore(make_s3_client(settings.s3))
    store.ensure_bucket(settings.s3.bucket_exports)
    store.ensure_bucket(settings.s3.bucket_artifacts)
    redis = Redis.from_url(settings.redis.url)
    return AppState(
        settings=settings,
        session_factory=factory,
        clock=SYSTEM_CLOCK,
        limiter=RateLimiter(
            RedisCounterStore(redis), window_seconds=settings.api.rate_limit_window_seconds
        ),
        evidence=ClickHouseEvidence(ch) if ch is not None else NoEvidence(),
        store=store,
        ch=ch,
        dns_txt=resolvers.dns_txt_lookup(settings),
        http_get=resolvers.http_get,
    )


def application() -> Any:
    """ASGI factory for multi-process uvicorn (`payintel api serve --workers N`).

    Each worker process builds its own engines and clients from the environment.
    """
    from payintel.api.app import create_app
    from payintel.core.settings import get_settings

    return create_app(build_state(get_settings()))

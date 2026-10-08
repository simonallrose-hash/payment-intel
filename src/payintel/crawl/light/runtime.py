"""Wiring for a light-scan worker process: reference data, rules, guards, politeness, sinks.

`build_context` is the one place that turns `Settings` into a ready `ScanContext`;
the CLI (`payintel worker-light`), the benchmark (`scripts/bench_light.py`) and
the integration tests all go through it, overriding only the pieces they need
(resolver, limiter, sinks, HTTP transport).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from clickhouse_connect.driver.client import Client

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.models.base import ProviderRole
from payintel.core.reference_loader import ReferenceData, load_reference
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.crawl.egress import EgressGuard, system_resolve
from payintel.crawl.light.artifacts import ArtifactWriter
from payintel.crawl.light.fetcher import Fetcher
from payintel.crawl.light.worker import ScanContext
from payintel.detect.country import CountryDetector
from payintel.detect.rules import load_rules
from payintel.discovery.classify import EcommerceClassifier
from payintel.discovery.dns import UnboundResolver
from payintel.discovery.parking import ParkingDetector
from payintel.history.writer import ObservationBuffer
from payintel.scheduler.politeness import MemoryRateLimiter, RateLimiter

Resolve = Callable[[str], Awaitable[list[str]]]


def provider_roles(reference: ReferenceData) -> dict[str, ProviderRole]:
    return {p["id"]: ProviderRole(p["role"]) for p in reference.providers}


def unbound_resolve(settings: Settings) -> Resolve:
    """A-record lookup through the local Unbound (NFR-S-09: no system resolver in prod)."""
    resolver = UnboundResolver(
        settings.discovery.dns_nameservers,
        settings.discovery.dns_port,
        settings.discovery.dns_timeout_seconds,
    )

    async def resolve(hostname: str) -> list[str]:
        record = await resolver.resolve(hostname)
        return [*record.a, *record.aaaa]

    return resolve


def build_context(
    settings: Settings,
    *,
    clock: Clock = SYSTEM_CLOCK,
    worker_id: str = "worker-light-0",
    ch_client: Client | None = None,
    store: ObjectStore | None = None,
    limiter: RateLimiter | None = None,
    resolve: Resolve | None = None,
    allow_private: bool = False,
    transport: Any = None,
    sleep: Any = None,
    base_scheme: str = "https",
    reference: ReferenceData | None = None,
) -> ScanContext:
    reference = reference or load_reference()
    ruleset = load_rules(reference=reference)
    guard = EgressGuard(
        resolve or (system_resolve if allow_private else unbound_resolve(settings)),
        allow_private=allow_private,
    )
    fetcher = Fetcher(
        user_agent=settings.identity.user_agent,
        guard=guard,
        limiter=limiter or MemoryRateLimiter(),
        connect_timeout=settings.light.connect_timeout_seconds,
        read_timeout=settings.light.read_timeout_seconds,
        max_bytes=settings.light.max_page_bytes,
        host_rps=settings.scan.max_requests_per_second_per_host,
        ip_rps=settings.scan.max_requests_per_second_per_ip,
        sleep=sleep,
        transport=transport,
    )
    return ScanContext(
        settings=settings,
        ruleset=ruleset,
        provider_roles=provider_roles(reference),
        classifier=EcommerceClassifier(
            ecommerce_threshold=settings.discovery.ecommerce_threshold,
            not_ecommerce_threshold=settings.discovery.not_ecommerce_threshold,
        ),
        country=CountryDetector(),
        parking=ParkingDetector(),
        fetcher=fetcher,
        buffer=ObservationBuffer(ch_client),
        writer=ArtifactWriter(store, settings.s3.bucket_artifacts),
        clock=clock,
        worker_id=worker_id,
        base_scheme=base_scheme,
    )

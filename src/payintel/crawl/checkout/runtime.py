"""Wiring for a checkout worker process: browser, walker, rules, guards, sinks.

`build_checkout_context` is the one place that turns `Settings` into a ready
`CheckoutContext`; the CLI (`payintel worker-checkout`) and the integration
tests go through it, overriding only the pieces they need (transport, rewrite
to a local fixture server, sinks, clock).
"""

from __future__ import annotations

from typing import Any

from clickhouse_connect.driver.client import Client

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.crypto import SecretBox
from payintel.core.models.base import SignalType
from payintel.core.reference_loader import ReferenceData, load_reference
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.crawl.checkout.accounts import AccountManager
from payintel.crawl.checkout.browser import BrowserPool
from payintel.crawl.checkout.dictionary import load_dictionary
from payintel.crawl.checkout.guardrails.injection import Rewriter
from payintel.crawl.checkout.identities import IdentityProvider
from payintel.crawl.checkout.payment_values import load_payment_values
from payintel.crawl.checkout.walker import CheckoutWalker, WalkerConfig
from payintel.crawl.checkout.worker import CheckoutContext, RobotsFetch
from payintel.crawl.egress import EgressGuard, system_resolve
from payintel.crawl.light.artifacts import ArtifactWriter
from payintel.crawl.light.fetcher import Fetcher
from payintel.crawl.light.runtime import Resolve, provider_roles, unbound_resolve
from payintel.detect.country import CountryDetector
from payintel.detect.hosts import HostCategorizer
from payintel.detect.rules import RuleSet, load_rules
from payintel.history.writer import ObservationBuffer
from payintel.scheduler.politeness import AsnPolicy, MemoryRateLimiter, RateLimiter


def global_candidates(ruleset: RuleSet) -> list[str]:
    """Window globals the capture probes for: the root names of every js_global rule."""
    names = {
        r.pattern.split(".")[0].split("(")[0].strip()
        for r in ruleset.enabled()
        if r.signal_type == SignalType.JS_GLOBAL
    }
    return sorted(n for n in names if n)


def walker_config(settings: Settings, *, current_year: int) -> WalkerConfig:
    c = settings.checkout
    return WalkerConfig(
        walk_timeout_s=float(c.walk_timeout_seconds),
        network_idle_timeout_ms=c.network_idle_timeout_seconds * 1000,
        action_timeout_ms=int(c.action_timeout_seconds * 1000),
        max_clicks=c.max_clicks_per_walk,
        max_steps=c.max_steps_per_checkout,
        memory_limit_mb=float(c.context_memory_limit_mb),
        screenshot_max_bytes=c.screenshot_max_bytes,
        stop_detail_max_chars=c.stop_detail_max_chars,
        current_year=current_year,
    )


def account_manager(settings: Settings) -> AccountManager | None:
    """None when no encryption key is configured: registration is then disabled (FR-CW-12)."""
    key = settings.secrets.encryption_key.get_secret_value()
    if not key:
        return None
    return AccountManager(
        SecretBox.from_base64(key), company_domain=settings.identity.company_domain
    )


def build_browser_pool(settings: Settings, *, har: bool = True) -> BrowserPool:
    from pathlib import Path
    from tempfile import mkdtemp

    c = settings.checkout
    return BrowserPool(
        load_dictionary(),
        user_agent=settings.identity.user_agent,
        headless=c.headless,
        restart_every=c.browser_restart_every_walks,
        viewport=(c.viewport_width, c.viewport_height),
        har_dir=Path(mkdtemp(prefix="payintel-har-")) if har else None,
    )


def build_checkout_context(
    settings: Settings,
    *,
    pool: BrowserPool,
    clock: Clock = SYSTEM_CLOCK,
    worker_id: str = "worker-checkout-0",
    ch_client: Client | None = None,
    store: ObjectStore | None = None,
    limiter: RateLimiter | None = None,
    resolve: Resolve | None = None,
    allow_private: bool = False,
    transport: Any = None,
    sleep: Any = None,
    base_scheme: str = "https",
    reference: ReferenceData | None = None,
    asn_policy: AsnPolicy | None = None,
    rewrite: Rewriter | None = None,
    robots_fetch: RobotsFetch | None = None,
    walker: CheckoutWalker | None = None,
    ruleset: RuleSet | None = None,
) -> CheckoutContext:
    reference = reference or load_reference()
    ruleset = ruleset or load_rules(reference=reference)
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
        asn_policy=asn_policy,
    )
    if walker is None:
        cfg = walker_config(settings, current_year=clock.now().year)
        cfg.allow_private = allow_private
        cfg.rewrite = rewrite
        cfg.global_candidates = global_candidates(ruleset)
        walker = CheckoutWalker(
            pool,
            dictionary=load_dictionary(),
            payment_values=load_payment_values(),
            config=cfg,
        )
    return CheckoutContext(
        settings=settings,
        reference=reference,
        ruleset=ruleset,
        provider_roles=provider_roles(reference),
        hosts=HostCategorizer(ruleset),
        country=CountryDetector(),
        fetcher=fetcher,
        buffer=ObservationBuffer(ch_client),
        writer=ArtifactWriter(store, settings.s3.bucket_artifacts),
        walker=walker,
        identities=IdentityProvider(
            company_domain=settings.identity.company_domain,
            company_phone=settings.identity.company_phone,
        ),
        accounts=account_manager(settings),
        clock=clock,
        worker_id=worker_id,
        base_scheme=base_scheme,
        robots_fetch=robots_fetch,
    )

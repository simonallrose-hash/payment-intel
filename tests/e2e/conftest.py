"""Checkout e2e fixtures: one Chromium per session, the shop simulator per module."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from payintel.crawl.checkout.browser import BrowserPool
from payintel.crawl.checkout.dictionary import load_dictionary
from payintel.crawl.checkout.identities import IdentityProvider
from payintel.crawl.checkout.payment_values import load_payment_values
from payintel.crawl.checkout.walker import CheckoutWalker, WalkerConfig
from tests.e2e.shopsim import SimServer, start_server

UA = "PayIntelBot/1.0 (+https://example.invalid/bot)"
IDENTITIES = IdentityProvider(company_domain="payintel.example", company_phone="+44 20 7946 0000")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser_pool(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[BrowserPool]:
    pool = BrowserPool(
        load_dictionary(),
        user_agent=UA,
        headless=True,
        restart_every=200,
        har_dir=Path(tmp_path_factory.mktemp("har")),
    )
    await pool.start()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(scope="module")
def sim() -> Iterator[SimServer]:
    server, httpd = start_server()
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()


def make_walker(
    pool: BrowserPool, sim: SimServer, *, walk_timeout_s: float = 60.0, **overrides: object
) -> CheckoutWalker:
    cfg = WalkerConfig(
        walk_timeout_s=walk_timeout_s,
        network_idle_timeout_ms=3_000,
        action_timeout_ms=4_000,
        allow_private=True,
        rewrite=sim.rewrite,
        global_candidates=["Stripe", "adyen", "Klarna", "braintree", "paypal"],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return CheckoutWalker(
        pool, dictionary=load_dictionary(), payment_values=load_payment_values(), config=cfg
    )

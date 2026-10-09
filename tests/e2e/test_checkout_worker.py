"""Checkout worker slice (4.4): lease → walk against the simulator → PostgreSQL, ClickHouse, S3.

The walks are real Chromium walks; every other service is the real local
stack of `tests/conftest.py`. Shop names carry a real TLD (`woo-de.de`) so
they pass the public-suffix check of the discovery ingest.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from alembic import command as alembic_command
from clickhouse_connect.driver.client import Client
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import FixedClock
from payintel.core.models.base import (
    ChangeEventType,
    Coverage,
    DomainSourceKind,
    DomainStatus,
    ScanStatus,
    ScanType,
    StoreAccountStatus,
)
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan, ScanRun, StoreAccount
from payintel.core.models.store import ChangeEvent, StoreCheckoutHost, StoreProfile, StoreProvider
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.s3 import ObjectStore
from payintel.core.settings import S3Settings, SecretsSettings, Settings
from payintel.crawl.checkout.browser import BrowserPool
from payintel.crawl.checkout.runtime import build_checkout_context
from payintel.crawl.checkout.worker import CheckoutOutcome, CheckoutScanner, run_batch, scrub_har
from payintel.crawl.light.robots import parse_robots
from payintel.discovery.ingest import ingest
from payintel.discovery.sources.base import SourceRecord
from payintel.history.read import read_store
from payintel.scheduler import planner, queue
from payintel.scheduler.politeness import MemoryRateLimiter
from tests.conftest import alembic_config_for
from tests.e2e.conftest import make_walker
from tests.e2e.shopsim import ShopConfig, ShopState, SimServer

KEY_B64 = base64.b64encode(bytes(range(32))).decode()


class LoopbackTransport(httpx.AsyncHTTPTransport):
    """Send every request to the simulator, keeping the original Host header."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self._port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        local = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await super().handle_async_request(local)


async def _loopback(_hostname: str) -> list[str]:
    return ["127.0.0.1"]


def _settings(s3: S3Settings | None = None) -> Settings:
    secrets = SecretsSettings(encryption_key=KEY_B64)
    return Settings(s3=s3, secrets=secrets) if s3 else Settings(secrets=secrets)


@pytest_asyncio.fixture(loop_scope="session")
async def scanner(
    browser_pool: BrowserPool,
    sim: SimServer,
    fixed_clock: FixedClock,
    ch_client: Client,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> AsyncIterator[CheckoutScanner]:
    sim.shops.setdefault(
        "blocked-shop",
        ShopState(ShopConfig(name="blocked-shop", flavour="generic", protection="403")),
    )
    ctx = build_checkout_context(
        _settings(s3_settings),
        pool=browser_pool,
        clock=fixed_clock,
        worker_id="w-checkout",
        ch_client=ch_client,
        store=object_store,
        limiter=MemoryRateLimiter(),
        resolve=_loopback,
        allow_private=True,
        transport=LoopbackTransport(sim.port),
        base_scheme="http",
        rewrite=sim.rewrite,
        walker=make_walker(browser_pool, sim),
    )
    yield CheckoutScanner(ctx)
    await ctx.fetcher.aclose()


def _prepare(session: Session, clock: FixedClock, hostnames: list[str]) -> None:
    sync_reference(session, load_reference(), clock=clock)
    ingest(
        session,
        [SourceRecord(h, rank=i + 1) for i, h in enumerate(hostnames)],
        source=DomainSourceKind.MANUAL,
        origin="t",
        clock=clock,
    )
    for d in session.execute(select(Domain)).scalars():
        d.status = DomainStatus.ECOMMERCE
    session.flush()
    planner.ensure_plans(session, ScanType.CHECKOUT, s=Settings().scan, clock=clock)


def _lease(session: Session, clock: FixedClock, hostname: str) -> ScanPlan:
    host = session.execute(select(Host).where(Host.hostname == hostname)).scalar_one()
    leased = queue.lease(
        session, ScanType.CHECKOUT, "w-checkout", limit=10, lease_seconds=600, clock=clock
    )
    plan = next(p for p in leased if p.host_id == host.id)
    return plan


async def _scan(scanner: CheckoutScanner, session: Session, plan: ScanPlan) -> CheckoutOutcome:
    return await scanner.scan(session, plan)


def _keys(store: ObjectStore, bucket: str, prefix: str) -> set[str]:
    resp = store._client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return {o["Key"] for o in resp.get("Contents", [])}


@pytest.mark.asyncio(loop_scope="session")
async def test_reached_payment_step_persists_everything(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: CheckoutScanner,
    sim: SimServer,
    ch_client: Client,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> None:
    sim.reset("woo-de")
    _prepare(db_session, fixed_clock, ["woo-de.de"])
    plan = _lease(db_session, fixed_clock, "woo-de.de")
    out = await _scan(scanner, db_session, plan)

    assert out.status == ScanStatus.REACHED_PAYMENT_STEP and out.coverage == Coverage.PAYMENT_STEP
    assert out.stop is None and out.walk is not None and out.walk.adapter == "woocommerce"
    providers = {p.target_id for p in out.providers}
    assert "stripe" in providers, providers
    assert {"paypal", "visa"} <= {m.target_id for m in out.methods}
    assert {h.etld1 for h in out.hosts} >= {"stripe.com"}
    assert any(e["kind"] == "platform_detected" for e in out.walk.journal.as_list())
    assert out.identity_country == "DE"
    assert sim.state("woo-de").counters.forbidden() == {
        "orders_placed": 0,
        "newsletters": 0,
        "captcha_solves": 0,
        "foreign_logins": 0,
        "needless_registrations": 0,
    }

    # PostgreSQL: run, profile, current state, schedule
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.scan_type == ScanType.CHECKOUT
    assert run.status == ScanStatus.REACHED_PAYMENT_STEP and run.coverage == Coverage.PAYMENT_STEP
    assert run.checkout_country == "DE" and run.artifact_prefix == out.artifact_prefix
    profile = db_session.get(StoreProfile, out.host_id)
    assert profile is not None
    assert profile.checkout_status == ScanStatus.REACHED_PAYMENT_STEP
    assert profile.coverage == Coverage.PAYMENT_STEP
    assert profile.last_checkout_scan_at == fixed_clock.now()
    assert profile.last_light_scan_at is None
    assert profile.platform_id == "woocommerce"
    rows = (
        db_session.execute(select(StoreProvider).where(StoreProvider.host_id == out.host_id))
        .scalars()
        .all()
    )
    assert {r.provider_id for r in rows} >= providers
    assert all(
        r.confirmations == 1 and r.active_on_checkout for r in rows if r.provider_id in providers
    )
    db_session.refresh(plan)
    assert plan.locked_until is None and plan.fail_count == 0
    assert plan.next_scan_at == fixed_clock.now() + timedelta(days=30)
    assert out.diff is not None and out.diff.events == []  # first sighting: no event yet

    # S3: manifest, payment-step DOM, screenshot, scrubbed HAR
    keys = _keys(object_store, s3_settings.bucket_artifacts, out.artifact_prefix)
    names = {k.removeprefix(out.artifact_prefix) for k in keys}
    assert {
        "manifest.json",
        "payment_step.html.gz",
        "payment_block.html.gz",
        "screenshot.jpg",
    } <= names
    assert "har.json.gz" in names and "stop.jpg" not in names
    manifest = json.loads(
        object_store.get_bytes(s3_settings.bucket_artifacts, out.artifact_prefix + "manifest.json")
    )
    assert manifest["status"] == "reached_payment_step" and manifest["stop"] is None
    assert [s["name"] for s in manifest["steps"]][:4] == [
        "navigation",
        "product",
        "cart",
        "checkout",
    ]
    kinds = {e["kind"] for e in manifest["journal"]}
    assert {"identity", "goto", "fill", "click"} <= kinds
    assert manifest["network"] and all("host" in e for e in manifest["network"])
    har = json.loads(
        gzip.decompress(
            object_store.get_bytes(
                s3_settings.bucket_artifacts, out.artifact_prefix + "har.json.gz"
            )
        )
    )
    for entry in har["log"]["entries"]:
        assert entry["request"]["cookies"] == [] and entry["response"]["cookies"] == []
        names_ = {h["name"].lower() for h in entry["request"]["headers"]}
        assert "cookie" not in names_ and "authorization" not in names_
        assert "postData" not in entry["request"]
    dom = gzip.decompress(
        object_store.get_bytes(
            s3_settings.bucket_artifacts, out.artifact_prefix + "payment_step.html.gz"
        )
    ).decode()
    assert "PayPal" in dom and "checkout-probe" not in dom  # sanitised: the probe e-mail is masked

    # ClickHouse: observations
    scanner.ctx.buffer.flush()
    scans = ch_client.query(
        "SELECT scan_type, status, coverage, adapter FROM obs_scan WHERE etld1 = 'woo-de.de'"
    ).result_rows
    assert scans == [("checkout", "reached_payment_step", "payment_step", "woocommerce")]
    prov = ch_client.query(
        "SELECT DISTINCT provider_id, active_on_checkout FROM obs_provider WHERE etld1 = 'woo-de.de'"
    ).result_rows
    assert ("stripe", 1) in prov
    methods = ch_client.query(
        "SELECT DISTINCT method_id FROM obs_payment_method WHERE etld1 = 'woo-de.de'"
    ).result_rows
    assert {"paypal", "visa"} <= {r[0] for r in methods}
    hosts = ch_client.query(
        "SELECT third_party_etld1, category, first_seen FROM obs_checkout_host WHERE host_id = %(h)s",
        parameters={"h": out.host_id},
    ).result_rows
    assert ("stripe.com", "psp", 1) in hosts
    stops = ch_client.query(
        "SELECT count() FROM obs_scan_stop WHERE etld1 = 'woo-de.de'"
    ).result_rows
    assert stops == [(0,)]


@pytest.mark.asyncio(loop_scope="session")
async def test_requested_trace_is_recorded_once_and_stored(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: CheckoutScanner,
    sim: SimServer,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> None:
    """FR-QA-06: a manual re-run with `trace_requested` stores trace.zip and clears the flag."""
    sim.reset("shopify-de")
    _prepare(db_session, fixed_clock, ["shopify-de.de"])
    host = db_session.execute(select(Host).where(Host.hostname == "shopify-de.de")).scalar_one()
    plan = db_session.execute(
        select(ScanPlan).where(ScanPlan.host_id == host.id, ScanPlan.scan_type == ScanType.CHECKOUT)
    ).scalar_one()
    plan.trace_requested = True
    plan.requested_by = "analyst@payintel.test"
    db_session.flush()
    plan = _lease(db_session, fixed_clock, "shopify-de.de")
    out = await _scan(scanner, db_session, plan)
    assert out.status == ScanStatus.REACHED_PAYMENT_STEP and out.walk is not None
    assert out.walk.trace is not None and out.walk.trace.startswith(b"PK")
    assert any(e["kind"] == "trace" for e in out.walk.journal.as_list())
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.trace_key == f"{out.artifact_prefix}trace.zip"
    keys = _keys(object_store, s3_settings.bucket_artifacts, out.artifact_prefix)
    assert run.trace_key in keys
    manifest = json.loads(
        object_store.get_bytes(s3_settings.bucket_artifacts, f"{out.artifact_prefix}manifest.json")
    )
    assert manifest["trace_key"] == run.trace_key
    db_session.refresh(plan)
    assert plan.trace_requested is False and plan.requested_by is None


@pytest.mark.asyncio(loop_scope="session")
async def test_two_scans_confirm_state_and_emit_events(
    db_session: Session, fixed_clock: FixedClock, scanner: CheckoutScanner, sim: SimServer
) -> None:
    sim.reset("woo-de")
    _prepare(db_session, fixed_clock, ["woo-de.de"])
    first = await _scan(scanner, db_session, _lease(db_session, fixed_clock, "woo-de.de"))
    assert first.reached_payment and first.diff is not None and not first.diff.events
    fixed_clock.advance(days=31)
    second = await _scan(scanner, db_session, _lease(db_session, fixed_clock, "woo-de.de"))
    assert second.reached_payment and second.scan_run_id != first.scan_run_id
    assert second.diff is not None
    events = (
        db_session.execute(select(ChangeEvent).where(ChangeEvent.host_id == second.host_id))
        .scalars()
        .all()
    )
    types = {e.event_type for e in events}
    assert ChangeEventType.PROVIDER_ADDED in types
    assert {e.entity_id for e in events if e.event_type == ChangeEventType.PROVIDER_ADDED} >= {
        "stripe"
    }
    rows = (
        db_session.execute(select(StoreProvider).where(StoreProvider.host_id == second.host_id))
        .scalars()
        .all()
    )
    assert all(r.confirmations == 2 and r.last_seen == fixed_clock.now().date() for r in rows)
    hosts = (
        db_session.execute(
            select(StoreCheckoutHost).where(StoreCheckoutHost.host_id == second.host_id)
        )
        .scalars()
        .all()
    )
    assert {h.third_party_etld1 for h in hosts} >= {"stripe.com"}
    assert next(h for h in hosts if h.third_party_etld1 == "stripe.com").provider_id == "stripe"
    assert all(h.confirmations == 2 for h in hosts)
    # NFR-R-06: the current state is readable from PostgreSQL alone
    state = read_store(db_session, second.host_id)
    assert state is not None and state.profile["checkout_status"] == "reached_payment_step"
    assert any(p["provider_id"] == "stripe" for p in state.providers)
    assert {e["event_type"] for e in state.recent_events} >= {"provider_added", "method_added"}
    assert "platform_changed" not in {e["event_type"] for e in state.recent_events}


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_persists_a_system_account(
    db_session: Session, fixed_clock: FixedClock, scanner: CheckoutScanner, sim: SimServer
) -> None:
    sim.shops.setdefault(
        "wall-shop",
        ShopState(
            ShopConfig(name="wall-shop", flavour="shopware6", guest="none", registration="ok")
        ),
    )
    sim.reset("wall-shop")
    _prepare(db_session, fixed_clock, ["wall-shop.de"])
    out = await _scan(scanner, db_session, _lease(db_session, fixed_clock, "wall-shop.de"))
    assert out.reached_payment and out.walk is not None and out.walk.registration is not None
    acc = db_session.execute(
        select(StoreAccount).where(StoreAccount.host_id == out.host_id)
    ).scalar_one()
    assert acc.status == StoreAccountStatus.ACTIVE and acc.created_scan_run_id == out.scan_run_id
    assert acc.email.startswith("checkout-probe+h") and acc.password_encrypted.startswith("v1:")
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.used_account
    # the next walk logs in with the stored account instead of registering again
    fixed_clock.advance(days=31)
    again = await _scan(scanner, db_session, _lease(db_session, fixed_clock, "wall-shop.de"))
    assert again.walk is not None
    assert again.reached_payment, (again.stop, again.walk.journal.as_list()[-8:])
    assert again.walk.used_account and again.walk.registration is None
    assert sim.state("wall-shop").counters.needless_registrations == 0
    db_session.refresh(acc)
    assert acc.last_used_at == fixed_clock.now()


@pytest.mark.asyncio(loop_scope="session")
async def test_blocked_shop_gets_the_cooldown_and_a_stop_record(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: CheckoutScanner,
    ch_client: Client,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> None:
    _prepare(db_session, fixed_clock, ["blocked-shop.de"])
    plan = _lease(db_session, fixed_clock, "blocked-shop.de")
    out = await _scan(scanner, db_session, plan)
    assert out.status == ScanStatus.BLOCKED and out.stop is not None
    assert out.stop.step.value == "protection" and out.stop.reason == "http_403_429"
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.stop_step == "protection" and run.stop_reason == "http_403_429"
    profile = db_session.get(StoreProfile, out.host_id)
    assert profile is not None and profile.checkout_status == ScanStatus.BLOCKED
    assert profile.coverage is None  # a block says nothing about how far the shop can be walked
    db_session.refresh(plan)
    assert plan.next_scan_at == fixed_clock.now() + timedelta(days=30)
    assert plan.last_error is not None and plan.last_error.startswith("http_403_429")
    keys = {
        k.removeprefix(out.artifact_prefix)
        for k in _keys(object_store, s3_settings.bucket_artifacts, out.artifact_prefix)
    }
    assert {"manifest.json", "stop.jpg", "stop.html.gz"} <= keys
    scanner.ctx.buffer.flush()
    rows = ch_client.query(
        "SELECT stop_step, stop_reason, artifact_key, steps.name FROM obs_scan_stop WHERE etld1 = 'blocked-shop.de'"
    ).result_rows
    assert len(rows) == 1 and rows[0][:2] == ("protection", "http_403_429")
    assert rows[0][2].endswith("stop.jpg") and "navigation" in rows[0][3]
    assert out.diff is not None and out.diff.ignored


@pytest.mark.asyncio(loop_scope="session")
async def test_robots_disallow_never_opens_the_browser(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: CheckoutScanner,
    browser_pool: BrowserPool,
) -> None:
    async def robots(_base: str) -> object:
        return parse_robots("User-agent: *\nDisallow: /\n", 200, agent_token="payintelbot")

    scanner.ctx.robots_fetch = robots  # type: ignore[assignment]
    _prepare(db_session, fixed_clock, ["woo-de.de"])
    walks_before = browser_pool.walks_since_restart
    out = await _scan(scanner, db_session, _lease(db_session, fixed_clock, "woo-de.de"))
    assert out.status == ScanStatus.BLOCKED and out.walk is None
    assert out.stop is not None and out.stop.reason == "robots_disallowed"
    assert browser_pool.walks_since_restart == walks_before
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.stop_reason == "robots_disallowed"


@pytest.mark.asyncio(loop_scope="session")
async def test_run_batch_commits_and_flushes(
    fresh_database: str,
    fixed_clock: FixedClock,
    scanner: CheckoutScanner,
    sim: SimServer,
    ch_client: Client,
) -> None:
    alembic_command.upgrade(alembic_config_for(fresh_database), "head")
    engine = create_engine(fresh_database, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sim.reset("woo-de")
    sim.reset("generic-en")
    names = ["woo-de.de", "generic-en.com", "blocked-shop.de"]
    try:
        with factory() as setup:
            _prepare(setup, fixed_clock, names)
            setup.commit()
        outcomes = await run_batch(factory, scanner, limit=10, concurrency=3)
        assert {o.etld1: o.status for o in outcomes} == {
            "woo-de.de": ScanStatus.REACHED_PAYMENT_STEP,
            "generic-en.com": ScanStatus.REACHED_PAYMENT_STEP,
            "blocked-shop.de": ScanStatus.BLOCKED,
        }
        assert scanner.ctx.buffer.pending == 0
        rows = ch_client.query("SELECT etld1, status FROM obs_scan ORDER BY etld1").result_rows
        assert [r[0] for r in rows] == sorted(names)
        with factory() as check:
            runs = check.execute(select(ScanRun).where(ScanRun.worker_id == "w-checkout")).scalars()
            assert len(list(runs)) == 3
            plans = check.execute(select(ScanPlan).join(Host).where(Host.hostname.in_(names)))
            assert all(p.locked_until is None for p in plans.scalars().all())
    finally:
        engine.dispose()


def test_scrub_har_drops_cookies_auth_and_bodies() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "headers": [
                            {"name": "Cookie", "value": "sid=1"},
                            {"name": "Accept", "value": "*/*"},
                        ],
                        "cookies": [{"name": "sid", "value": "1"}],
                        "postData": {"text": "card=4242"},
                    },
                    "response": {
                        "headers": [
                            {"name": "Set-Cookie", "value": "sid=2"},
                            {"name": "Content-Type", "value": "text/html"},
                        ],
                        "cookies": [{"name": "sid", "value": "2"}],
                        "content": {"size": 3, "text": "abc"},
                    },
                }
            ]
        }
    }
    out = scrub_har(json.dumps(har).encode())
    assert out is not None
    doc = json.loads(out)
    e = doc["log"]["entries"][0]
    assert e["request"]["headers"] == [{"name": "Accept", "value": "*/*"}]
    assert e["response"]["headers"] == [{"name": "Content-Type", "value": "text/html"}]
    assert e["request"]["cookies"] == [] and e["response"]["cookies"] == []
    assert "postData" not in e["request"] and "text" not in e["response"]["content"]
    assert scrub_har(b"not json") is None

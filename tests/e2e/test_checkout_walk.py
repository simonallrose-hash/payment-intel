"""Checkout walks against the simulator (plan §4): AC-04, AC-05, AC-16 and the happy paths."""

from __future__ import annotations

import asyncio
import os

import pytest

from payintel.core.models.base import Coverage, ScanStatus, StoreAccountStatus
from payintel.core.reference_loader import load_reference
from payintel.crawl.checkout.accounts import Credentials
from payintel.crawl.checkout.browser import BrowserPool
from payintel.crawl.checkout.payment_values import load_payment_values
from payintel.crawl.checkout.stops import normalise_stop
from payintel.crawl.checkout.types import WalkFlags, WalkStep
from payintel.crawl.checkout.walker import WalkInput, WalkResult
from tests.e2e.conftest import IDENTITIES, make_walker
from tests.e2e.shopsim import SimServer

TAX = load_reference().stop_reasons
PLATFORM_OF = {
    "woocommerce": "woocommerce",
    "magento2": "magento2",
    "shopware6": "shopware6",
    "prestashop": "prestashop",
    "generic": None,
}
TRAP_RUNS = int(os.environ.get("PAYINTEL_TEST_TRAP_RUNS", "100"))  # AC-04 run: 1000
TRAP_CONCURRENCY = int(os.environ.get("PAYINTEL_TEST_TRAP_CONCURRENCY", "8"))


def _input(
    sim: SimServer,
    shop: str,
    *,
    flags: WalkFlags | None = None,
    credentials: Credentials | None = None,
    token: str = "run-1",  # noqa: S107 - e-mail token, not a credential
) -> WalkInput:
    cfg = sim.state(shop).config
    country = {"de": "DE", "en": "GB", "fr": "FR"}[cfg.language]
    return WalkInput(
        url=sim.url(shop),
        etld1=f"{shop}.test",
        identity=IDENTITIES.for_country(country, email_token=token),
        flags=flags or WalkFlags(),
        url_check=lambda url: None,
        platform_id=PLATFORM_OF[cfg.flavour],
        credentials=credentials,
    )


def _assert_clean(sim: SimServer, shop: str, r: WalkResult) -> None:
    """No forbidden action on the server, none attempted from the browser (FR-CW-04)."""
    c = sim.state(shop).counters
    assert c.forbidden() == {
        "orders_placed": 0,
        "newsletters": 0,
        "captcha_solves": 0,
        "foreign_logins": 0,
        "needless_registrations": 0,
    }, c.forbidden()
    assert not any(e.kind == "click" and "final" in str(e.detail) for e in r.journal.entries)
    allowed = load_payment_values().numbers
    assert all(n in allowed for n in c.card_numbers), c.card_numbers
    assert r.journal.of("identity")


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("shop", ["woo-de", "magento-en", "shopware-de", "presta-fr", "generic-en"])
async def test_each_adapter_reaches_the_payment_step(
    browser_pool: BrowserPool, sim: SimServer, shop: str
) -> None:
    sim.reset(shop)
    walker = make_walker(browser_pool, sim)
    r = await walker.walk(_input(sim, shop))
    assert r.stop is None, (r.stop, r.journal.as_list()[-6:])
    assert r.status == ScanStatus.REACHED_PAYMENT_STEP and r.coverage == Coverage.PAYMENT_STEP
    assert r.methods_revealed and r.capture is not None
    labels = " ".join(r.capture.labels)
    assert "PayPal" in labels and "Visa" in labels
    assert r.capture.payment_block_html and r.capture.screenshot_jpeg
    assert [s.name for s in r.steps][:4] == ["navigation", "product", "cart", "checkout"]
    assert r.steps[-1].name == "payment"
    assert r.adapter == (PLATFORM_OF[sim.state(shop).config.flavour] or "heuristic")
    assert r.har and len(r.har) > 100
    _assert_clean(sim, shop, r)
    fills = {e.detail["field"] for e in r.journal.of("fill")}
    assert {"first_name", "last_name", "street_line", "postcode", "city", "email"} <= fills
    # the journal never carries a real value: all fills come from the synthetic identity
    assert all(e.detail["value"] != "" for e in r.journal.of("fill"))


@pytest.mark.asyncio(loop_scope="session")
async def test_trap_shop_never_triggers_a_forbidden_action(
    browser_pool: BrowserPool, sim: SimServer
) -> None:
    """AC-04: N walks of the trap shop, 0 orders / newsletters / captcha / foreign logins."""
    shop = "trap_shop"
    sim.reset(shop)
    walker = make_walker(browser_pool, sim)
    sem = asyncio.Semaphore(TRAP_CONCURRENCY)
    outcomes: list[WalkResult] = []

    async def one(i: int) -> None:
        async with sem:
            outcomes.append(await walker.walk(_input(sim, shop, token=f"trap-{i}")))

    await asyncio.gather(*(one(i) for i in range(TRAP_RUNS)))
    c = sim.state(shop).counters
    assert c.forbidden() == {
        "orders_placed": 0,
        "newsletters": 0,
        "captcha_solves": 0,
        "foreign_logins": 0,
        "needless_registrations": 0,
    }, c.forbidden()
    assert not any((m, p) == ("POST", "/order/submit") for m, s, p in sim.requests if s == shop)
    reached = sum(1 for r in outcomes if r.reached_payment)
    assert reached == TRAP_RUNS, [r.stop for r in outcomes if r.stop][:3]
    refused = sum(len(r.journal.of("click_refused")) for r in outcomes)
    blocked = sum(len(r.dom_blocked) for r in outcomes)
    assert refused == 0  # decoys are skipped in favour of the allowed step control
    assert blocked == 0  # nothing even tried to submit an order form
    assert all(not r.blocked_posts for r in outcomes)


@pytest.mark.asyncio(loop_scope="session")
async def test_trap_decoys_are_refused_when_they_are_the_only_option(
    browser_pool: BrowserPool, sim: SimServer
) -> None:
    """The browser-side guard and the router hold even when a decoy is clicked by a bug."""
    shop = "trap_shop"
    sim.reset(shop)
    walker = make_walker(browser_pool, sim)
    r = await walker.walk(_input(sim, shop))
    assert r.reached_payment
    page_url = r.final_url
    # Simulate a defect: dispatch clicks on every decoy through the raw page API.
    wc = await browser_pool.new_walk_context(
        __import__("payintel.crawl.checkout.browser", fromlist=["ContextOptions"]).ContextOptions(
            allow_private=True, rewrite=sim.rewrite
        )
    )
    page = await wc.new_page()
    await page.goto(page_url)
    await page.goto(sim.url(shop))
    for sel in (
        "#confirmOrderForm button",
        "button[name=place_order]",
        "button.btn-pay",
        "a[data-action=place-order]",
        "button[aria-label]",
        "input[type=submit][value]",
    ):
        loc = page.locator(sel).first
        try:
            await loc.click(timeout=2_000, force=True)
        except Exception as exc:  # hidden / detached decoys: the click itself may fail
            print(f"decoy {sel}: {exc.__class__.__name__}")
    await page.wait_for_timeout(500)
    blocked = await page.evaluate("() => window.__payintel.blocked.length")
    await wc.close()
    assert blocked >= 3
    assert sim.state(shop).counters.orders_placed == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_multistep_checkout_counts_steps(browser_pool: BrowserPool, sim: SimServer) -> None:
    sim.reset("multistep_checkout")
    r = await make_walker(browser_pool, sim).walk(_input(sim, "multistep_checkout"))
    assert r.reached_payment, r.stop
    names = [s.name for s in r.steps]
    assert names.count("address") >= 1 and names.count("shipping") >= 1
    assert len(r.journal.of("click")) >= 4  # add, (shipping), 3 step presses
    _assert_clean(sim, "multistep_checkout", r)


@pytest.mark.asyncio(loop_scope="session")
async def test_no_guest_registers_once_then_logs_in(
    browser_pool: BrowserPool, sim: SimServer
) -> None:
    """FR-CW-12: first walk registers a system account, second walk logs into it."""
    shop = "no_guest_registration"
    sim.reset(shop)
    walker = make_walker(browser_pool, sim)
    r1 = await walker.walk(_input(sim, shop))
    assert r1.reached_payment, (r1.stop, r1.journal.as_list()[-8:])
    assert r1.registration is not None and r1.registration.status == StoreAccountStatus.ACTIVE
    assert r1.used_account
    st = sim.state(shop)
    assert st.counters.registrations == [r1.registration.email]
    assert r1.registration.email.startswith("checkout-probe+")
    assert st.counters.newsletters == 0 and st.counters.needless_registrations == 0
    cred = Credentials(
        r1.registration.email, r1.registration.password, 1, StoreAccountStatus.ACTIVE
    )
    r2 = await walker.walk(_input(sim, shop, credentials=cred))
    assert r2.reached_payment, (r2.stop, r2.journal.as_list()[-8:])
    assert r2.registration is None and r2.used_account
    assert st.counters.registrations == [r1.registration.email]
    assert st.counters.logins == [cred.email] and st.counters.foreign_logins == 0
    pw_entries = [e for e in r1.journal.of("fill") if e.detail["field"] == "account_password"]
    assert pw_entries and all(e.detail["value"] == "***" for e in pw_entries)
    _assert_clean(sim, shop, r2)


@pytest.mark.asyncio(loop_scope="session")
async def test_guest_available_means_no_registration(
    browser_pool: BrowserPool, sim: SimServer
) -> None:
    shop = "guest_available"
    sim.reset(shop)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop))
    assert r.reached_payment, r.stop
    assert r.registration is None and not r.used_account
    assert sim.state(shop).counters.registrations == []
    _assert_clean(sim, shop, r)


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_disabled_flag_stops_at_the_wall(
    browser_pool: BrowserPool, sim: SimServer
) -> None:
    shop = "no_guest_registration"
    sim.reset(shop)
    flags = WalkFlags(allow_account_registration=False)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop, flags=flags))
    assert r.stop is not None
    assert (r.stop.step, r.stop.reason) == (
        WalkStep.CHECKOUT,
        "guest_unavailable_registration_disabled",
    )
    assert r.status == ScanStatus.LOGIN_REQUIRED and r.coverage == Coverage.CART
    assert sim.state(shop).counters.registrations == []


@pytest.mark.asyncio(loop_scope="session")
async def test_payment_fields_required(browser_pool: BrowserPool, sim: SimServer) -> None:
    """FR-CW-14: methods appear only after the documented test number is typed."""
    shop = "payment_fields_required"
    sim.reset(shop)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop))
    assert r.reached_payment, r.stop
    assert r.tokenizer == "stripe" and not r.methods_revealed
    assert r.payment_fill is not None and r.payment_fill.number_filled
    c = sim.state(shop).counters
    assert c.tokenizer_calls == 1 and c.card_numbers == [load_payment_values().preferred[0]]
    assert r.capture is not None and "PayPal" in " ".join(r.capture.labels)
    assert "js.tokenizer.test" in r.recorder.hosts()
    _assert_clean(sim, shop, r)
    # flag off: the walk still reaches the payment step, but types nothing
    sim.reset(shop)
    flags = WalkFlags(allow_payment_field_fill=False)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop, flags=flags))
    assert r.reached_payment and r.status == ScanStatus.REACHED_PAYMENT_STEP
    assert r.payment_fill is not None and r.payment_fill.filled == []
    assert sim.state(shop).counters.tokenizer_calls == 0
    assert r.journal.of("payment_fill_refused")


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("shop", "reason", "status"),
    [
        (
            "registration_email_verification",
            "email_verification_pending",
            StoreAccountStatus.PENDING_VERIFICATION,
        ),
        ("registration_captcha", "registration_captcha", None),
        ("registration_sms", "registration_sms_required", None),
        ("registration_documents", "registration_documents_required", None),
        ("registration_payment", "registration_payment_required", None),
    ],
)
async def test_registration_outcomes(
    browser_pool: BrowserPool,
    sim: SimServer,
    shop: str,
    reason: str,
    status: StoreAccountStatus | None,
) -> None:
    sim.reset(shop)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop))
    assert r.stop is not None and r.stop.step == WalkStep.REGISTRATION
    assert r.stop.reason == reason, (r.stop, r.journal.as_list()[-5:])
    c = sim.state(shop).counters
    assert c.captcha_solves == 0 and c.newsletters == 0
    if status is None:
        assert r.registration is None or r.registration.status == StoreAccountStatus.FAILED
    else:
        assert r.registration is not None and r.registration.status == status
        assert r.status == ScanStatus.REGISTRATION_PENDING_VERIFICATION
    assert r.screenshot and r.dom


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_closed(browser_pool: BrowserPool, sim: SimServer) -> None:
    sim.reset("registration_closed")
    r = await make_walker(browser_pool, sim).walk(_input(sim, "registration_closed"))
    assert r.stop is not None and r.stop.step == WalkStep.REGISTRATION
    assert r.stop.reason == "registration_form_unknown"
    assert r.status == ScanStatus.LOGIN_REQUIRED


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("shop", "reason", "vendor"),
    [
        ("cloudflare_challenge", "antibot_challenge", "cloudflare"),
        ("http_403", "http_403_429", "akamai"),
        ("http_429", "http_403_429", "http_429"),
        ("captcha_page", "captcha", "hcaptcha"),
        ("geo_block", "geo_blocked", "geo"),
        ("age_gate", "age_gate", "age_gate"),
    ],
)
async def test_protection_pages_are_blocked_stops(
    browser_pool: BrowserPool, sim: SimServer, shop: str, reason: str, vendor: str
) -> None:
    """AC-05: the walk stops at `protection`, status blocked, nothing is attempted."""
    sim.reset(shop)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop))
    assert r.stop is not None and r.stop.step == WalkStep.PROTECTION
    assert r.stop.reason == reason and r.blocked_by == vendor
    assert r.status == ScanStatus.BLOCKED and r.coverage == Coverage.HOMEPAGE
    assert TAX.is_valid("protection", r.stop.reason, r.stop.detail)
    assert r.screenshot and r.dom and r.journal.of("stop")
    assert sim.state(shop).counters.captcha_solves == 0
    assert len(r.journal.of("click")) == 0


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("shop", "step", "reason", "status"),
    [
        ("stop_http_error", WalkStep.NAVIGATION, "http_error", ScanStatus.ERROR),
        ("stop_navigation_error", WalkStep.NAVIGATION, "navigation_error", ScanStatus.ERROR),
        ("stop_no_product", WalkStep.PRODUCT, "no_product_found", ScanStatus.NO_PRODUCT_FOUND),
        ("stop_out_of_stock", WalkStep.PRODUCT, "out_of_stock", ScanStatus.NO_PRODUCT_FOUND),
        (
            "stop_price_on_request",
            WalkStep.PRODUCT,
            "price_on_request",
            ScanStatus.NO_PRODUCT_FOUND,
        ),
        ("stop_cart_empty", WalkStep.CART, "cart_empty_after_add", ScanStatus.ADD_TO_CART_FAILED),
        (
            "stop_add_to_cart_failed",
            WalkStep.CART,
            "cart_empty_after_add",
            ScanStatus.ADD_TO_CART_FAILED,
        ),
        ("stop_min_order", WalkStep.CART, "min_order_value", ScanStatus.REACHED_CART),
        (
            "stop_checkout_not_found",
            WalkStep.CHECKOUT,
            "checkout_not_found",
            ScanStatus.REACHED_CART,
        ),
        ("stop_login_failed", WalkStep.CHECKOUT, "login_failed", ScanStatus.LOGIN_REQUIRED),
        (
            "stop_required_field_unmapped",
            WalkStep.ADDRESS,
            "required_field_unmapped",
            ScanStatus.REACHED_CHECKOUT,
        ),
        (
            "stop_address_validation",
            WalkStep.ADDRESS,
            "required_field_unmapped",
            ScanStatus.REACHED_CHECKOUT,
        ),
        (
            "stop_no_shipping_option",
            WalkStep.SHIPPING,
            "no_shipping_option",
            ScanStatus.REACHED_CHECKOUT,
        ),
        (
            "stop_payment_step_not_detected",
            WalkStep.PAYMENT,
            "payment_step_not_detected",
            ScanStatus.REACHED_CHECKOUT,
        ),
    ],
)
async def test_every_stop_step_has_a_fixture(
    browser_pool: BrowserPool,
    sim: SimServer,
    shop: str,
    step: WalkStep,
    reason: str,
    status: ScanStatus,
) -> None:
    """AC-16: a stop at every step of the taxonomy with screenshot + DOM evidence."""
    sim.reset(shop)
    cred = None
    if shop == "stop_login_failed":
        email = IDENTITIES.for_country("GB", email_token="run-1").email
        sim.state(shop).accounts[email] = "stored-password-Aa1"
        cred = Credentials(email, "stored-password-Aa1", 1, StoreAccountStatus.ACTIVE)
    r = await make_walker(browser_pool, sim).walk(_input(sim, shop, credentials=cred))
    assert r.stop is not None, r.journal.as_list()[-5:]
    assert (r.stop.step, r.stop.reason) == (step, reason), (r.stop, r.journal.as_list()[-6:])
    assert r.status == status
    norm = normalise_stop(r.stop, TAX, detail_max_chars=500)
    assert norm.reason == reason and TAX.is_valid(step.value, norm.reason, norm.detail)
    assert r.dom, "DOM evidence missing"
    if reason != "navigation_error":
        assert r.screenshot, "screenshot evidence missing"
    _assert_clean(sim, shop, r)


@pytest.mark.asyncio(loop_scope="session")
async def test_walk_deadline_is_a_timeout_stop(browser_pool: BrowserPool, sim: SimServer) -> None:
    sim.reset("stop_timeout")
    r = await make_walker(browser_pool, sim, walk_timeout_s=4.0).walk(_input(sim, "stop_timeout"))
    assert r.stop is not None and r.stop.reason == "timeout" and r.status == ScanStatus.TIMEOUT
    assert "checkout" in r.stop.detail
    assert r.coverage == Coverage.CART  # furthest step started was checkout
    assert r.duration_ms < 4_000 + 2 * 3_000 + 2_000  # deadline + bounded evidence capture

"""Observation capture (FR-CW-07/08) against a local page: read-only, bounded artefacts."""

from __future__ import annotations

import pytest

from payintel.crawl.checkout.browser import WalkContext
from payintel.crawl.checkout.capture import (
    NetworkRecorder,
    api_favicon_fetcher,
    capture_page,
    network_rows,
    screenshot_jpeg,
)
from tests.unit.checkout.conftest import PageServer

PAGE = """<!doctype html><html><head><title>Kasse</title>
<link rel="icon" href="/favicon.ico">
<script src="https://js.stripe.com/v3/"></script>
<script>window.Stripe = function(){}; window.adyen = {checkout: 1};</script>
</head><body>
<h1>Checkout</h1><p>Total: 49,90 €</p>
<form action="/?wc-ajax=checkout" id="order_review">
<div class="woocommerce-checkout-payment" id="payment">
  <ul class="wc_payment_methods">
    <li><input type="radio" name="payment_method" value="stripe"><label>Credit card (Stripe)</label>
        <img src="/img/visa-logo.svg" alt="Visa"></li>
    <li><input type="radio" name="payment_method" value="paypal"><label>PayPal</label></li>
    <li><input type="radio" name="payment_method" value="klarna_pay_later">
        <label>Rechnung</label></li>
  </ul>
  <iframe src="https://js.stripe.com/v3/elements-inner-card.html"
          name="__privateStripeFrame1"></iframe>
</div>
<button id="place_order">Place order</button></form>
<img src="https://cdn.analytics.example/pixel.gif" alt="">
</body></html>"""


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_page_reads_signals_and_artefacts(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page_server.pages["/checkout/"] = PAGE
    page_server.pages["/favicon.ico"] = "ICON"
    page_server.pages["/img/visa-logo.svg"] = "<svg/>"
    page = await walk_context.new_page()
    rec = NetworkRecorder(page)
    resp = await page.goto(page_server.url("/checkout/"))
    await rec.finish_main(resp)
    await page.wait_for_load_state("networkidle")
    cap = await capture_page(
        page,
        page_type="payment_step",
        global_candidates=["Stripe", "adyen.checkout", "Klarna", "braintree"],
        cookie_names=await walk_context.cookie_names(),
        headers=rec.main_headers,
        screenshot_max_bytes=300 * 1024,
        favicon_fetch=api_favicon_fetcher(
            page.context, allow_private=True, rewrite=page_server.rewrite
        ),
    )
    assert cap.title == "Kasse" and cap.page_type == "payment_step"
    assert cap.js_globals == {"Stripe", "adyen.checkout"}
    assert "PayPal" in cap.labels and "Visa" in cap.labels and "visa-logo.svg" in cap.labels
    assert cap.payment_block_selector and "wc_payment_methods" in cap.payment_block_html
    assert cap.favicon_url.endswith("/favicon.ico") and len(cap.favicon_sha256) == 64
    assert 0 < len(cap.screenshot_jpeg) <= 300 * 1024
    assert cap.currency_text_sample == "49,90 €"
    assert rec.main_status == 200 and rec.main_url.startswith("http://shop.test/")
    assert "shop.test" in rec.hosts()
    third = rec.third_party("shop.test")
    assert "js.stripe.com" in third and "cdn.analytics.example" in third
    sig = cap.signals(rec.hosts())
    assert "https://js.stripe.com/v3/" in sig.script_srcs
    assert any("elements-inner-card" in s for s in sig.iframe_srcs)
    assert "http://shop.test/?wc-ajax=checkout" in sig.form_actions
    assert "Stripe" in sig.js_globals and sig.favicon_sha256 == cap.favicon_sha256
    rows = network_rows(rec)
    assert rows and rows[0]["url"].startswith("http://shop.test/checkout/")
    assert rows[0]["status"] == 200 and rows[0]["type"] == "document"
    small = await screenshot_jpeg(page, 10)
    assert small == b""
    fetch = api_favicon_fetcher(page.context, allow_private=False, rewrite=page_server.rewrite)
    assert await fetch("http://127.0.0.1/favicon.ico") is None  # egress rule
    assert await fetch("http://shop.test/missing.ico") is None  # 404
    await page.close()

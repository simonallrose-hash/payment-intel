"""GuardedPage + injected guard in a real Chromium (FR-CW-04, FR-CW-05, FR-CW-14)."""

from __future__ import annotations

import pytest

from payintel.crawl.checkout.browser import WalkContext
from payintel.crawl.checkout.guardrails import BuyerField, GuardrailStop, PaymentField, Purpose
from payintel.crawl.checkout.types import ActionJournal, WalkFlags, WalkStopped
from tests.unit.checkout.conftest import PageServer, make_guarded

pytestmark = pytest.mark.asyncio(loop_scope="session")

PAGE = """
<html><body>
<form id="address" action="/checkout/address" method="post"
      onsubmit="window.addressSubmitted=true;return false;">
  <input name="first_name"><input name="last_name"><input name="email"><input name="postcode">
  <select name="country"><option value="">Choose…</option><option value="FR">France</option>
    <option value="DE">Deutschland</option></select>
  <select name="size"><option value="">Bitte wählen</option><option value="s" disabled>S</option>
    <option value="m">M</option></select>
  <label><input type="checkbox" name="newsletter"> Subscribe to our newsletter</label>
  <label><input type="checkbox" name="terms" required> I agree to the terms</label>
  <label><input type="checkbox" name="remember"> Remember me</label>
  <label><input type="checkbox" name="giftwrap"> Gift wrap</label>
  <button type="submit" id="continue">Continue to payment</button>
</form>
<form id="order" action="/checkout/place-order" method="post"
      onsubmit="window.orderSubmitted=true;return false;">
  <input id="cc" name="cardnumber" autocomplete="cc-number">
  <button type="submit" id="place">Place order</button>
  <button type="submit" id="decoy" name="place_order">Continue</button>
  <button type="button" id="bypass"
          onclick="document.getElementById('order').submit()">Mehr</button>
</form>
<a id="cart" href="/cart">Cart (1)</a>
<script>
window.clicks = [];
document.getElementById('place').addEventListener('click', () => window.clicks.push('place'));
document.getElementById('continue').addEventListener('click', () => window.clicks.push('continue'));
</script>
</body></html>
"""


async def test_final_button_is_refused_and_dom_guard_blocks_programmatic_clicks(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    gp = make_guarded(page)
    with pytest.raises(GuardrailStop) as exc:
        await gp.click(gp.locator("#place"), Purpose.STEP)
    assert exc.value.stop.reason == "guardrail_final_action_only"
    assert exc.value.stop.step.value == "payment"
    with pytest.raises(GuardrailStop) as exc2:
        await gp.click(gp.locator("#decoy"), Purpose.STEP)
    assert exc2.value.decision.matched == "place_order"
    # bypass attempts straight in the DOM: click(), form.submit(), requestSubmit() are all cancelled
    await page.evaluate("document.getElementById('place').click()")
    await page.evaluate("document.getElementById('order').submit()")
    await page.evaluate("document.getElementById('order').requestSubmit()")
    await page.evaluate("document.getElementById('bypass').click()")
    blocked = await gp.dom_blocked()
    assert {b["kind"] for b in blocked} >= {"click", "submit()", "requestSubmit()"}
    assert await page.evaluate("window.orderSubmitted") is None
    assert (
        await page.evaluate("window.clicks") == []
    )  # page listener never ran: stopImmediatePropagation
    # the step button is allowed and its page handler runs
    await gp.click(gp.locator("#continue"), Purpose.STEP)
    assert await page.evaluate("window.clicks") == ["continue"]
    # the required terms box blocks native validation until ticked; then the submit goes through
    assert await page.evaluate("window.addressSubmitted") is None
    assert await gp.check_required_checkbox(gp.locator("[name=terms]"))
    await gp.click(gp.locator("#continue"), Purpose.STEP)
    assert await page.evaluate("window.addressSubmitted") is True
    assert gp.journal.of("click")[0].detail["matched"] == "continue to payment"
    assert len(gp.journal.of("click_refused")) == 2


async def test_capture_phase_cancels_every_submit(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    gp = make_guarded(page)
    await gp.enter_capture_phase()
    await page.evaluate("document.getElementById('address').requestSubmit()")
    assert await page.evaluate("window.addressSubmitted") is None
    with pytest.raises(WalkStopped):
        await gp.click(gp.locator("#continue"), Purpose.STEP)
    assert (await gp.dom_blocked())[0]["why"] == "phase:capture"


async def test_buyer_fields_come_from_identity_only(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    j = ActionJournal(lambda: 0)
    gp = make_guarded(page, journal=j)
    assert await gp.fill_buyer_field(gp.locator("[name=first_name]"), BuyerField.FIRST_NAME)
    assert await gp.fill_buyer_field(gp.locator("[name=email]"), BuyerField.EMAIL)
    assert not await gp.fill_buyer_field(gp.locator("[name=missing]"), BuyerField.CITY)
    assert await page.input_value("[name=first_name]") == "Test"
    assert await page.input_value("[name=email]") == "checkout-probe+run-1@payintel.example"
    assert await gp.select_country(gp.locator("[name=country]"))
    assert await page.input_value("[name=country]") == "DE"
    assert (
        await gp.select_first_option(gp.locator("[name=size]")) == "m"
    )  # placeholder and disabled skipped
    fields = {e.detail["field"]: e.detail["value"] for e in j.of("fill")}
    assert fields == {"first_name": "Test", "email": "checkout-probe+run-1@payintel.example"}


async def test_checkboxes_newsletter_never_optional_never_required_yes(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    gp = make_guarded(page)
    assert not await gp.check_required_checkbox(gp.locator("[name=newsletter]"))
    assert not await gp.check_required_checkbox(gp.locator("[name=remember]"))
    assert await gp.check_required_checkbox(gp.locator("[name=terms]"))
    assert await page.is_checked("[name=terms]")
    assert not await page.is_checked("[name=newsletter]")
    skipped = {e.detail["reason"].split(":")[0] for e in gp.journal.of("checkbox_skipped")}
    assert skipped == {"forbidden consent"}  # "remember me" is a forbidden consent too
    assert not await gp.check_required_checkbox(gp.locator("[name=giftwrap]"))
    assert gp.journal.of("checkbox_skipped")[-1].detail["reason"] == "optional"


async def test_payment_fields_flag_and_reference_only(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    gp_off = make_guarded(page, flags=WalkFlags(allow_payment_field_fill=False))
    assert not await gp_off.fill_payment_field(gp_off.locator("#cc"), PaymentField.NUMBER)
    assert await page.input_value("#cc") == ""
    assert (
        gp_off.journal.of("payment_fill_refused")[0].detail["reason"]
        == "allow_payment_field_fill=false"
    )
    gp = make_guarded(page)
    assert await gp.fill_payment_field(gp.locator("#cc"), PaymentField.NUMBER)
    assert await page.input_value("#cc") == "4242424242424242"
    with pytest.raises(WalkStopped):
        await gp.fill_payment_field(
            gp.locator("#cc"), PaymentField.NUMBER, number="4916338506082832"
        )
    assert await page.input_value("#cc") == "4242424242424242"
    assert await gp.fill_payment_field(gp.locator("#cc"), PaymentField.EXPIRY)
    assert await page.input_value("#cc") == "12/30"


async def test_click_budget_and_navigation_allowed(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page = await walk_context.new_page()
    page_server.pages["/"] = PAGE
    await page.goto(page_server.url("/"))
    gp = make_guarded(page)
    gp.max_clicks = 1
    await gp.click(gp.locator("#continue"), Purpose.STEP)
    with pytest.raises(WalkStopped) as exc:
        await gp.click(gp.locator("#continue"), Purpose.STEP)
    assert "click budget" in exc.value.stop.detail
    controls = await gp.find_controls("step_actions")
    assert [c.text for _, c in controls] == ["Continue to payment", "Continue"]
    # route layer: a POST to an order endpoint never leaves the browser
    await page.evaluate(
        "fetch('/checkout/place-order', {method: 'POST', body: 'x'}).catch(() => null)"
    )
    assert walk_context.stats.blocked_posts == ["http://shop.test/checkout/place-order"]
    assert ("POST", "shop.test", "/checkout/place-order", "x") not in page_server.requests
    assert page_server.requests[0][:3] == ("GET", "shop.test", "/")

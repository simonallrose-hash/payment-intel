"""Hosted-field fill (FR-CW-14): only reference test values, only with the flag, never submitted."""

from __future__ import annotations

import pytest

from payintel.crawl.checkout.browser import WalkContext
from payintel.crawl.checkout.payment_fill import detect_tokenizer, fill_test_card
from payintel.crawl.checkout.types import ActionJournal, WalkFlags
from tests.unit.checkout.conftest import PageServer, make_guarded

STRIPE_PAGE = """<!doctype html><html><body><h1>Payment</h1>
<form id="payment-form" action="/pay">
<iframe name="__privateStripeFrame1" src="/stripe-number"></iframe>
<iframe name="__privateStripeFrame2" src="/stripe-exp"></iframe>
<iframe name="__privateStripeFrame3" src="/stripe-cvc"></iframe>
<button id="submit">Pay now</button></form></body></html>"""
NUMBER_FRAME = '<html><body><input name="cardnumber" id="n"></body></html>'
EXP_FRAME = '<html><body><input name="exp-date" id="e"></body></html>'
CVC_FRAME = '<html><body><input name="cvc" id="c"></body></html>'
GENERIC_PAGE = """<!doctype html><html><body><form action="/checkout/">
<input autocomplete="cc-name" id="holder"><input autocomplete="cc-number" id="num">
<input autocomplete="cc-exp-month" id="mm"><input autocomplete="cc-exp-year" id="yy">
<input autocomplete="cc-csc" id="cvc"><button type="button">Continue</button>
</form></body></html>"""
NO_CARD_PAGE = "<!doctype html><html><body><p>Choose: PayPal, invoice</p></body></html>"


@pytest.mark.asyncio(loop_scope="session")
async def test_stripe_hosted_fields_are_filled_with_documented_values(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page_server.pages.update(
        {
            "/checkout/": STRIPE_PAGE,
            "/stripe-number": NUMBER_FRAME,
            "/stripe-exp": EXP_FRAME,
            "/stripe-cvc": CVC_FRAME,
        }
    )
    page = await walk_context.new_page()
    await page.goto(page_server.url("/checkout/"))
    journal = ActionJournal(lambda: 0)
    gp = make_guarded(page, journal=journal)
    assert await detect_tokenizer(gp) == "stripe"
    result = await fill_test_card(gp)
    assert result.tokenizer == "stripe" and result.number_filled
    assert set(result.filled) == {"number", "expiry", "cvc"}
    number = (
        await page.frame_locator("iframe[name=__privateStripeFrame1]").locator("#n").input_value()
    )
    assert number == gp.payment_values.preferred[0]
    assert number in gp.payment_values.numbers
    exp = await page.frame_locator("iframe[name=__privateStripeFrame2]").locator("#e").input_value()
    assert exp == "12/30"
    fills = journal.of("payment_fill")
    assert len(fills) == 3 and all(f.detail["value"] for f in fills)
    assert not any("POST" == m for m, *_ in page_server.requests)  # nothing submitted
    await page.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_generic_fields_and_flag_off(
    walk_context: WalkContext, page_server: PageServer
) -> None:
    page_server.pages["/checkout/"] = GENERIC_PAGE
    page_server.pages["/plain/"] = NO_CARD_PAGE
    page = await walk_context.new_page()
    await page.goto(page_server.url("/checkout/"))
    gp = make_guarded(page)
    result = await fill_test_card(gp)
    assert result.tokenizer == "generic"
    assert set(result.filled) == {"holder", "number", "expiry_month", "expiry_year", "cvc"}
    assert await page.locator("#holder").input_value() == "TEST PROBE"
    assert await page.locator("#mm").input_value() == "12"
    # flag off: fields are detected but nothing is typed, refusals are journaled
    await page.goto(page_server.url("/checkout/"))
    journal = ActionJournal(lambda: 0)
    gp = make_guarded(page, flags=WalkFlags(allow_payment_field_fill=False), journal=journal)
    result = await fill_test_card(gp)
    assert result.attempted and result.filled == []
    assert await page.locator("#num").input_value() == ""
    assert journal.of("payment_fill_refused")
    await page.goto(page_server.url("/plain/"))
    gp = make_guarded(page)
    assert await detect_tokenizer(gp) == ""
    assert not (await fill_test_card(gp)).attempted
    await page.close()

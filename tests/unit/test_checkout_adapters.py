"""FR-CW-03 adapter registry: platform selectors first, heuristic after; Shopify (stage 4)."""

from __future__ import annotations

from payintel.crawl.checkout.adapters import ADAPTERS, HEURISTIC, adapter_for
from payintel.crawl.checkout.adapters.shopify import SHOPIFY
from payintel.crawl.checkout.guardrails.page import BuyerField


def test_registry_covers_the_five_platforms_and_falls_back_to_heuristic() -> None:
    assert {a.name for a in ADAPTERS} == {
        "woocommerce",
        "magento2",
        "shopware6",
        "prestashop",
        "shopify",
    }
    assert adapter_for(None) is HEURISTIC and adapter_for("bigcartel") is HEURISTIC
    assert adapter_for("SHOPWARE").name == "shopware6"


def test_shopify_hints_come_first_and_keep_the_heuristic_behind_them() -> None:
    a = adapter_for("shopify")
    assert a.name == "shopify" and a.cart_path == "/cart" and a.checkout_path == "/checkout"
    assert a.add_to_cart[0] == "button[type=submit][name='add']"
    assert a.add_to_cart[-1] == HEURISTIC.add_to_cart[-1]
    assert len(a.add_to_cart) == len(set(a.add_to_cart))  # duplicates removed, order kept
    # the checkout fields match standard autocomplete tokens with a section prefix
    first = a.fields[BuyerField.FIRST_NAME]
    assert first[0] == "input[autocomplete~='given-name']"
    assert "input[autocomplete='given-name']" in first  # heuristic still behind it
    assert a.guest_controls == HEURISTIC.guest_controls  # guest by default: nothing to click
    assert SHOPIFY.payment_blocks == () and a.payment_blocks == HEURISTIC.payment_blocks

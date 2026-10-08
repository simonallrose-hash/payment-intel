"""PrestaShop 1.7 / 8 (classic theme) selectors: /order with the guest tab in step 1."""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

PRESTASHOP = AdapterHints(
    name="prestashop",
    platform_ids=("prestashop",),
    product_links=("a.product-thumbnail", ".product-title a", ".product-miniature a[href]"),
    product_url_patterns=(r"/\d+-[\w-]+\.html$", r"/[\w-]+/\d+-"),
    variant_selects=(".product-variants select", "select[name^='group']"),
    variant_options=(".product-variants input[type=radio]",),
    add_to_cart=("button.add-to-cart", "form#add-to-cart-or-refresh button[type=submit]"),
    cart_path="/cart?action=show",
    cart_links=("a[href*='controller=cart']", "a[href*='/cart']", ".blockcart a"),
    cart_items=(".cart-item", ".cart-items .product-line-grid"),
    checkout_path="/order",
    checkout_links=("a[href*='/order']", "a[href*='controller=order']", ".checkout a.btn"),
    guest_controls=(
        "#checkout-guest-form",
        "a[href='#checkout-guest-form']",
        "label[for*='guest' i]",
    ),
    login_form=("#checkout-login-form form", "form#login-form"),
    register_form=("#checkout-guest-form form", "form#customer-form"),
    fields={
        BuyerField.FIRST_NAME: ("input[name='firstname']",),
        BuyerField.LAST_NAME: ("input[name='lastname']",),
        BuyerField.EMAIL: ("input[name='email']",),
        BuyerField.STREET_LINE: ("input[name='address1']",),
        BuyerField.POSTCODE: ("input[name='postcode']",),
        BuyerField.CITY: ("input[name='city']",),
        BuyerField.PHONE: ("input[name='phone']",),
    },
    country_selects=("select[name='id_country']",),
    region_selects=("select[name='id_state']",),
    salutation_selects=(),
    required_checkboxes=(
        "input[name='psgdpr']",
        "input[name='customer_privacy']",
        "#conditions_to_approve\\[terms-and-conditions\\]",
    ),
    shipping_options=(".delivery-option input[type=radio]", "input[name^='delivery_option']"),
    step_buttons=(
        "button[name='continue']",
        "button[name='confirm-addresses']",
        "button[name='confirmDeliveryOption']",
    ),
    payment_blocks=(".payment-options", "#checkout-payment-step"),
    payment_options=(".payment-options input[type=radio]", "input[name='payment-option']"),
    login_fields={
        "email": ("#checkout-login-form input[name='email']",),
        "password": ("#checkout-login-form input[name='password']",),
    },
    register_fields={"password": ("input[name='password']",), "confirm": ()},
)

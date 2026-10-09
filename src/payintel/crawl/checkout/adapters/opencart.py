"""OpenCart 3.0 and 4.0 selectors from the default theme templates in the OpenCart repository.

Sources: `catalog/view/template/product/product.twig`, `product/thumb.twig`,
`checkout/register.twig` (4.0), `checkout/guest.twig` and `checkout/checkout.twig`
(3.0), `checkout/cart_list.twig`, `checkout/shipping_method.twig`,
`checkout/payment_method.twig`. Routes are not SEO-rewritten by default
(`index.php?route=checkout/cart`), SEO URLs are keywords under the root, so
product discovery relies on the `product-thumb` boxes rather than a path pattern.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

OPENCART = AdapterHints(
    name="opencart",
    platform_ids=("opencart",),
    product_links=(".product-thumb .image a[href]", ".product-thumb h4 a[href]"),
    product_url_patterns=(r"route=product/product",),
    variant_selects=("#form-product select[name^='option']", "#product select[name^='option']"),
    add_to_cart=("#button-cart", "#form-product button[type=submit]"),
    cart_path="index.php?route=checkout/cart",
    cart_links=("a[href*='route=checkout/cart']", "#header-cart a[href]"),
    cart_items=("#shopping-cart table tbody tr", "#shopping-cart input[name='quantity']"),
    checkout_path="index.php?route=checkout/checkout",
    checkout_links=("a[href*='route=checkout/checkout']",),
    guest_controls=("#input-guest", "input[name='account'][value='guest']"),
    login_form=("#form-login",),
    register_controls=("#input-register", "input[name='account'][value='register']"),
    register_form=("#form-register",),
    fields={
        BuyerField.FIRST_NAME: ("#input-firstname", "#input-payment-firstname"),
        BuyerField.LAST_NAME: ("#input-lastname", "#input-payment-lastname"),
        BuyerField.EMAIL: ("#input-email", "#input-payment-email"),
        BuyerField.PHONE: ("#input-telephone", "#input-payment-telephone"),
        BuyerField.STREET_LINE: ("#input-payment-address-1", "#input-shipping-address-1"),
        BuyerField.CITY: ("#input-payment-city", "#input-shipping-city"),
        BuyerField.POSTCODE: ("#input-payment-postcode", "#input-shipping-postcode"),
    },
    country_selects=("#input-payment-country", "#input-shipping-country"),
    region_selects=("#input-payment-zone", "#input-shipping-zone"),
    required_checkboxes=("#input-register-agree", "#input-checkout-agree", "input[name='agree']"),
    shipping_options=("input[name='shipping_method']",),
    step_buttons=(
        "#button-register",
        "#button-guest",
        "#button-payment-address",
        "#button-shipping-address",
        "#button-shipping-method",
        "#button-payment-method",
    ),
    payment_blocks=("#form-payment-method", "#collapse-payment-method", "#modal-payment"),
    payment_options=("input[name='payment_method']",),
    login_fields={"email": ("#input-email",), "password": ("#input-password",)},
    register_fields={"password": ("#input-password",), "confirm": ("#input-confirm",)},
)

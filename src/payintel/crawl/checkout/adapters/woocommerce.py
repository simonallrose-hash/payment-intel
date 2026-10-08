"""WooCommerce (classic checkout and block checkout) selectors from the WooCommerce templates."""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

WOOCOMMERCE = AdapterHints(
    name="woocommerce",
    platform_ids=("woocommerce",),
    product_links=("a.woocommerce-LoopProduct-link", "ul.products li.product a[href]"),
    product_url_patterns=(r"/product/", r"/produkt/", r"/produit/", r"/producto/"),
    variant_selects=("form.variations_form select", "table.variations select"),
    add_to_cart=(
        "button.single_add_to_cart_button",
        "form.cart button[type=submit]",
        "form.cart button[name='add-to-cart']",
        "a.add_to_cart_button",
    ),
    cart_path="/cart/",
    cart_links=("a.cart-contents", "a.wc-block-mini-cart__button", "a[href*='/cart/']"),
    cart_items=(
        "form.woocommerce-cart-form tr.cart_item",
        ".wc-block-cart-items__row",
        ".woocommerce-cart-form__cart-item",
    ),
    checkout_path="/checkout/",
    checkout_links=("a.checkout-button", "a.wc-proceed-to-checkout", "a[href*='/checkout/']"),
    guest_controls=(),
    fields={
        BuyerField.FIRST_NAME: ("#billing_first_name", "#billing-first_name"),
        BuyerField.LAST_NAME: ("#billing_last_name", "#billing-last_name"),
        BuyerField.STREET_LINE: ("#billing_address_1", "#billing-address_1"),
        BuyerField.POSTCODE: ("#billing_postcode", "#billing-postcode"),
        BuyerField.CITY: ("#billing_city", "#billing-city"),
        BuyerField.PHONE: ("#billing_phone", "#billing-phone"),
        BuyerField.EMAIL: ("#billing_email", "#email"),
    },
    country_selects=("#billing_country", "#billing-country select"),
    region_selects=("#billing_state", "#billing-state select"),
    required_checkboxes=("#terms", "input[name='terms']", ".wc-block-checkout__terms input"),
    shipping_options=(
        "input[name^='shipping_method']",
        ".wc-block-components-radio-control__input",
    ),
    step_buttons=(),
    payment_blocks=(
        "#payment",
        ".woocommerce-checkout-payment",
        ".wc-block-checkout__payment-method",
    ),
    payment_options=("input[name='payment_method']", ".wc_payment_methods input[type=radio]"),
)

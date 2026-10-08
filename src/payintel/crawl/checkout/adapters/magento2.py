"""Magento 2 (Luma / Hyvä one-page checkout) selectors from the Magento_Checkout templates."""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

MAGENTO2 = AdapterHints(
    name="magento2",
    platform_ids=("magento2", "magento"),
    product_links=("a.product-item-link", "li.product-item a.product-item-photo"),
    product_url_patterns=(r"\.html$",),
    variant_selects=("select.super-attribute-select", "#product-options-wrapper select"),
    variant_options=(".swatch-attribute .swatch-option",),
    add_to_cart=("#product-addtocart-button", "button.tocart"),
    cart_path="/checkout/cart/",
    cart_links=("a.action.showcart", "a[href*='/checkout/cart']"),
    cart_items=("#shopping-cart-table .cart.item", ".cart-items .item", "tbody.cart.item"),
    checkout_path="/checkout/",
    checkout_links=("button.checkout", "#top-cart-btn-checkout", "a[href*='/checkout/']"),
    guest_controls=(),
    login_form=("form#login-form", "form.form-login"),
    fields={
        BuyerField.EMAIL: ("#customer-email", "input[name='username']"),
        BuyerField.FIRST_NAME: ("input[name='firstname']",),
        BuyerField.LAST_NAME: ("input[name='lastname']",),
        BuyerField.STREET_LINE: ("input[name='street[0]']",),
        BuyerField.POSTCODE: ("input[name='postcode']",),
        BuyerField.CITY: ("input[name='city']",),
        BuyerField.PHONE: ("input[name='telephone']",),
    },
    country_selects=("select[name='country_id']",),
    region_selects=("select[name='region_id']",),
    shipping_options=(
        "#checkout-shipping-method-load input[type=radio]",
        ".table-checkout-shipping-method input[type=radio]",
    ),
    step_buttons=("#shipping-method-buttons-container button.continue", "button.continue"),
    payment_blocks=("#checkout-payment-method-load", ".checkout-payment-method", "#payment"),
    payment_options=("#checkout-payment-method-load input[type=radio]", ".payment-method input"),
    login_fields={
        "email": ("#login-form input[name='username']", "input[name='login[username]']"),
        "password": ("#login-form input[name='password']", "input[name='login[password]']"),
    },
    register_fields={
        "password": ("#password", "input[name='password']"),
        "confirm": ("#password-confirmation", "input[name='password_confirmation']"),
    },
    register_controls=("a[href*='/customer/account/create']",),
    register_form=("form.form-create-account",),
)

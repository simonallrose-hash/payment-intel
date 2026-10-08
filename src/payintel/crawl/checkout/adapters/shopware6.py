"""Shopware 6 storefront selectors (register page with guest checkbox, /checkout/confirm)."""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

SHOPWARE6 = AdapterHints(
    name="shopware6",
    platform_ids=("shopware6", "shopware"),
    product_links=("a.product-name", ".product-box a.product-image-link", ".product-box a[href]"),
    product_url_patterns=(r"/[\w-]+/[\w-]+$", r"/detail/"),
    variant_selects=("select.product-detail-configurator-select",),
    variant_options=(".product-detail-configurator-option-input",),
    add_to_cart=("button.btn-buy", "form.buy-widget button[type=submit]"),
    cart_path="/checkout/cart",
    cart_links=("a.header-cart-btn", "a[href*='/checkout/cart']"),
    cart_items=(".line-item", ".cart-item", ".checkout-aside-item"),
    checkout_path="/checkout/confirm",
    checkout_links=("a.begin-checkout-btn", "a[href*='/checkout/confirm']"),
    guest_controls=("#personalGuest", "input[name='guest']"),
    login_form=("form.login-form", "form[action*='/account/login']"),
    register_form=("form.register-form", "form[action*='/account/register']"),
    fields={
        BuyerField.FIRST_NAME: ("#personalFirstName", "#billingAddressFirstName"),
        BuyerField.LAST_NAME: ("#personalLastName", "#billingAddressLastName"),
        BuyerField.EMAIL: ("#personalMail", "#loginMail"),
        BuyerField.STREET_LINE: ("#billingAddressAddressStreet",),
        BuyerField.POSTCODE: ("#billingAddressAddressZipcode",),
        BuyerField.CITY: ("#billingAddressAddressCity",),
        BuyerField.PHONE: ("#billingAddressAddressPhoneNumber", "#personalPhoneNumber"),
    },
    country_selects=("#billingAddressAddressCountry",),
    region_selects=("#billingAddressAddressCountryState",),
    salutation_selects=("#personalSalutation",),
    required_checkboxes=("#tos", "input[name='tos']", "#acceptedDataProtection"),
    shipping_options=("input[name='shippingMethodId']",),
    step_buttons=(".register-submit button", "form.register-form button[type=submit]"),
    payment_blocks=(".payment-methods", ".confirm-payment", ".checkout-confirm-payment"),
    payment_options=("input[name='paymentMethodId']",),
    login_fields={"email": ("#loginMail",), "password": ("#loginPassword",)},
    register_fields={
        "password": ("#personalPassword",),
        "confirm": ("#personalPasswordConfirmation",),
    },
)

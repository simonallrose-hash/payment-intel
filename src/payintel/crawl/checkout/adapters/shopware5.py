"""Shopware 5 Responsive theme selectors (`shopware5/shopware`, `themes/Frontend/Bare`).

Sources: `frontend/detail/buy.tpl` (`form.buybox--form[name=sAddToBasket]`,
`button.buybox--button`, `select#sQuantity`), `frontend/listing/product-box/*.tpl`
(`a.product--title`, `a.product--image`), `frontend/checkout/cart.tpl` + `items/product.tpl`
(`.row--product`, `a.btn--checkout-proceed`), `frontend/register/personal_fieldset.tpl`
and `billing_fieldset.tpl` (ids `firstname`, `lastname`, `register_personal_email`,
`street`, `zipcode`, `city`, `country`, `phone`; guest checkbox
`#register_personal_skipLogin`), `frontend/checkout/change_shipping.tpl`
(`input[name=sDispatch]`), `change_payment.tpl` (`input[name=payment]#payment_mean<id>`),
`confirm.tpl` (`.payment--method-info`, `#sAGB`, `form#confirm--form`).
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

SHOPWARE5 = AdapterHints(
    name="shopware5",
    platform_ids=("shopware5",),
    product_links=("a.product--title[href]", "a.product--image[href]", ".product--box a[href]"),
    product_url_patterns=(r"/detail/index/sArticle/\d+", r"/[\w-]+/\d+/[\w-]+$"),
    variant_selects=("form.buybox--form select[name^='group']",),
    add_to_cart=("form.buybox--form button.buybox--button", "button.buybox--button"),
    cart_path="/checkout/cart",
    cart_links=("a.btn--cart", "a[href*='/checkout/cart']"),
    cart_items=(".row--product", ".table--tr.row--product", "select[name='sQuantity']"),
    checkout_path="/checkout/confirm",
    checkout_links=("a.btn--checkout-proceed", "a[href*='/checkout/confirm']"),
    guest_controls=("#register_personal_skipLogin",),
    login_form=("form.register--login", "form[action*='/account/login']"),
    register_form=("form.register--form", "form[action*='/register/saveRegister']"),
    fields={
        BuyerField.FIRST_NAME: ("#firstname", "input[name='register[personal][firstname]']"),
        BuyerField.LAST_NAME: ("#lastname", "input[name='register[personal][lastname]']"),
        BuyerField.EMAIL: ("#register_personal_email", "input[name='register[personal][email]']"),
        BuyerField.STREET_LINE: ("#street", "input[name='register[billing][street]']"),
        BuyerField.POSTCODE: ("#zipcode", "input[name='register[billing][zipcode]']"),
        BuyerField.CITY: ("#city", "input[name='register[billing][city]']"),
        BuyerField.PHONE: ("#phone", "input[name='register[personal][phone]']"),
    },
    country_selects=("#country", "select[name='register[billing][country]']"),
    region_selects=("select[name^='register[billing][country_state_']", "select.select--state"),
    salutation_selects=("#salutation", "select[name='register[personal][salutation]']"),
    required_checkboxes=("#sAGB",),
    shipping_options=("input[name='sDispatch']", ".dispatch--method input[type=radio]"),
    step_buttons=("form.register--form button[type=submit]",),
    payment_blocks=(".payment--method-list", ".payment--panel", ".payment--method-info"),
    payment_options=("input[name='payment']", ".payment--method input[type=radio]"),
    login_fields={
        "email": ("input[name='email']",),
        "password": ("input[name='password']",),
    },
    register_fields={
        "password": ("#register_personal_password",),
        "confirm": ("#register_personal_passwordConfirmation",),
    },
)

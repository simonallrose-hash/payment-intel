"""OXID eShop 7 selectors from the Wave theme repository (`OXID-eSales/wave-theme`, `tpl/`).

Sources: `page/details/inc/productmain.tpl` (form `js-oxProductForm`, `fnc=tobasket`,
`#toBasket`, amount `am`), `widget/product/listitem_grid.tpl` (`a.title`),
`page/checkout/basket.tpl` + `inc/basketcontents*.tpl` (`#basket_form`,
`li#list_cartItem_N`, `aproducts[..][am]`, next step posts `cl=user`),
`page/checkout/inc/options.tpl` (`form#optionNoRegistration` = checkout without
registration), `form/login.tpl` (`#lgn_usr`, `#lgn_pwd`), `form/fieldset/user_billing.tpl`
(`invadr_oxuser__ox*` with `billing …` autocomplete), `page/checkout/payment.tpl`
(`select[name=sShipSet]`, `form#payment`) and `inc/payment_other.tpl`
(`input[name=paymentid]#payment_<id>`). Controllers are addressed by `cl=`; the SEO
URL of the basket (`/warenkorb/`) is language dependent, so the adapter uses `cl=`.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

OXID = AdapterHints(
    name="oxid",
    platform_ids=("oxid",),
    product_links=("a.title[href]", "form[name^='tobasket'] .picture a[href]"),
    product_url_patterns=(r"cl=details", r"\.html$"),
    add_to_cart=("#toBasket", "form.js-oxProductForm button[type=submit]"),
    cart_path="index.php?cl=basket",
    cart_links=("a[href*='cl=basket']", "a[href*='/warenkorb/']", "a[href*='/cart/']"),
    cart_items=(
        "li[id^='list_cartItem_']",
        "#basketcontents_list .row",
        "input[name^='aproducts']",
    ),
    checkout_path="index.php?cl=user",
    checkout_links=("form:has(input[name='cl'][value='user']) button.nextStep",),
    guest_controls=("form#optionNoRegistration button[type=submit]",),
    login_form=("form#optionLogin", "form#login"),
    register_controls=("form#optionRegistration button[type=submit]",),
    fields={
        BuyerField.FIRST_NAME: ("#invadr_oxuser__oxfname",),
        BuyerField.LAST_NAME: ("#invadr_oxuser__oxlname",),
        BuyerField.STREET_LINE: ("#invadr_oxuser__oxstreet",),
        BuyerField.HOUSE_NUMBER: ("#invadr_oxuser__oxstreetnr",),
        BuyerField.POSTCODE: ("#invadr_oxuser__oxzip",),
        BuyerField.CITY: ("#invadr_oxuser__oxcity",),
        BuyerField.PHONE: ("#invadr_oxuser__oxfon",),
        BuyerField.EMAIL: ("#userLoginName", "input[name='lgn_usr']"),
    },
    country_selects=("#invCountrySelect",),
    region_selects=("#invadr_oxuser__oxstateid",),
    salutation_selects=("#invadr_oxuser__oxsal",),
    shipping_options=("select[name='sShipSet']",),
    step_buttons=("button.nextStep[type=submit]", "#paymentNextStepBottom"),
    payment_blocks=("form#payment", "#paymentHeader"),
    payment_options=("input[name='paymentid']", "dd.payment-option input[type=radio]"),
    login_fields={"email": ("#lgn_usr",), "password": ("#lgn_pwd",)},
)

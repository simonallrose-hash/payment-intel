"""Magento 1 / OpenMage base theme selectors (`OpenMage/magento-lts`, `base/default/template`).

Sources: `catalog/product/view/addtocart.phtml` (`#product-addtocart-button.btn-cart`, `#qty`),
`catalog/product/list.phtml` (`a.product-image`, `h2.product-name a`), `checkout/cart.phtml`
(`#shopping-cart-table`) + `cart/item/default.phtml` (`cart[<id>][qty]`),
`checkout/onepage/link.phtml` (`button.btn-proceed-checkout`), `onepage/login.phtml`
(`input[id='login:guest']`, `#login-email`, `#login-password`), `onepage/billing.phtml`
(`billing:firstname` … `billing:telephone`, `#co-billing-form`),
`onepage/shipping_method/available.phtml` (`input[name=shipping_method]#s_method_*`),
`onepage/payment/methods.phtml` (`input[name='payment[method]']#p_method_*`).
Ids contain colons, so they are matched with attribute selectors.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

MAGENTO1 = AdapterHints(
    name="magento1",
    platform_ids=("magento1",),
    product_links=("a.product-image[href]", "h2.product-name a[href]", ".products-grid li.item a"),
    product_url_patterns=(r"\.html$",),
    variant_selects=("#product_addtocart_form select.super-attribute-select",),
    add_to_cart=("#product-addtocart-button", "button.btn-cart"),
    cart_path="/checkout/cart/",
    cart_links=("a[href*='/checkout/cart']",),
    cart_items=("#shopping-cart-table tbody tr", "input[name^='cart['][name$='[qty]']"),
    checkout_path="/checkout/onepage/",
    checkout_links=("button.btn-proceed-checkout", "a[href*='/checkout/onepage']"),
    guest_controls=("input[id='login:guest']", "input[name='checkout_method'][value='guest']"),
    login_form=("#login-form",),
    register_controls=("input[id='login:register']",),
    fields={
        BuyerField.FIRST_NAME: ("input[id='billing:firstname']",),
        BuyerField.LAST_NAME: ("input[id='billing:lastname']",),
        BuyerField.EMAIL: ("input[id='billing:email']",),
        BuyerField.STREET_LINE: ("input[id='billing:street1']",),
        BuyerField.CITY: ("input[id='billing:city']",),
        BuyerField.POSTCODE: ("input[id='billing:postcode']",),
        BuyerField.PHONE: ("input[id='billing:telephone']",),
    },
    country_selects=("select[id='billing:country_id']",),
    region_selects=("select[id='billing:region_id']",),
    shipping_options=("input[name='shipping_method']", "dl.sp-methods input[type=radio]"),
    step_buttons=("#co-billing-form button.button", "#co-shipping-method-form button.button"),
    payment_options=("input[name='payment[method]']", "input[id^='p_method_']"),
    login_fields={"email": ("#login-email",), "password": ("#login-password",)},
)

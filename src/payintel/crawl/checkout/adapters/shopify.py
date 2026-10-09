"""Shopify storefront selectors (Online Store 2.0 themes, Dawn as the reference).

Sources, in order of confidence:

* storefront markup from the Dawn theme (`sections/main-product.liquid`,
  `snippets/buy-buttons.liquid`, `sections/main-cart-footer.liquid`,
  `snippets/card-product.liquid`): product links `/products/<handle>`, the
  product form posts to `/cart/add` with a hidden `id` (variant) and submits
  through `button[type=submit][name="add"]`; the cart lives at `/cart` and its
  checkout control is `button#checkout[name="checkout"][form="cart"]`;
* the HTML standard for the checkout page: Shopify's checkout is not a theme
  and its markup is not documented, but its fields carry standard
  `autocomplete` tokens with a section prefix (`shipping given-name`), so the
  adapter matches tokens with `~=` where the heuristic matches whole values.
  Everything else on the checkout (payment block, radios) is left to the
  heuristic on purpose: nothing undocumented is hard-coded (ADR-0029).

The checkout stays on the store's own domain (`/checkouts/…`), so the eTLD+1
rule of the walker holds; a store that still sends buyers to a
`*.myshopify.com` host is outside the eTLD+1 and stops with
`checkout_not_found`, which is the intended behaviour.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

SHOPIFY = AdapterHints(
    name="shopify",
    platform_ids=("shopify",),
    product_links=(
        ".card__heading a[href*='/products/']",
        "a.full-unstyled-link[href*='/products/']",
        "a[href*='/products/']",
    ),
    product_url_patterns=(r"/products/[\w-]+",),
    variant_selects=("form[action$='/cart/add'] select[name^='options']", ".product-form select"),
    variant_options=("variant-radios input[type=radio]", "fieldset.product-form__input label"),
    add_to_cart=(
        "button[type=submit][name='add']",
        "button.product-form__submit",
        "form[action$='/cart/add'] button[type=submit]",
    ),
    cart_path="/cart",
    cart_links=("a[href='/cart']", "a#cart-icon-bubble", "a.header__icon--cart"),
    cart_items=("tr.cart-item", ".cart-item", "cart-items .cart-item"),
    checkout_path="/checkout",
    checkout_links=("button#checkout[name='checkout']", "button[name='checkout']"),
    guest_controls=(),  # the checkout asks for an e-mail: guest by default
    fields={
        BuyerField.EMAIL: ("input[autocomplete~='email']", "input[type=email]"),
        BuyerField.FIRST_NAME: ("input[autocomplete~='given-name']",),
        BuyerField.LAST_NAME: ("input[autocomplete~='family-name']",),
        BuyerField.STREET_LINE: ("input[autocomplete~='address-line1']",),
        BuyerField.POSTCODE: ("input[autocomplete~='postal-code']",),
        BuyerField.CITY: ("input[autocomplete~='address-level2']",),
        BuyerField.PHONE: ("input[autocomplete~='tel']",),
    },
    country_selects=("select[autocomplete~='country']",),
    region_selects=("select[autocomplete~='address-level1']",),
)

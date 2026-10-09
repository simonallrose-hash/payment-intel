"""BigCommerce selectors from Cornerstone (storefront theme) and checkout-js (optimised checkout).

Sources: `templates/components/products/add-to-cart.html` (`input#form-action-addToCart`,
`qty[]`), `products/card.html` (`article.card`, `a.card-figure__link`, `h3.card-title a`),
`templates/pages/cart.html` (`a.button--primary[data-primary-checkout-now-action]` to
`urls.checkout.single_address`), `components/cart/content.html` (`tr.cart-item`, `qty-<id>`);
checkout-js `customer/EmailField.tsx` (`input#email[autocomplete=email]`),
`customer/GuestForm.tsx` (`#checkout-customer-continue`), `address/AddressFormType.ts`
(standard `autocomplete` tokens per field), `shipping/shippingOption/ShippingOptionsList.tsx`
(`id^=shippingOptionRadio-`), `payment/paymentMethod/PaymentMethodList.tsx`
(`input[name=paymentProviderRadio]`). Product and cart URLs are store-configurable, so the
cart is reached through the header link and the checkout through the cart page button.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import AdapterHints
from payintel.crawl.checkout.guardrails.page import BuyerField

BIGCOMMERCE = AdapterHints(
    name="bigcommerce",
    platform_ids=("bigcommerce",),
    product_links=("a.card-figure__link[href]", ".card-title a[href]", "article.card a[href]"),
    variant_selects=("form[data-cart-item-add] select[name^='attribute']",),
    variant_options=("form[data-cart-item-add] input[type=radio][name^='attribute']",),
    add_to_cart=("#form-action-addToCart", "form[data-cart-item-add] [type=submit]"),
    cart_path="/cart.php",
    cart_links=("a.navUser-action--cart", "a[href='/cart.php']", "a[href*='/cart.php']"),
    cart_items=("tr.cart-item", "[data-item-row]", "input.cart-item-qty-input"),
    checkout_links=(
        "a[data-primary-checkout-now-action]",
        "a.button--primary[href*='/checkout']",
    ),
    guest_controls=("#checkout-customer-continue",),
    fields={
        BuyerField.EMAIL: ("input#email[type=email]", "input[autocomplete~='email']"),
        BuyerField.FIRST_NAME: ("input[autocomplete~='given-name']",),
        BuyerField.LAST_NAME: ("input[autocomplete~='family-name']",),
        BuyerField.STREET_LINE: ("input[autocomplete~='address-line1']",),
        BuyerField.POSTCODE: ("input[autocomplete~='postal-code']",),
        BuyerField.CITY: ("input[autocomplete~='address-level2']",),
        BuyerField.PHONE: ("input[autocomplete~='tel']",),
    },
    country_selects=("select[autocomplete~='country']",),
    region_selects=("select[autocomplete~='address-level1']",),
    shipping_options=("input[id^='shippingOptionRadio-']",),
    step_buttons=("#checkout-customer-continue", "#checkout-shipping-continue"),
    payment_options=("input[name='paymentProviderRadio']",),
)
